"""Contract discovery must retain ambiguity instead of claiming compatibility."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("forge_contracts", REPO / "scripts/forge-contracts.py")
contracts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contracts)


class ContractsTests(unittest.TestCase):
    def commands(self, source):
        rows, skipped = contracts.python_rows(source)
        self.assertEqual(skipped, [])
        return [r for r in rows if "argv" in r and "callee" in r]

    def test_direct_argv_excludes_other_tools_and_table_rows(self):
        rows = self.commands("""
subprocess.run(["gh", "pr", "list", "--json", "number,title"])
subprocess.run(["git", "fetch"])
rows.append(["gh", "git", "uvx"])
w.writerow(["gh", "auth", "status"])
shlex.join(["gh", "pr", "view"])
subprocess.CompletedProcess(["gh", "pr", "list"], 0)
""")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "direct_argv")
        self.assertEqual(rows[0]["contract"]["action"], "list")

    def test_named_wrappers_are_explicitly_unverified(self):
        rows = self.commands("""
self._gh(["api", "user"])
gh_json("pr", "view", "3")
run(["gh", "auth", "token"])
read(["api", "user"])
read(path)
""")
        self.assertEqual(
            [r["kind"] for r in rows],
            ["wrapper_candidate", "wrapper_candidate", "executor_candidate", "callback_candidate"],
        )
        self.assertEqual(rows[1]["argv"], ["pr", "view", "3"])

    def test_fstrings_formats_spreads_and_names_are_not_evaluated(self):
        rows = self.commands("""
subprocess.run(["gh", "api", f"repos/{repo!s}/pulls/{number:04d}", *flags])
self._gh(command)
subprocess.run(["gh"] + arguments + ["--paginate"])
""")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["contract"]["resolution"] == "dynamic" for r in rows))
        self.assertEqual(rows[0]["argv"][-1], {"spread": "flags"})
        self.assertIn("04d", str(rows[0]["argv"]))
        self.assertEqual(rows[1]["argv"], [{"spread": "command"}])

    def test_dynamic_executors_and_nested_launchers_remain_review_candidates(self):
        rows = self.commands(
            'subprocess.run(command)\nsubprocess.run(["nix", "shell", "--command", "gh", "pr", "view"])'
        )
        self.assertEqual([r["kind"] for r in rows], ["dynamic_executor_candidate", "embedded_argv_candidate"])
        self.assertNotIn("group", rows[1]["contract"])

    def test_keyword_subprocess_and_literal_concat(self):
        rows = self.commands('subprocess.run(args=["gh", "api", "us" + "er"])')
        self.assertEqual(rows[0]["argv"], ["gh", "api", "user"])

    def test_flag_scan_cannot_claim_option_arity(self):
        row = self.commands('self._gh(["pr", "create", "--title", "--json", "--body=x"])')[0]
        self.assertIn("lexical", row["contract"]["flag_scan"])
        self.assertEqual(row["contract"]["flags"], ["--body", "--json", "--title"])
        row = self.commands('subprocess.run(["gh", "--repo", "pr", "api", "user"])')[0]
        self.assertNotIn("group", row["contract"])

    def test_queries_record_composition_and_fstring_once(self):
        rows, skipped = contracts.python_rows("""
