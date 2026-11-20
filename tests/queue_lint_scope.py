"""The worker's environment-lint scope (``tauceti_queue/lint_scope.py``): the changed closure since a
verified pass, or the full library whenever that scope cannot be trusted."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_queue import lint_scope

GIT_ENV = {
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


def lean(*imports: str, body: str = "theorem t : True := trivial\n") -> str:
    return "".join(f"import {m}\n" for m in imports) + "\n" + body


# A ← B ← C, D independent, E imports D: the library's import graph.
LIBRARY = {
    "TauCeti/A.lean": lean("Mathlib.Logic.Basic"),
    "TauCeti/B.lean": lean("TauCeti.A"),
    "TauCeti/Sub/C.lean": lean("TauCeti.B"),
    "TauCeti/D.lean": lean(),
    "TauCeti/E.lean": lean("TauCeti.D"),
    "README.md": "library\n",
    "lean-toolchain": "leanprover/lean4:v4.0.0\n",
}


class HeaderTest(unittest.TestCase):
    def test_reads_module_system_headers_with_comments(self):
        text = (
            "/- Copyright -/\nmodule -- the module system\n\npublic import TauCeti.A\n"
            "public meta import TauCeti.B\nimport all TauCeti.C /- inline -/\nmeta import Mathlib.X\n"
            "\n/-! # Doc\n\nimport inside the header's comments is skipped\n-/\n\npublic section\n"
            "\n/--\nimport at the start of a line in a later comment -/\ntheorem t : True := trivial\n"
        )
        self.assertEqual(lint_scope.header_imports(text), ["TauCeti.A", "TauCeti.B", "TauCeti.C", "Mathlib.X"])
        # Fail closed: an import line outside comments after the header means it was misread.
        self.assertIsNone(lint_scope.header_imports(text + "import TauCeti.D\n"))
        self.assertIsNone(lint_scope.header_imports(text + "-- a stray /- opener\nimport\n"))

    def test_nested_comments_and_prelude(self):
        self.assertEqual(
            lint_scope.header_imports("prelude\n/- a /- nested -/ still -/ import TauCeti.A\ndef x := 1\n"),
            ["TauCeti.A"],
        )
        self.assertEqual(lint_scope.header_imports("theorem t : True := trivial\n"), [])

    def test_unreadable_headers_are_none(self):
        for text in (
            "/- unterminated\nimport TauCeti.A\n",
            "import «TauCeti».A\n",
            "import\n",
            "import TauCeti.A\ndef x := 1\nimport TauCeti.B\n",
            "import TauCeti.A@[inline] def f (n : Nat) := n\n",
        ):
            with self.subTest(text=text):
                self.assertIsNone(lint_scope.header_imports(text))


class ScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lint-scope-"))
        self.repo = self.tmp / "content"
        self.repo.mkdir()
        git(self.repo, "init", "--quiet")
        self.passes = self.tmp / "tools" / lint_scope.PASSES_FILE
        self.verified = self.commit(LIBRARY, "verified")

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)], check=False)

    def commit(self, files: dict[str, str | None], message: str) -> str:
        for name, text in files.items():
            path = self.repo / name
            if text is None:
                git(self.repo, "rm", "--quiet", name)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            git(self.repo, "add", name)
        git(self.repo, "commit", "--quiet", "--allow-empty", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def choose(self, key="k"):
        return lint_scope.choose(self.repo, head=lint_scope.snapshot(self.repo), passes=self.passes, key=key)

    def record(self, commit=None, key="k"):
        self.assertTrue(lint_scope.record_pass(self.passes, self.repo, commit or self.verified, key, "full"))

    def test_without_a_recorded_pass_the_lint_is_full(self):
        scope = self.choose()
        self.assertEqual(scope["scope"], "full")
        self.assertIn("no verified lint pass", scope["reason"])

    def test_the_changed_module_and_its_transitive_importers(self):
        self.record()
        self.commit({"TauCeti/A.lean": lean("Mathlib.Logic.Basic", body="theorem t' : True := trivial\n")}, "A")
        scope = self.choose()
        self.assertEqual(scope["scope"], "changed", scope)
        self.assertEqual(scope["reference"], self.verified)
        self.assertEqual(scope["modules"], ["TauCeti.A", "TauCeti.B", "TauCeti.Sub.C"])
        self.assertEqual(scope["changed_modules"], 1)

    def test_an_unchanged_tree_has_an_empty_scope(self):
        self.record()
        scope = self.choose()
        self.assertEqual((scope["scope"], scope["modules"]), ("changed", []))

    def test_a_changed_attribute_line_means_the_full_lint(self):
        # `attribute [..]` can set an instance, deprecated or nolint attribute on a declaration of a
        # module outside the closure; so can `to_additive existing` and `add_decl_doc`.
        self.record()
        for line in ("attribute [instance] TauCeti.t", "@[to_additive existing]", "add_decl_doc TauCeti.t"):
            with self.subTest(line):
                git(self.repo, "checkout", "--quiet", "--detach", self.verified)
                self.commit({"TauCeti/E.lean": lean("TauCeti.D", body=line + "\n")}, line)
                scope = self.choose()
                self.assertEqual(scope["scope"], "full")
                self.assertIn("TauCeti/E.lean may set another module's attribute", scope["reason"])
        git(self.repo, "checkout", "--quiet", "--detach", self.verified)
        self.commit({"TauCeti/E.lean": None}, "a deleted file's attribute line counts too")
        self.assertEqual(self.choose()["scope"], "changed")
        self.commit({"TauCeti/D.lean": lean(body="-- attribute [simp] foo\n")}, "removed again")
        self.assertEqual(self.choose()["scope"], "full")

    def test_an_edit_to_a_multi_line_attribute_command_means_the_full_lint(self):
        # Only the target line changes; the command's first line is unchanged context.
        self.commit({"TauCeti/E.lean": lean("TauCeti.D", body="attribute [deprecated]\n  TauCeti.t\n")}, "E")
        self.record(git(self.repo, "rev-parse", "HEAD"))
        self.commit({"TauCeti/E.lean": lean("TauCeti.D", body="attribute [deprecated]\n  TauCeti.u\n")}, "E'")
        self.assertIn("may set another module's attribute", self.choose()["reason"])

    def test_an_ignored_lean_file_means_the_full_lint(self):
        # lint-env.sh lints every Lean file on disk; a clean status does not show an ignored one.
        self.commit({".gitignore": "__pycache__/\n"}, "ignore")
        self.record(git(self.repo, "rev-parse", "HEAD"))
        hidden = self.repo / "TauCeti" / "__pycache__" / "Hidden.lean"
        hidden.parent.mkdir()
        hidden.write_text(lean())
        self.assertIn("an untracked Lean file: TauCeti/__pycache__/Hidden.lean", self.choose()["reason"])

    def test_a_new_import_edge_is_followed(self):
        self.record()
        self.commit({"TauCeti/E.lean": lean("TauCeti.D", "TauCeti.Sub.C")}, "E imports C")
        self.commit({"TauCeti/B.lean": lean("TauCeti.A", body="theorem b : True := trivial\n")}, "B")
        self.assertEqual(self.choose()["modules"], ["TauCeti.B", "TauCeti.E", "TauCeti.Sub.C"])

    def test_deleted_and_added_modules(self):
        self.record()
        self.commit({"TauCeti/D.lean": None, "TauCeti/E.lean": lean(), "TauCeti/F.lean": lean("TauCeti.E")}, "drop D")
        self.assertEqual(self.choose()["modules"], ["TauCeti.E", "TauCeti.F"])

    def test_the_reference_with_the_smallest_change_is_chosen(self):
        self.record()
        middle = self.commit({"TauCeti/D.lean": lean(body="theorem d : True := trivial\n")}, "D")
        self.record(middle)
        self.commit({"TauCeti/A.lean": lean("Mathlib.Logic.Basic", body="theorem a : True := trivial\n")}, "A")
        scope = self.choose()
        self.assertEqual(scope["reference"], middle)
        self.assertEqual(scope["modules"], ["TauCeti.A", "TauCeti.B", "TauCeti.Sub.C"])

    def test_full_lint_fallbacks(self):
        self.record()
        cases = {
            "pins": ({"lean-toolchain": "leanprover/lean4:v4.1.0\n"}, "lean-toolchain changed"),
            "scripts": ({"scripts/lint-baseline.txt": "x\n"}, "scripts/lint-baseline.txt changed"),
            "lean outside": ({"Other/X.lean": lean()}, "Other/X.lean changed"),
        }
        for name, (files, reason) in cases.items():
            with self.subTest(name):
                git(self.repo, "checkout", "--quiet", "--detach", self.verified)
                self.commit(files, name)
                scope = self.choose()
                self.assertEqual(scope["scope"], "full")
                self.assertIn(reason, scope["reason"])

    def test_documentation_changes_keep_the_scope(self):
        self.record()
        self.commit({"README.md": "changed\n", "docs/notes.txt": "x\n"}, "docs")
        self.assertEqual(self.choose()["scope"], "changed")

    def test_another_key_or_a_missing_commit_is_not_a_reference(self):
        self.record(key="other")
        self.assertEqual(self.choose()["scope"], "full")
        with self.passes.open("a") as handle:
            handle.write(json.dumps({"commit": "0" * 40, "tree": "1" * 40, "key": "k"}) + "\nnot json\n")
        self.assertIn("no verified lint pass", self.choose()["reason"])

    def test_uncommitted_changes_unreadable_headers_and_whole_library(self):
        self.record()
        (self.repo / "TauCeti" / "D.lean").write_text(lean(body="-- edited\n"))
        self.assertIn("uncommitted", self.choose()["reason"])
        self.commit({"TauCeti/D.lean": "/- never closed\n"}, "unterminated")
        self.assertIn("could not be read", self.choose()["reason"])
        git(self.repo, "checkout", "--quiet", "--detach", self.verified)
        self.commit({"TauCeti/A.lean": lean(body="theorem z : True := trivial\n")}, "A")
        self.commit({"TauCeti/D.lean": lean(body="theorem z : True := trivial\n")}, "D")
        scope = self.choose()
        self.assertEqual(scope["scope"], "full")
        self.assertIn("whole library", scope["reason"])

    def test_disabled_by_the_environment(self):
        self.record()
        os.environ[lint_scope.ENABLE_VARIABLE] = "0"
        self.addCleanup(os.environ.pop, lint_scope.ENABLE_VARIABLE, None)
        self.assertEqual(self.choose()["reason"], f"{lint_scope.ENABLE_VARIABLE}=0")

    def test_the_passes_file_is_compacted(self):
        for _ in range(lint_scope.COMPACT_AT + 1):
            self.record()
        self.assertLessEqual(len(self.passes.read_text().splitlines()), lint_scope.KEEP + 1)
        self.assertEqual(self.choose()["scope"], "changed")

    def test_the_lint_key_covers_every_materialized_file(self):
        scripts = self.tmp / "scripts"
        (scripts / "sub").mkdir(parents=True)
        (scripts / "lint-env.sh").write_text("a")
        (scripts / "sub" / "lint-baseline.txt").write_text("b")
        first = lint_scope.lint_key("driver", scripts)
        self.assertEqual(first, lint_scope.lint_key("driver", scripts))
        self.assertNotEqual(first, lint_scope.lint_key("other driver", scripts))
        (scripts / "sub" / "lint-baseline.txt").write_text("c")
        self.assertNotEqual(first, lint_scope.lint_key("driver", scripts))


TAG = "/w/.lake/GlobalLint-0123.lean"
MARKER = "LINTENV-GLOBAL-COMPLETE-0123"
LINTERS = ["simpNF", "simpComm"]
CLEAN = f"-- Found 0 errors in 10 declarations (plus 2 automatically generated ones) in TauCeti with 2 linters\n\n{MARKER}\n"
FAILED = (
    'LINT-ENV-ORIGINS {"version": 1, "records": [{"linter": "simpNF", "declaration": "TauCeti.a",'
    ' "report_name": "TauCeti.a", "module": "TauCeti.Sub.C", "line": 7, "column": 3}]}\n'
    f"{TAG}:1:0: error: -- Found 2 errors in 10 declarations (plus 0 automatically generated ones) in TauCeti"
    " with 2 linters\n\n"
    "/- The `simpNF` linter reports:\nSOME SIMP LEMMAS ARE NOT IN SIMP-NORMAL FORM. -/\n"
    "#check @TauCeti.a /- Left-hand side simplifies from\n  x\nto\n  y -/\n"
    "#check TauCeti.b /- simp can prove this -/\n"
    f"{MARKER}\n"
)


class ReportTest(unittest.TestCase):
    def parse(self, output, rc):
        return lint_scope.parse_report(output, tag=TAG, marker=MARKER, linters=LINTERS, rc=rc)

    def test_clean_and_failed_reports(self):
        self.assertEqual(self.parse(CLEAN, 0), set())
        violations = self.parse(FAILED, 1)
        self.assertEqual(violations, {("simpNF", "TauCeti.a"), ("simpNF", "TauCeti.b")})
        self.assertEqual(
            lint_scope.locations(FAILED, violations)["findings"],
            [
                {"linter": "simpNF", "declaration": "TauCeti.a", "path": "TauCeti/Sub/C.lean", "line": 7, "column": 3},
                {"linter": "simpNF", "declaration": "TauCeti.b", "unknown": "unlocated"},
            ],
        )

    def test_malformed_reports_are_rejected(self):
        forged_section = FAILED.replace("#check TauCeti.b", "/- The `simpNF` linter reports:\n#check TauCeti.b")
        for name, output, rc in (
            ("no header", MARKER + "\n", 0),
            ("no marker", CLEAN.replace(MARKER, ""), 0),
            ("exit code", CLEAN, 1),
            ("unanchored errors", FAILED.replace(TAG + ":1:0: error: ", ""), 1),
            ("error exit 0", FAILED, 0),
            ("count", FAILED.replace("Found 2 errors", "Found 3 errors"), 1),
            ("linter count", CLEAN.replace("with 2 linters", "with 3 linters"), 0),
            ("duplicate section", forged_section, 1),
            ("unknown section", FAILED.replace("`simpNF` linter", "`docBlame` linter"), 1),
            ("two headers", CLEAN + CLEAN, 0),
            ("unclosed block", FAILED.replace("simp can prove this -/", "simp can prove this"), 1),
            ("greedy section name", FAILED.replace("`simpNF` linter", "`bogus`x` linter"), 1),
            (
                "non-ASCII space after a block",
                FAILED.replace("simp can prove this -/", "simp can prove this -/\u00a0"),
                1,
            ),
        ):
            with self.subTest(name), self.assertRaises(lint_scope.ReportError):
                self.parse(output, rc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
