#!/usr/bin/env python3
"""Exercise native worker scheduling, gh routing and retained publication on stock Forgejo."""

import base64
import dataclasses
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from forgejo_git_source import source_fixture
from runner import cli as review_cli
from runner import publication as shared_publication
from tauceti_tools.ci_candidate import RepositoryIdentity

from tauceti_worker.config import Config
from tauceti_worker.forgejo_worker import ForgejoWorkerSource
from tauceti_worker.review_state import ReviewState
from tauceti_worker.source_publication import pending


def retain_plan(source, cfg, number, head, path, *, actor_id):
    assert json.loads(path.read_text())["head_sha"] == head
    return shared_publication.retain(
        source.repository.key, number, path, cfg.store, actor=source.repository.forge.key + "/users/" + str(actor_id)
    )


def publish(source, path):
    value = json.loads(path.read_text())
    status = shared_publication.publish(source.repository.key, path, Path(value["ledger"]))
    if status == 0:
        source.invalidate(value["pr"])
    return status == 0


from tauceti_worker.survey import Counters, survey
from tauceti_worker.worker_sources import activate, source_config


@unittest.skipUnless(os.environ.get("FORGEJO_BINARY"), "set FORGEJO_BINARY for disposable native worker")
class NativeWorker(unittest.TestCase):
    def test_native_branch_pr_schedule_publication_and_recovery(self):
        engine = Path(os.environ.get("TAUCETI_REVIEW_ENGINE_DIR") or Path(review_cli.__file__).resolve().parent.parent)
        self.assertTrue(
            (engine / "runner/cli.py").is_file() and (engine / "rubrics").is_dir(),
            "Set TAUCETI_REVIEW_ENGINE_DIR to a Review checkout when using an installed Review wheel",
        )
        with source_fixture() as (root, api, repository, row, tokens):
            from tauceti_tools.forge import Forgejo

            grant = api.request(
                "POST",
                "admin/users/operator/tokens",
                {
                    "name": "native-worker",
                    "scopes": ["read:user", "write:repository", "write:issue"],
                },
            )
            api = Forgejo(api.forge, token=grant["sha1"], timeout=5)
            path = root / "worker-token"
            path.write_text(grant["sha1"] + "\n")
            path.chmod(0o600)
            source = ForgejoWorkerSource(RepositoryIdentity(repository, row["id"]), api, token_file=path)
            cfg = Config.resolve("native-worker", home=root)
            cfg = dataclasses.replace(
                cfg,
                state=root / "state",
                checkout=root / "checkout",
                store_dir=root / "store",
                logdir=root / "logs",
                sbcache=root / "cache",
            )
            cfg = source_config(cfg, source)
            endpoint = "repos/" + repository.name
            api.request("POST", endpoint + "/branches", {"old_branch_name": "main", "new_branch_name": "proposal"})
            api.request(
                "POST",
                endpoint + "/contents/Proof.lean",
                {
                    "branch": "proposal",
                    "message": "Proposal",
                    "content": base64.b64encode(b"theorem example_proof : True := trivial\n").decode(),
                },
            )
            with (
                activate(source, cfg),
                patch.object(review_cli, "CACHE_DIR", root / "review-cache"),
                patch.dict(os.environ, {"TAUCETI_REVIEW_ENGINE_DIR": str(engine)}),
            ):
                encoded = base64.b64encode(("x-access-token:" + grant["sha1"]).encode()).decode()
                self.assertFalse(any(grant["sha1"] in value or encoded in value for value in os.environ.values()))
                # Exercise an actual worker-authored Git commit and safe push;
                # API file writes do not verify process-local commit attribution.
                checkout = root / "author-checkout"
                subprocess.run(
                    ["git", "clone", "--quiet", "--branch", "proposal", repository.clone_url, str(checkout)], check=True
                )
                (checkout / "native-author.txt").write_text("Authored by the native worker.\n")
                subprocess.run(["git", "-C", str(checkout), "add", "native-author.txt"], check=True)
                subprocess.run(
                    ["git", "-C", str(checkout), "commit", "--quiet", "-m", "Native worker authors"], check=True
                )
                self.assertEqual(
                    subprocess.check_output(
                        ["git", "-C", str(checkout), "show", "-s", "--format=%an"], text=True
                    ).strip(),
                    "operator",
                )
                original_head = api.request("GET", endpoint + "/branches/proposal")["commit"]["id"]
                with patch.dict(os.environ, {"TAUCETI_PUSH_EXPECT": original_head}):
                    subprocess.run(
                        [str(Path(__file__).resolve().parents[1] / "scripts/git-safe-push"), "proposal"],
                        cwd=checkout,
                        check=True,
                        capture_output=True,
                    )
                result = source._gh(
                    [
                        "pr",
                        "create",
                        "--repo",
                        repository.name,
                        "--base",
                        "main",
                        "--head",
                        "operator:proposal",
                        "--title",
                        "Native worker fixture",
                        "--body",
                        "A proof.",
                    ]
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                pulls = api.pulls(repository, state="open")
                self.assertEqual(len(pulls), 1)
                pr = pulls[0]["number"]
                head = pulls[0]["head"]["sha"]
                rs, counters = ReviewState(cfg, source), Counters(cfg)
                sv = survey(
                    cfg, source, rs, counters, deep=True, stages=["review", "fix", "rebase"], selected_roadmap="any"
                )
                self.assertFalse(sv.github_failed, sv.errors)
                self.assertIsNone(sv.dependency_wait)
                self.assertEqual([candidate.pr for candidate in sv.reviewable.actionable], [pr])
                candidate = sv.reviewable.actionable[0]
                self.assertEqual(candidate.source_repository, repository)
                self.assertEqual(candidate.source_repository_id, row["id"])
                self.assertFalse(sv.open_prs[0].build_success)
                from types import SimpleNamespace

                from tauceti_worker.work_units import _still_actionable

                worker = SimpleNamespace(cfg=cfg, gh=source, rs=rs, counters=counters)
                self.assertTrue(_still_actionable("review", worker, sv, candidate))

                work = cfg.state / "review-work/test"
                work.mkdir(parents=True)
                from tauceti_worker.source_claims import lease

                _, base = review_cli.pr_ref_oids(repository.key, pr)
                comparison = review_cli.merge_base_sha(repository.key, base, head, pr)
                review_cli.prepare_review_source(repository.key, head, comparison, work)
                self.assertIsNotNone(comparison)
                first_claim = lease(source, cfg, f"review/{pr}/{head}")
                peer_cfg = dataclasses.replace(cfg, wid="other-worker")
                peer_claim = lease(source, peer_cfg, f"review/{pr}/{head}")
                self.assertEqual(first_claim.acquire(), 0)
                self.assertEqual(peer_claim.acquire(), 1)
                self.assertEqual(first_claim.release(), 0)
                cfg.store_dir.mkdir(parents=True)
                cfg.store.write_text(
                    json.dumps(
                        {
                            "prs": {
                                str(pr): {
                                    "pending_publication_head_sha": head,
                                    "state": {"style": {"pending_thread_run_id": "fixture-run"}},
                                }
                            }
                        }
                    )
                )
                meta = {"head_sha": head, "mode": "commit", "states": {"style": "blocking_request"}}
                (work / "scoreboard.md").write_text(
                    "<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 " + json.dumps(meta) + "-->\nChanges requested."
                )
                (work / "style.md").write_text("<!--tauceti-rubric:style-->\nExplain the declaration.")
                plan = {
                    "head_sha": head,
                    "round": 1,
                    "scoreboard_body": str(work / "scoreboard.md"),
                    "threads": [
                        {
                            "action": "upsert",
                            "required": True,
                            "rubric": "style",
                            "run_id": "fixture-run",
                            "path": "Proof.lean",
                            "body": str(work / "style.md"),
                        }
                    ],
                }
                (work / "plan.json").write_text(json.dumps(plan))
                actor_id = int(source.authenticated_actor().principal.native_id)
                context = {
                    "source": source.identity.record(),
                    "pr": pr,
                    "head": head,
                    "actor_id": actor_id,
                    "lifecycle": 2,
                    "store": str(cfg.store),
                }
                (work / "review-context.json").write_text(json.dumps(context))
                # Main may advance while inference is running. Anchor against
                # the retained comparison, without another fallible remote read.
                api.request(
                    "POST",
                    endpoint + "/contents/main-note.txt",
                    {
                        "branch": "main",
                        "message": "Advance main during inference",
                        "content": base64.b64encode(b"Main advanced.\n").decode(),
                    },
                )
                with patch.object(
                    source, "authenticated_actor", side_effect=AssertionError("post-inference actor read")
                ):
                    publication = retain_plan(source, cfg, pr, head, work / "plan.json", actor_id=actor_id)
                self.assertFalse((work / "code").exists())
                self.assertTrue(publish(source, publication))
                published_state = json.loads(cfg.store.read_text())["prs"][str(pr)]
                self.assertEqual(published_state["published_head_sha"], head)
                self.assertNotIn("pending_publication_head_sha", published_state)
                self.assertNotIn("pending_thread_run_id", published_state["state"]["style"])
                roots = source.review_comments(pr)
                self.assertEqual(len(roots), 1)
                root = roots[0]
                # A lost acknowledgement leaves a prepared intent. Reconciliation adopts
                # the exact native marker and must not duplicate roots or the scoreboard.
                intent = json.loads(publication.read_text())
                intent["state"] = "prepared"
                publication.write_text(json.dumps(intent))
                self.assertTrue(publish(source, publication))
                self.assertEqual(len(source.review_comments(pr)), 1)
                self.assertEqual(len(source.issue_comments(pr, fresh=True)), 1)
                publication.unlink()
                self.assertTrue(pending(source, cfg))
                self.assertEqual(len(source.review_comments(pr)), 1)
                intent = json.loads(publication.read_text())
                intent["state"] = "prepared"
                intent["threads"][0].update(
                    comment_id=root["id"],
                    run_id="second-run",
                    body="<!--tauceti-rubric:style-->\nExplain this revised finding.",
                )
                publication.write_text(json.dumps(intent))
                self.assertTrue(publish(source, publication))
                updated = source.review_comments(pr, fresh=True)
                self.assertEqual(len(updated), 1)
                self.assertEqual(updated[0]["id"], root["id"])
                self.assertIn("revised finding", updated[0]["body"])
                # A short unchanged engine result has no scoreboard body.
                unchanged = cfg.state / "review-work/unchanged"
                unchanged.mkdir()
                (unchanged / "plan.json").write_text(
                    json.dumps({"head_sha": head, "review_changed": False, "threads": []})
                )
                (unchanged / "review-context.json").write_text(json.dumps(context))
                self.assertFalse(pending(source, cfg))
                rs.bust(pr)
                fixed = survey(cfg, source, rs, counters, deep=True, stages=["fix"], selected_roadmap="any")
                self.assertEqual([candidate.pr for candidate in fixed.needs_fix.actionable], [pr], fixed.errors)
                self.assertTrue(_still_actionable("fix", worker, fixed, fixed.needs_fix.actionable[0]))

                api.request(
                    "POST",
                    f"{endpoint}/pulls/{pr}/reviews/{root['pull_request_review_id']}/comments",
                    {
                        "body": "Native UI reply, without private markers.",
                        "path": root["path"],
                        "new_position": root.get("position", 0),
                        "old_position": root.get("original_position", 0),
                    },
                )
                ui = source.review_comments(pr, fresh=True)
                self.assertEqual(
                    next(item for item in ui if item["body"].startswith("Native UI"))["in_reply_to_id"], root["id"]
                )
                reply = source._gh(
                    [
                        "api",
                        "-X",
                        "POST",
                        f"repos/{repository.name}/pulls/{pr}/comments/{root['id']}/replies",
                        "-f",
                        "body=Here is the reasoning.",
                    ]
                )
                self.assertEqual(reply.returncode, 0, reply.stderr)
                self.assertIn("tauceti-source-reply:root:", reply.stdout)
                rs.bust(pr)
                contested = survey(
                    cfg, source, rs, counters, deep=True, stages=["fix", "review"], selected_roadmap="any"
                )
                self.assertFalse(contested.needs_fix.actionable)
                self.assertEqual(contested.reviewable.actionable[0].contest, "style")
                check = source._gh(["pr", "view", str(pr), "--repo", repository.name, "--json", "number,headRefOid"])
                self.assertEqual(check.returncode, 0, check.stderr)
                self.assertEqual(json.loads(check.stdout)["headRefOid"], head)

                # The scoreboard is authoritative; direct answers and close
                # notes use native threads as the engine's best-effort UI actions.
                intent = json.loads(publication.read_text())
                intent["state"] = "prepared"
                intent["threads"] = [
                    {
                        "action": "reply",
                        "required": False,
                        "rubric": "style",
                        "in_reply_to": root["id"],
                        "reply_dedupe": 100,
                        "body": "The reviewer has considered this contest.",
                    }
                ]
                publication.write_text(json.dumps(intent))
                self.assertTrue(publish(source, publication))
                self.assertTrue(
                    any("considered this contest" in item["body"] for item in source.review_comments(pr, fresh=True))
                )
                intent["state"] = "prepared"
                intent["threads"] = [
                    {
                        "action": "close",
                        "required": False,
                        "rubric": "style",
                        "comment_id": root["id"],
                        "body": "<!--tauceti-rubric:style-->\nNow passing.",
                    }
                ]
                publication.write_text(json.dumps(intent))
                self.assertTrue(publish(source, publication))
                self.assertIn(
                    "Now passing",
                    next(item for item in source.review_comments(pr, fresh=True) if item["id"] == root["id"])["body"],
                )

                # A binary or empty-file finding has no changed line. Keep a
                # real native conversation root with the same publication checks.
                intent = json.loads(publication.read_text())
                intent["state"] = "prepared"
                intent["threads"] = [
                    {
                        "action": "upsert",
                        "required": True,
                        "rubric": "style",
                        "conversation": True,
                        "run_id": "binary-fixture",
                        "body": "<!--tauceti-rubric:style-->\nBinary finding.",
                    }
                ]
                publication.write_text(json.dumps(intent))
                self.assertTrue(publish(source, publication))
                conversation = next(
                    row for row in source.review_comments(pr, fresh=True) if row.get("thread_kind") == "issue-comment"
                )
                reply = source._gh(
                    [
                        "api",
                        "-X",
                        "POST",
                        f"repos/{repository.name}/pulls/{pr}/comments/{conversation['id']}/replies",
                        "-f",
                        "body=This file has no changed text lines.",
                    ]
                )
                self.assertEqual(reply.returncode, 0, reply.stderr)

                # A retained result for an older head can be archived, but its
                # scoreboard cannot become the verdict for a newer revision.
                prior_count = len(source.issue_comments(pr, fresh=True))
                content = api.request("GET", endpoint + "/contents/Proof.lean?ref=proposal")
                api.request(
                    "PUT",
                    endpoint + "/contents/Proof.lean",
                    {
                        "branch": "proposal",
                        "sha": content["sha"],
                        "message": "Advance proposal",
                        "content": base64.b64encode(b"theorem example_proof : True := by trivial\n").decode(),
                    },
                )
                intent["state"] = "prepared"
                publication.write_text(json.dumps(intent))
                self.assertFalse(publish(source, publication))
                self.assertEqual(json.loads(publication.read_text())["state"], "superseded")
                self.assertEqual(len(source.issue_comments(pr, fresh=True)), prior_count)


if __name__ == "__main__":
    unittest.main()
