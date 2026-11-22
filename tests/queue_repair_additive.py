#!/usr/bin/env python3
"""The additive pre-pass for rebase conflicts (tauceti_queue.repair_additive).

``merge_text`` on Lean sources: imports (deduplicated, kept sorted), module docstring list items and
adjacent new declarations are unioned, the target's lines first; a hunk that changes a base line, sits
inside a proof, declares a name twice, repeats a paragraph, changes the scope after it or leaves a
block open is refused. ``resolve`` reads the three versions from a repository.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_queue import repair_additive as ra  # noqa: E402

HEADER = """\
module

public import Mathlib.A
import Mathlib.C

/-!
# Things

## Main results

* `foo`: a thing
-/

@[expose] public section

namespace TauCeti

theorem foo : True := trivial

end TauCeti
"""


def union(ours: str, base: str, theirs: str) -> tuple[str, list[str]]:
    text, kinds = ra.merge_text(ours, base, theirs)
    return text.decode(), kinds


class Accepted(unittest.TestCase):
    def test_imports_are_unioned_deduplicated_and_kept_sorted(self):
        ours = HEADER.replace("import Mathlib.C\n", "import Mathlib.B\nimport Mathlib.C\n")
        theirs = HEADER.replace("import Mathlib.C\n", "import Mathlib.Ab\nimport Mathlib.B\nimport Mathlib.C\n")
        # Ours' block is `public import Mathlib.A`, `import ...` (not sorted as strings), so the order is
        # ours then theirs, without theirs' duplicate.
        text, kinds = union(ours, HEADER, theirs)
        self.assertEqual(kinds, ["imports"])
        self.assertIn("public import Mathlib.A\nimport Mathlib.B\nimport Mathlib.Ab\nimport Mathlib.C\n", text)
        sorted_base = "import Mathlib.A\nimport Mathlib.D\n\ntheorem x : True := trivial\n"
        ours = sorted_base.replace("import Mathlib.D\n", "import Mathlib.C\nimport Mathlib.D\n")
        theirs = sorted_base.replace("import Mathlib.D\n", "import Mathlib.B\nimport Mathlib.D\n")
        text, _ = union(ours, sorted_base, theirs)
        self.assertTrue(
            text.startswith("import Mathlib.A\nimport Mathlib.B\nimport Mathlib.C\nimport Mathlib.D\n"), text
        )

    def test_an_import_both_sides_add_is_kept_once(self):
        base = "import Mathlib.A\nimport Mathlib.Z\n\ntheorem x : True := trivial\n"
        ours = "import Mathlib.A\nimport Mathlib.Z\nimport Mathlib.B\n\ntheorem x : True := trivial\n"
        theirs = (
            "import Mathlib.A\nimport Mathlib.B\nimport Mathlib.Z\nimport Mathlib.C\n\ntheorem x : True := trivial\n"
        )
        text, kinds = union(ours, base, theirs)
        self.assertEqual(kinds, ["imports"])
        self.assertEqual(text.count("import Mathlib.B\n"), 1, text)

    def test_an_import_in_a_docstring_example_is_not_an_import(self):
        base = "import Mathlib.A\n\n/-!\n```\nimport Extra.I\n```\n-/\n\ntheorem x : True := trivial\n"
        ours = base.replace("import Mathlib.A\n", "import Mathlib.A\nimport Mathlib.B\n")
        theirs = base.replace("import Mathlib.A\n", "import Mathlib.A\nimport Extra.I\n")
        text, _ = union(ours, base, theirs)
        self.assertTrue(text.startswith("import Mathlib.A\nimport Mathlib.B\nimport Extra.I\n"), text)

    def test_docstring_items_keep_the_target_s_first(self):
        ours = HEADER.replace("* `foo`: a thing\n", "* `foo`: a thing\n* `bar`: from main\n  continued\n")
        theirs = HEADER.replace("* `foo`: a thing\n", "* `foo`: a thing\n* `baz`: from the member\n")
        text, kinds = union(ours, HEADER, theirs)
        self.assertEqual(kinds, ["docstring"])
        self.assertIn("* `foo`: a thing\n* `bar`: from main\n  continued\n* `baz`: from the member\n-/", text)

    def test_adjacent_new_declarations_keep_both_in_order(self):
        anchor = "theorem foo : True := trivial\n"
        ours = HEADER.replace(anchor, anchor + "\n/-- Main's. -/\ntheorem bar : True := trivial\n")
        theirs = HEADER.replace(anchor, anchor + "\n@[simp]\ntheorem baz : True := by\n  trivial\n")
        text, kinds = union(ours, HEADER, theirs)
        self.assertEqual(kinds, ["declarations"])
        self.assertIn(
            anchor
            + "\n/-- Main's. -/\ntheorem bar : True := trivial\n\n@[simp]\ntheorem baz : True := by\n  trivial\n",
            text,
        )

    def test_a_balanced_namespace_block_may_open_a_scope_inside_it(self):
        anchor = "end TauCeti\n"
        ours = HEADER.replace(anchor, anchor + "\nnamespace A\n\nopen B\n\ntheorem a : True := trivial\n\nend A\n")
        theirs = HEADER.replace(anchor, anchor + "\ntheorem _root_.c : True := trivial\n")
        text, kinds = union(ours, HEADER, theirs)
        self.assertEqual(kinds, ["declarations"])
        self.assertTrue(text.endswith("end A\n\ntheorem _root_.c : True := trivial\n"), text)

    def test_every_kind_in_one_file(self):
        anchor = "theorem foo : True := trivial\n"
        ours = (
            HEADER.replace("import Mathlib.C\n", "import Mathlib.C\nimport Mathlib.D\n")
            .replace("* `foo`: a thing\n", "* `foo`: a thing\n* `bar`: b\n")
            .replace(anchor, anchor + "\ntheorem bar : True := trivial\n")
        )
        theirs = (
            HEADER.replace("import Mathlib.C\n", "import Mathlib.C\nimport Mathlib.E\n")
            .replace("* `foo`: a thing\n", "* `foo`: a thing\n* `baz`: z\n")
            .replace(anchor, anchor + "\ntheorem baz : True := trivial\n")
        )
        _, kinds = union(ours, HEADER, theirs)
        self.assertEqual(sorted(kinds), ["declarations", "docstring", "imports"])


class Refused(unittest.TestCase):
    def refused(self, ours: str, base: str, theirs: str, reason: str) -> None:
        with self.assertRaises(ra.Refused) as caught:
            ra.merge_text(ours, base, theirs)
        self.assertIn(reason, str(caught.exception))

    def test_a_changed_base_line(self):
        base = "theorem a : True := trivial\n"
        self.refused("theorem a : True := by trivial\n", base, "theorem a : True := (trivial)\n", "not purely additive")

    def test_inside_a_proof(self):
        base = "theorem a : True := by\n  skip\n  trivial\n"
        ours = "theorem a : True := by\n  skip\n  skip\n  trivial\n"
        theirs = "theorem a : True := by\n  skip\n  exact trivial\n  trivial\n"
        self.refused(ours, base, theirs, "inside a declaration, proof or term")

    def test_the_same_name_from_both_sides(self):
        anchor = "theorem foo : True := trivial\n"
        ours = HEADER.replace(anchor, anchor + "\ntheorem bar : True := trivial\n")
        theirs = HEADER.replace(anchor, anchor + "\ntheorem bar : True := by trivial\n")
        self.refused(ours, HEADER, theirs, "declares `TauCeti.bar` twice")

    def test_a_name_the_file_already_declares(self):
        base = "namespace N\n\ntheorem x : True := trivial\n\nend N\n\ntheorem y : True := trivial\n"
        ours = base + "\ntheorem z : True := trivial\n"
        theirs = base + "\ntheorem N.x : True := trivial\n"
        self.refused(ours, base, theirs, "declares `N.x` twice")

    def test_an_identical_block(self):
        anchor = "theorem foo : True := trivial\n"
        block = "\n@[simp]\ntheorem same : True := trivial\n"
        ours = HEADER.replace(anchor, anchor + block + "\ntheorem other : True := trivial\n")
        theirs = HEADER.replace(anchor, anchor + block)
        self.refused(ours, HEADER, theirs, "same paragraph")

    def test_the_same_docstring_item(self):
        ours = HEADER.replace("* `foo`: a thing\n", "* `foo`: a thing\n* `bar`: b\n* `qux`: q\n")
        theirs = HEADER.replace("* `foo`: a thing\n", "* `foo`: a thing\n* `bar`: b\n")
        self.refused(ours, HEADER, theirs, "same docstring line")

    def test_prose_in_a_docstring(self):
        ours = HEADER.replace("* `foo`: a thing\n", "* `foo`: a thing\n\nSome prose.\n")
        theirs = HEADER.replace("* `foo`: a thing\n", "* `foo`: a thing\n* `baz`: z\n")
        self.refused(ours, HEADER, theirs, "not a list item")

    def test_a_scope_change(self):
        anchor = "theorem foo : True := trivial\n"
        ours = HEADER.replace(anchor, anchor + "\nvariable {α : Type}\n\ntheorem bar : True := trivial\n")
        theirs = HEADER.replace(anchor, anchor + "\ntheorem baz : True := trivial\n")
        self.refused(ours, HEADER, theirs, "changes the scope after it")

    def test_an_unclosed_public_section(self):
        base = "theorem a : True := trivial\n"
        for opener in ("public section", "@[expose] public section"):
            ours = base + f"\n{opener}\n\ndef c : Nat := 1\n"
            theirs = base + "\ndef d : Nat := 2\n"
            self.refused(ours, base, theirs, "namespace or section open")

    def test_an_unclosed_namespace(self):
        anchor = "end TauCeti\n"
        ours = HEADER.replace(anchor, anchor + "\nnamespace A\n\ntheorem a : True := trivial\n")
        theirs = HEADER.replace(anchor, anchor + "\ntheorem c : True := trivial\n")
        self.refused(ours, HEADER, theirs, "namespace or section open")

    def test_after_an_attribute(self):
        base = "theorem a : True := trivial\n\n@[simp]\ntheorem b : True := trivial\n"
        ours = base.replace("@[simp]\n", "@[simp]\ntheorem c : True := trivial\n\n@[simp]\n")
        theirs = base.replace("@[simp]\n", "@[simp]\ntheorem d : True := trivial\n\n@[simp]\n")
        self.refused(ours, base, theirs, "follows an attribute")

    def test_a_declaration_docstring(self):
        base = "/-- A.\n-/\ntheorem a : True := trivial\n"
        ours = "/-- A.\nMain's sentence.\n-/\ntheorem a : True := trivial\n"
        theirs = "/-- A.\nThe member's sentence.\n-/\ntheorem a : True := trivial\n"
        self.refused(ours, base, theirs, "inside a comment or declaration docstring")

    def test_inside_a_do_block_after_comments(self):
        base = "def f : Nat := Id.run do\n  let mut x := 1\n-- step\n  return x\n"
        ours = "def f : Nat := Id.run do\n  let mut x := 1\n-- a\n  x := x + 1\n-- step\n  return x\n"
        theirs = "def f : Nat := Id.run do\n  let mut x := 1\n-- b\n  x := x * 2\n-- step\n  return x\n"
        self.refused(ours, base, theirs, "inside a declaration, proof or term")

    def test_a_one_line_doc_comment_left_for_what_follows(self):
        base = "theorem a : True := trivial\n\ntheorem b : True := trivial\n"
        ours = base.replace("\ntheorem b", "\ntheorem c : True := trivial\n\n/-- B. -/\ntheorem b")
        theirs = base.replace("\ntheorem b", "\ntheorem d : True := trivial\n\n/-- B'. -/\ntheorem b")
        self.refused(ours, base, theirs, "doc comment")

    def test_the_same_name_inside_namespace_blocks(self):
        base = "theorem a : True := trivial\n"
        ours = base + "\nnamespace N\n  theorem same : True := trivial\nend N\n"
        theirs = base + "\nnamespace N\n  theorem same : True := by trivial\nend N\n"
        self.refused(ours, base, theirs, "declares `N.same` twice")

    def test_an_indented_scope_command(self):
        base = "theorem a : True := trivial\n"
        ours = base + '\ntheorem b : True := trivial\n  local notation "flag" => false\n'
        theirs = base + "\ndef d : Bool := true\n"
        self.refused(ours, base, theirs, "syntax or scoped")

    def test_a_doc_comment_behind_an_ordinary_comment(self):
        base = "theorem a : True := trivial\n\ntheorem b : True := trivial\n"
        ours = base.replace("\ntheorem b", "\ntheorem c : True := trivial\n\n/-- B. -/\n/- note -/\ntheorem b")
        theirs = base.replace("\ntheorem b", "\ntheorem d : True := trivial\n\n/-- B. -/\n/- note -/\ntheorem b")
        self.refused(ours, base, theirs, "doc comment")

    def test_an_attribute_with_a_line_comment(self):
        base = "theorem a : True := trivial\n\ntheorem b : True := trivial\n"
        ours = base.replace("\ntheorem b", "\ntheorem c : True := trivial\n\n@[simp] -- for b\ntheorem b")
        theirs = base.replace("\ntheorem b", "\ntheorem d : True := trivial\n\n@[simp] -- for b\ntheorem b")
        self.refused(ours, base, theirs, "attribute")

    def test_a_dangling_attribute_behind_a_comment_or_over_lines(self):
        base = "theorem a : True := trivial\n\ntheorem b : True := trivial\n"
        for attribute in (
            "@[implemented_by r] /- for b -/\n",
            "@[simp,\n  nolint]\n",
            "@[\n  implemented_by r\n]\n",
            "  @[simp]\n",
            "  @[\n    implemented_by r\n  ]\n",
            "  @[aesop safe apply (rule_sets := [default])]\n",
        ):
            ours = base.replace("\ntheorem b", f"\ndef c : Nat := 1\n\n{attribute}theorem b")
            theirs = base.replace("\ntheorem b", "\ndef d : Nat := 3\n\ntheorem b")
            self.refused(ours, base, theirs, "attribute")

    def test_code_after_a_block_comment(self):
        base = "def answer : Bool := true\n"
        ours = base + '\ndef c : Nat := 1\n/- note -/ local notation "answer" => false\n'
        theirs = base + "\ndef result : Bool := answer\n"
        self.refused(ours, base, theirs, "syntax or scoped")

    def test_global_notation_inside_a_namespace(self):
        base = "def answer : Bool := true\n"
        ours = base + '\nnamespace N\nnotation "answer" => false\nend N\n'
        theirs = base + "\ndef result : Bool := answer\n"
        self.refused(ours, base, theirs, "syntax or scoped")

    def test_a_local_attribute_anywhere_in_the_list(self):
        base = "def answer : Bool := true\n"
        for attribute in ("attribute [simp, local instance] alt\n", "attribute [simp,\n  local instance] alt\n"):
            ours = base + "\ndef c : Nat := 1\n\n" + attribute
            theirs = base + "\ndef result : Bool := answer\n"
            self.refused(ours, base, theirs, "scope")

    def test_before_a_deriving_clause(self):
        base = "structure A where\n  x : Nat\nderiving Inhabited\n"
        ours = "structure A where\n  x : Nat\n\nstructure B where\n  y : Nat\nderiving Inhabited\n"
        theirs = "structure A where\n  x : Nat\n\nstructure C where\n  z : Nat\nderiving Inhabited\n"
        self.refused(ours, base, theirs, "inside a declaration, proof or term")

    def test_in_inside_a_comment_does_not_scope(self):
        base = "def answer : Bool := true\n"
        ours = base + "\ndef c : Nat := 1\nopen Foo /- in -/\n"
        theirs = base + "\ndef result : Bool := answer\n"
        self.refused(ours, base, theirs, "changes the scope after it")

    def test_inside_a_multiline_string(self):
        base = 'def s : String := "head\ndef tail : Nat := 0\n"\n'
        ours = 'def s : String := "head\ndef a : Nat := 1\ndef tail : Nat := 0\n"\n'
        theirs = 'def s : String := "head\ndef b : Nat := 2\ndef tail : Nat := 0\n"\n'
        self.refused(ours, base, theirs, "string")

    def test_conflict_markers_in_a_side(self):
        self.refused("<<<<<<< x\n", "", "y\n", "conflict-marker")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def commit(repo: Path, files: dict[str, str], parent: str | None) -> str:
    if parent:
        git(repo, "checkout", "--quiet", "--detach", parent)
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", "c")
    return git(repo, "rev-parse", "HEAD").strip()


class Resolve(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.repo = Path(scratch.name)
        git(self.repo, "init", "--quiet")
        git(self.repo, "config", "user.name", "T")
        git(self.repo, "config", "user.email", "t@example.invalid")
        anchor = "theorem foo : True := trivial\n"
        self.base = commit(self.repo, {"T/A.lean": HEADER, "notes.txt": "a\n"}, None)
        self.ours = commit(
            self.repo, {"T/A.lean": HEADER.replace(anchor, anchor + "\ntheorem o : True := trivial\n")}, self.base
        )
        self.theirs = commit(
            self.repo, {"T/A.lean": HEADER.replace(anchor, anchor + "\ntheorem t : True := trivial\n")}, self.base
        )

    def test_a_conflict_resolves_with_its_explanation(self):
        result = ra.resolve(self.repo, self.ours, self.theirs, ["T/A.lean"])
        self.assertIsInstance(result, ra.Union, result)
        self.assertEqual((result.hunks, result.kinds), (1, {"declarations": 1}))
        self.assertTrue(ra.marked(result.note))
        self.assertIn("theorem o : True := trivial\n\ntheorem t : True := trivial\n", result.files["T/A.lean"].decode())

    def test_other_files_and_missing_sides_are_left_to_the_agent(self):
        self.assertIn("not a Lean file", ra.resolve(self.repo, self.ours, self.theirs, ["notes.txt"]))
        self.assertIn("added or deleted", ra.resolve(self.repo, self.ours, self.theirs, ["T/B.lean"]))
        self.assertEqual(ra.resolve(self.repo, self.ours, self.theirs, []), "no conflicting paths")

    def test_a_file_that_is_not_regular_on_every_side(self):
        git(self.repo, "checkout", "--quiet", "--detach", self.theirs)
        (self.repo / "T/A.lean").chmod(0o755)
        git(self.repo, "commit", "--quiet", "-am", "mode")
        theirs = git(self.repo, "rev-parse", "HEAD").strip()
        self.assertIn("not a regular file", ra.resolve(self.repo, self.ours, theirs, ["T/A.lean"]))

    def test_the_switch(self):
        self.assertTrue(ra.enabled({}))
        self.assertFalse(ra.enabled({"TAUCETI_QUEUE_ADDITIVE_PREPASS": "0"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