q = "query($id:ID!){node(id:$id){id}}"
x = "mutation($id:ID!){" + fields + "}"
y = f"query{{repository(owner:{owner!r}){{id}}}}"
""")
        self.assertEqual(skipped, [])
        queries = [r for r in rows if r["kind"] == "graphql_literal"]
        self.assertEqual(len(queries), 3)
        self.assertEqual(queries[1]["composition"], "string_piece")
        self.assertIn("format", queries[2]["query"])
        self.assertEqual(queries[2]["composition"], "dynamic_fstring")
        extra, _ = contracts.python_rows('q = "{viewer{login}}"\nf = "fragment Who on User{login}"')
        self.assertEqual(len(extra), 2)

    def test_python_agent_instruction_literals_are_candidates_with_source_ranges(self):
        rows, skipped = contracts.python_rows('prompt = "Read the PR\\ngh pr view 3 --json title"\n')
        self.assertEqual(skipped, [])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["kind"], "python_string_candidate")
        self.assertEqual((row["line"], row["end_line"], row["template_line"]), (1, 1, 2))
        self.assertEqual(row["text"], "gh pr view 3 --json title")
        dynamic, _ = contracts.python_rows('prompt = f"gh {command} --repo {repo}"')
        self.assertEqual(dynamic[0]["text"], "gh {command} --repo {repo}")
        self.assertIn("format", dynamic[0]["template"])

    def fixture(self, files):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, data in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)

        def git(*args):
            return subprocess.check_output(["git", "-C", str(root), *args])

        git("init", "-q")
        git("add", ".")
        git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            "commit",
            "-qm",
            "Fixture",
        )
        return root, git("rev-parse", "HEAD").decode().strip()

    def document(self, root):
        return {
            "schema": "tauceti.forge-inventory/v1",
            "scanner_sha256": hashlib.sha256(Path(contracts.inventory.__file__).read_bytes()).hexdigest(),
            "repositories": [contracts.inventory.inspect("Fixture", root, "HEAD")],
        }

    def test_assigned_command_and_conditional_flags_are_linked_without_evaluation(self):
        rows, _ = contracts.python_rows(
            '\ndef publish():\n    command = ["gh", "workflow", "run", "pr-build.yml"]\n    if extra:\n        command.extend(["--ref", branch])\n    run(command)\n'
        )
        self.assertTrue(any(r["kind"] == "argv_literal" for r in rows))
        call = next(r for r in rows if r["kind"] == "linked_executor_candidate")
        self.assertEqual(call["binding_sources"][0]["name"], "command")
        self.assertEqual(call["binding_sources"][0]["value"], ["gh", "workflow", "run", "pr-build.yml"])
        self.assertTrue(any(r["kind"] == "argv_extension_candidate" for r in rows))

    def test_variadic_calls_execv_and_remaining_arguments_are_preserved(self):
        rows = self.commands(
            '\n_cmd("gh", "secret", "set", name)\nasyncio.create_subprocess_exec("gh", "pr", "list")\nos.execv(binary, ["gh", *args])\ngh_json(f"repos/{repo}", "--paginate", *flags)\ngh(["pr", "list"], token)\n'
        )
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["argv"][:3], ["gh", "secret", "set"])
        self.assertEqual(rows[2]["argv"], ["gh", {"spread": "args"}])
        self.assertEqual(rows[2]["executable"], {"python": "binary"})
        self.assertEqual(rows[3]["argv"][1:], ["--paginate", {"spread": "flags"}])
        self.assertEqual(rows[4]["argv"], ["pr", "list"])

    def test_quote_spanning_graphql_preserves_body_flags_and_range(self):
        rows = contracts.text_rows(
            "gh api graphql -f query='query($id:ID!){\n node(id:$id){id}\n}' -f id=abc\ngh pr list"
        )
        shell = [r for r in rows if r["kind"] == "shell_candidate"]
        self.assertEqual(len(shell), 2)
        self.assertEqual((shell[0]["line"], shell[0]["end_line"]), (1, 3))
        self.assertEqual(shell[0]["tokens"][-2:], ["-f", "id=abc"])
        self.assertIn("node(id:$id){id}", shell[0]["tokens"][4])

    def test_typescript_http_query_text_and_field_prefixed_queries_are_candidates(self):
        rows = contracts.text_rows(
            'const q = `query($id:ID!){node(id:$id){id}}`;\nfetch("/graphql", {body: JSON.stringify({query:q})})'
        )
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["kind"] == "graphql_text_candidate" for r in rows))
        queries, _ = contracts.python_rows('q="query=query{viewer{login}}"\na="{organization(login: $org){id}}"')
        self.assertEqual(queries[0]["query"], "query{viewer{login}}")
        self.assertEqual(queries[0]["field_prefix"], "query=")
        self.assertEqual(len(queries), 2)

    def test_ids_separate_occurrences_from_shared_content_and_ignore_review_wording(self):
        source = 'subprocess.run(["gh", "auth", "status"]); subprocess.run(["gh", "auth", "status"])'
        rows, _ = contracts.python_rows(source)
        a = [
            contracts.identify({"path": "a.py", "blob": "b" * 40, **r}, "Fixture", "a" * 40, "inventory") for r in rows
        ]
        b = [
            contracts.identify({"path": "a.py", "blob": "b" * 40, **r}, "Fixture", "c" * 40, "declared_pin")
            for r in rows
        ]
        self.assertEqual(len({r["id"] for r in a + b}), len(a + b))
        self.assertEqual([r["content_id"] for r in a], [r["content_id"] for r in b])
        changed = contracts.identify(
            {**a[0], "review": "new prose", "contract": {"flag_scan": "new prose"}}, "Fixture", "a" * 40, "inventory"
        )
        self.assertEqual(changed["id"], a[0]["id"])

    def test_wrapper_only_sources_and_extensionless_python_are_selected(self):
        root, _ = self.fixture(
            {
                "wrapper.py": 'gh_json("pr", "list")',
                "launcher": '#!/usr/bin/env -S uv run --script\n_cmd("gh", "cache", "list")',
            }
        )
        self.assertFalse(any(r["path"] == "wrapper.py" for r in self.document(root)["repositories"][0]["files"]))
        report = contracts.build_report(self.document(root), b"source", {"Fixture": root}, [])
        self.assertEqual({r["path"] for r in report["snapshots"][0]["records"]}, {"wrapper.py", "launcher"})

    def test_inventory_digest_and_file_selection_are_enforced(self):
        root, _ = self.fixture({"source.py": 'subprocess.run(["gh", "auth", "status"])'})
        doc = self.document(root)
        doc["scanner_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            contracts.build_report(doc, b"source", {"Fixture": root}, [])
        doc = self.document(root)
        doc["repositories"][0]["files"] = []
        with self.assertRaisesRegex(ValueError, "file selection changed"):
            contracts.build_report(doc, b"source", {"Fixture": root}, [])

    def test_cli_rejects_missing_checkout_bad_pin_and_duplicate_pins(self):
        root, revision = self.fixture({"source.py": 'subprocess.run(["gh", "auth", "status"])'})
        manifest = root / "inventory.json"
        manifest.write_text(json.dumps(self.document(root)))
        output = root / "report.json"
        common = [sys.executable, str(Path(contracts.__file__)), "--inventory", str(manifest), "--output", str(output)]
        for rest in [
            [f"Other={root}"],
            ["--pin", "Fixture=HEAD", f"Fixture={root}"],
            ["--pin", f"Fixture={revision}", "--pin", f"Fixture={revision}", f"Fixture={root}"],
        ]:
            result = subprocess.run([*common, *rest], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse(output.exists())
        result = subprocess.run([*common, f"Fixture={root}"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("python", json.loads(output.read_text())["runtime"])

    def test_dependency_pin_provenance_matches_the_declaring_git_blob(self):
        target, revision = self.fixture({"source.py": 'gh_json("pr", "list")'})
        dependency = "fixture-dependency @ git+https://github.com/example/Dependency@" + revision
        source, source_revision = self.fixture(
            {"pyproject.toml": "[project]\ndependencies = [" + json.dumps(dependency) + "]\n"}
        )
        blob = contracts.inventory.git(source, "rev-parse", source_revision + ":pyproject.toml").decode().strip()
        pin = {
            "repository": "Dependency",
            "revision": revision,
            "provenance": {
                "kind": "git_dependency",
                "repository": "Fixture",
                "revision": source_revision,
                "path": "pyproject.toml",
                "blob": blob,
                "dependency": dependency,
            },
        }
        origin = contracts.pin_provenance(pin, {"Fixture": source, "Dependency": target})
        self.assertEqual(origin["validation"], "matched immutable source dependency")
        with self.assertRaisesRegex(ValueError, "does not match"):
            contracts.pin_provenance(
                {**pin, "provenance": {**pin["provenance"], "blob": "0" * 40}}, {"Fixture": source}
            )
        with self.assertRaisesRegex(ValueError, "does not match"):
            contracts.pin_provenance({**pin, "revision": "0" * 40}, {"Fixture": source})

    def test_formatted_log_messages_are_not_anonymous_queries(self):
        rows, _ = contracts.python_rows('message = f"{name} failed"\nother = f"  {n} results"')
        self.assertEqual(rows, [])
        rows, _ = contracts.python_rows('query = f"{{organization(login:{org}){{id}}}}"')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "graphql_literal")

    def test_http_wrapper_calls_keep_methods_paths_bodies_and_query_bindings(self):
        rows, _ = contracts.python_rows(
            '\nQUERY = "query{viewer{login}}"\napi.request("PATCH", f"repos/{repo}/issues/comments/{id}", data={"body": body})\napi.pages(f"repos/{repo}/pulls")\napi.graphql(QUERY)\nself.request("https://api.github.com/graphql", "POST", payload)\napi.request(method="PATCH", path=path, data=payload)\n'
        )
        calls = [r for r in rows if r["kind"] == "http_wrapper_candidate"]
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[0]["positional_arguments"][0], "PATCH")
        self.assertIn("data", calls[0]["keyword_arguments"])
        self.assertEqual(calls[2]["binding_sources"][0]["value"], "query{viewer{login}}")
        self.assertIn("method", calls[4]["keyword_arguments"])
        self.assertNotIn("argv", calls[0])

    def test_module_binding_second_argument_and_keyword_calls_link_conservatively(self):
        rows, _ = contracts.python_rows(
            '\nGH = ["gh", "api", "user"]\ndef launch(cwd):\n    run(cwd, GH)\n    run(command=GH)\n'
        )
        linked = [r for r in rows if r["kind"] == "linked_executor_candidate"]
        self.assertEqual(len(linked), 2)
        self.assertTrue(all(r["binding_sources"][0]["name"] == "GH" for r in linked))

    def test_builder_return_retains_augmented_and_reassigned_flags(self):
        rows, _ = contracts.python_rows(
            '\ndef build():\n    cmd = ["gh", "workflow", "run", "build.yml"]\n    if enabled:\n        cmd += ["--repo", repo]\n    cmd = cmd + ["--ref", branch]\n    return cmd\n'
        )
        definitions = [r for r in rows if r["kind"] == "argv_assignment_candidate"]
        self.assertEqual(len(definitions), 3)
        self.assertTrue(any(r["assignment"] == "AugAssign" and "--repo" in r["value"] for r in definitions))
        self.assertTrue(any("--ref" in r["value"] for r in definitions))

    def test_reconciliation_and_scanned_manifest_expose_unparsed_and_empty_files(self):
        root, _ = self.fixture(
            {"caller.ts": 'execFile("gh", ["pr", "list"])', "empty.py": "x = 1", "broken.py": "gh pr list\nif broken !"}
        )
        result = contracts.inspect("Fixture", root, "HEAD")
        self.assertTrue(
            any(r["kind"] == "uncovered_inventory_hit" and r["path"] == "caller.ts" for r in result["records"])
        )
        self.assertTrue(any(r["path"] == "broken.py" for r in result["records"]))
        files = {r["path"]: r for r in result["scanned_files"]}
        self.assertEqual(files["empty.py"]["records"], 0)
        self.assertEqual(files["broken.py"]["language"], "python_parse_failed_text_fallback")
        self.assertEqual(result["skipped"][0]["reason"], "python_parse_failed")

    def test_prompt_apostrophe_does_not_swallow_following_commands(self):
        rows = contracts.text_rows("Use gh pr view in the author's notes.\ngh pr list", prompt=True)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["end_line"], 1)
        self.assertEqual(rows[1]["line"], 2)

    def test_runtime_receipt_is_timestamped_bound_and_never_verifies_running_processes(self):
        revision = "a" * 40
        observation = {
            "observed_at": "2026-11-07T04:05:43+00:00",
            "target": "fixture marker",
            "field": "revision",
            "revision": revision,
            "running_processes_verified": False,
            "parent_observation_sha256": "b" * 64,
        }
        provenance = {
            "kind": "runtime_marker",
            "observation": observation,
            "observation_sha256": hashlib.sha256(
                json.dumps(observation, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
        pin = {"repository": "Fixture", "revision": revision, "provenance": provenance}
        result = contracts.pin_provenance(pin, {})
        self.assertIn("running processes not verified", result["validation"])
        for altered in [
            {**provenance, "running_processes_verified": True},
            {**provenance, "observation": {**observation, "running_processes_verified": True}},
            {**provenance, "observation_sha256": "0" * 64},
        ]:
            with self.assertRaises(ValueError):
                contracts.pin_provenance({**pin, "provenance": altered}, {})

    def test_dependency_provenance_must_reference_an_inspected_source_pin(self):
        root, revision = self.fixture({"source.py": 'gh_json("pr", "list")'})
        pin = {
            "repository": "Fixture",
            "revision": revision,
            "provenance": {"kind": "git_dependency", "repository": "Other", "revision": "b" * 40},
        }
        with self.assertRaisesRegex(ValueError, "not bound to an inspected source pin"):
            contracts.build_report(self.document(root), b"input", {"Fixture": root}, [pin])

    def test_saved_report_binds_current_scanners_inputs_and_unique_occurrences(self):
        evidence = REPO / "docs" / "evidence"
        report = json.loads((evidence / "forgejo-contract-candidates-20261107.json").read_text())
        for key, path in [
            ("scanner_sha256", REPO / "scripts/forge-contracts.py"),
            ("inventory_scanner_sha256", REPO / "scripts/forge-inventory.py"),
            ("inventory_sha256", evidence / "forgejo-integration-inventory-20261107.json"),
            ("pin_manifest_sha256", evidence / "forgejo-contract-source-pins-20261107.json"),
        ]:
            self.assertEqual(report[key], hashlib.sha256(path.read_bytes()).hexdigest(), key)
        ids = [row["id"] for snapshot in report["snapshots"] for row in snapshot["records"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all("scanned_files" in snapshot for snapshot in report["snapshots"]))

    def test_local_wrapper_chain_and_workflow_dispatch_call_sites_are_retained(self):
        rows, _ = contracts.python_rows(
            '\nclass Backend:\n    def command(self, *args):\n        return subprocess.run(["gh", *args])\n    def read(self, path):\n        return self.command("api", path)\n    def get_json(self, path):\n        return self.read(path)\n    def collect(self):\n        self.command("workflow", "run", name, f"--repo={self.repo}")\n        self.read(f"repos/{self.repo}/pulls")\n        self.get_json(f"repos/{self.repo}/issues")\n        self.collect()\n'
        )
        calls = {r.get("callee"): r for r in rows if r.get("line", 0) >= 10 and "callee" in r}
        self.assertIn("self.command", calls)
        self.assertIn("self.read", calls)
        self.assertIn("self.get_json", calls)
        self.assertIn("self.collect", calls)
        self.assertTrue(calls["self.command"]["wrapper_definitions"])
        self.assertEqual(calls["self.command"]["positional_arguments"][:2], ["workflow", "run"])

    def test_plain_api_helpers_and_unknown_command_group_callers_are_candidates(self):
        rows, _ = contracts.python_rows(
            '\ndef api(path):\n    return subprocess.check_output(["gh", "api", path])\napi(f"repos/{repo}/compare/main...head")\nsend("workflow", "run", name)\nunknown_get(f"repos/{repo}/pulls")\n'
        )
        self.assertTrue(any(r.get("callee") == "api" and r["kind"] == "http_wrapper_candidate" for r in rows))
        self.assertTrue(any(r.get("callee") == "send" and r["kind"] == "command_group_candidate" for r in rows))
        self.assertTrue(any(r.get("callee") == "unknown_get" and r["kind"] == "endpoint_call_candidate" for r in rows))

    def test_unmatched_outer_quotes_never_hide_later_commands_or_reconciliation(self):
        source = 'exec("gh pr list")\nexec("gh issue list")\nconst q = `query{viewer{login}}`;\nexecFile("gh", ["auth","status"])'
        rows = contracts.text_rows(source)
        self.assertTrue(any(r["line"] == 2 and r["kind"] == "shell_candidate" for r in rows))
        self.assertTrue(any(r["line"] == 3 and r["kind"] == "graphql_text_candidate" for r in rows))
        root, _ = self.fixture({"caller.ts": source})
        report = contracts.inspect("Fixture", root, "HEAD")
        self.assertTrue(any(r["line"] == 4 and r["kind"] == "uncovered_inventory_hit" for r in report["records"]))

    def test_source_pins_reject_annotated_tag_object_ids(self):
        root, revision = self.fixture({"source.py": 'gh_json("pr","list")'})
        tag = (
            subprocess.check_output(
                ["git", "-C", str(root), "mktag"],
                input=(
                    f"object {revision}\ntype commit\ntag fixture\ntagger Fixture <fixture@example.invalid> 0 +0000\n\nFixture\n"
                ).encode(),
            )
            .decode()
            .strip()
        )
        with self.assertRaisesRegex(ValueError, "commit object, not a tag"):
            contracts.build_report(
                self.document(root),
                b"input",
                {"Fixture": root},
                [{"repository": "Fixture", "revision": tag, "provenance": {"kind": "operator_argument"}}],
            )

    def test_invalid_python_records_omission(self):
        rows, skipped = contracts.python_rows("if broken !")
        self.assertEqual(rows, [])
        self.assertEqual(skipped[0]["reason"], "python_parse_failed")

    def test_immutable_git_replay_never_imports_source_or_reads_untracked_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(*args):
                return subprocess.check_output(["git", "-C", tmp, *args])

            git("init", "-q")
            source = root / "source.py"
            source.write_text('raise RuntimeError("must not import source")\nsubprocess.run(["gh", "auth", "token"])\n')
            git("add", ".")
            git(
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                f"core.hooksPath={os.devnull}",
                "commit",
                "-qm",
                "Fixture",
            )
            first = contracts.inspect("Fixture", root, "HEAD")
            source.write_text('subprocess.run(["gh", "repo", "delete", "danger"])\n')
            (root / "untracked-token").write_text("private credential must not be inspected")
            replay = contracts.inspect("Fixture", root, first["source_revision"])
            self.assertEqual(first, replay)
            self.assertEqual(first["records"][0]["argv"], ["gh", "auth", "token"])
            self.assertEqual({r["kind"] for r in first["records"]}, {"direct_argv", "argv_literal"})
            self.assertEqual(len(first["records"][0]["blob"]), 40)

    def test_shell_continuations_and_prompt_boundary_are_candidates(self):
        rows = contracts.text_rows('gh pr list \\\n  --json number\ngh api "unterminated', prompt=True)
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["line"], rows[0]["end_line"]), (1, 2))
        self.assertEqual(rows[0]["tokens"], ["gh", "pr", "list", "--json", "number"])
        self.assertIsNone(rows[1]["tokens"])
        self.assertEqual(rows[0]["kind"], "prompt_candidate")


if __name__ == "__main__":
    unittest.main()
