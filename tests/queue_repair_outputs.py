#!/usr/bin/env python3
"""A reused repair tree's outputs that differ from their traces are found, discarded and never published."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_queue import repair_outputs, repair_publish


def fixture_hash(data: bytes) -> str:
    return f"{(len(data) * 2654435761 + sum(data)) % (1 << 64):016x}"


def hasher(paths):
    return {str(p): fixture_hash(p.read_bytes()) for p in paths}


class Outputs(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.lib = self.root / ".lake/build/lib/lean/TauCeti/Flat"
        self.ir = self.root / ".lake/build/ir/TauCeti/Flat"
        self.artifacts = self.root / ".lake/cache/artifacts"
        self.records = self.root / ".lake/cache/outputs/TauCeti"
        for directory in (self.lib, self.ir, self.artifacts, self.records):
            directory.mkdir(parents=True)

    def module(self, name, dep_hash, seed):
        names = {}
        for extension in ("olean", "olean.server", "olean.private", "ilean", "ir.sig", "ir", "c"):
            data = (seed + extension).encode() * 40
            names[extension] = fixture_hash(data) + "." + extension
            ((self.ir if extension == "c" else self.lib) / f"{name}.{extension}").write_bytes(data)
        outputs = {
            "m": True,
            "o": [names[e] for e in ("olean", "olean.server", "olean.private")],
            "i": names["ilean"],
            "rs": names["ir.sig"],
            "r": names["ir"],
            "c": names["c"],
        }
        (self.lib / f"{name}.trace").write_text(json.dumps({"depHash": dep_hash, "outputs": outputs}))
        (self.records / f"{dep_hash}.json").write_text("{}")
        return names

    def test_intact_tree_has_nothing_to_discard(self):
        self.module("Basic", "4366fd23170e9e25", "a")
        self.assertEqual(repair_outputs.damaged(self.root, {}, hasher=hasher), [])
        result = repair_outputs.discard_damaged(self.root, {}, hasher=hasher)
        self.assertEqual((result["checked"], result["damaged_count"], result["removed_modules"]), (True, 0, 0))

    def test_truncated_and_empty_outputs_are_discarded(self):
        basic = self.module("Basic", "4366fd23170e9e25", "a")
        self.module("Deck", "c270c715af183396", "b")
        self.module("Fiber", "1111111111111111", "c")
        private = self.lib / "Basic.olean.private"
        private.write_bytes(private.read_bytes()[:64])
        (self.lib / "Deck.ir.sig").write_bytes(b"")
        (self.artifacts / basic["olean"]).write_bytes(b"damaged")
        (self.lib / "Basic.Sub.olean").write_bytes(b"another module's output")
        found = repair_outputs.damaged(self.root, {}, hasher=hasher)
        self.assertEqual(
            sorted((d["module"], Path(d["path"]).name, d["size"]) for d in found),
            sorted(
                [
                    ("TauCeti.Flat.Basic", "Basic.olean.private", 64),
                    ("TauCeti.Flat.Basic", basic["olean"], 7),
                    ("TauCeti.Flat.Deck", "Deck.ir.sig", 0),
                ]
            ),
        )
        result = repair_outputs.discard_damaged(self.root, {}, hasher=hasher)
        self.assertEqual((result["damaged_count"], result["removed_modules"]), (3, 2))
        for name in ("Basic", "Deck"):
            self.assertFalse((self.lib / f"{name}.trace").exists())
            self.assertFalse((self.ir / f"{name}.c").exists())
        self.assertFalse((self.records / "4366fd23170e9e25.json").exists())
        self.assertFalse((self.artifacts / basic["olean"]).exists())
        self.assertTrue((self.lib / "Fiber.trace").exists())
        self.assertTrue((self.records / "1111111111111111.json").exists())
        self.assertTrue((self.lib / "Basic.Sub.olean").exists())
        self.assertEqual(repair_outputs.damaged(self.root, {}, hasher=hasher), [])

    def test_a_shared_lake_cache_is_the_one_checked_and_cleaned(self):
        # Repair workers point LAKE_CACHE_DIR at the pool's shared cache, not the worktree's.
        pool = self.root / "pool"
        (pool / "artifacts").mkdir(parents=True)
        (pool / "outputs/TauCeti").mkdir(parents=True)
        basic = self.module("Basic", "4366fd23170e9e25", "a")
        (pool / "artifacts" / basic["ir.sig"]).write_bytes(b"")
        (pool / "outputs/TauCeti/4366fd23170e9e25.json").write_text("{}")
        env = {"LAKE_CACHE_DIR": str(pool)}
        found = repair_outputs.damaged(self.root, env, hasher=hasher)
        self.assertEqual([Path(d["path"]).parent for d in found], [pool / "artifacts"])
        self.assertEqual(repair_outputs.discard_damaged(self.root, env, hasher=hasher)["removed_modules"], 1)
        self.assertFalse((pool / "artifacts" / basic["ir.sig"]).exists())
        self.assertFalse((pool / "outputs/TauCeti/4366fd23170e9e25.json").exists())
        self.assertFalse((self.lib / "Basic.trace").exists())

    def test_an_unavailable_hasher_is_reported_not_raised(self):
        self.module("Basic", "4366fd23170e9e25", "a")

        def failing(paths):
            raise repair_outputs.Unavailable("no helper")

        self.assertEqual(
            repair_outputs.discard_damaged(self.root, {}, hasher=failing), {"checked": False, "reason": "no helper"}
        )
        self.assertTrue((self.lib / "Basic.trace").exists())

    def test_publication_is_refused_for_damaged_or_unchecked_outputs(self):
        self.module("Basic", "4366fd23170e9e25", "a")
        (self.lib / "Basic.ir.sig").write_bytes(b"")
        revision = "d" * 40
        logs = []
        with (
            patch.object(
                repair_publish.repair, "git", side_effect=lambda w, *a, **k: revision if a[0] == "rev-parse" else ""
            ),
            patch.object(repair_publish, "log", logs.append),
            patch.object(repair_outputs, "native_hashes", side_effect=lambda w, e, paths: hasher(paths)),
        ):
            (self.root / ".lake/outputs.jsonl").write_text('"2026-03-17"\n')
            self.assertIsNone(repair_publish.freeze_outputs(self.root, revision, {}, self.root, ("lake",)))
            self.assertIn("differ from their traces", logs[-1])
            self.assertIn("TauCeti.Flat.Basic Basic.ir.sig (0 bytes)", logs[-1])
        with (
            patch.object(
                repair_publish.repair, "git", side_effect=lambda w, *a, **k: revision if a[0] == "rev-parse" else ""
            ),
            patch.object(repair_publish, "log", logs.append),
            patch.object(repair_outputs, "native_hashes", side_effect=repair_outputs.Unavailable("no helper")),
        ):
            self.assertIsNone(repair_publish.freeze_outputs(self.root, revision, {}, self.root, ("lake",)))
            self.assertIn("could not be checked", logs[-1])


if __name__ == "__main__":
    unittest.main()
