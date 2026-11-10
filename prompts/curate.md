Decide whether milestones of a formalization plan have already landed on the main
branch of Tau Ceti. This is a read-only investigation. Do not change any file
outside the one result file, do not build, and do not use Git, GitHub or the
network.

Read __CANDIDATES__. Each candidate has a `slug`, a roadmap `area`, the
`milestone` as its author described it, and `hits`: for a Lean name the
description quotes, or for the slug itself, the lines of main that declare a
declaration of that name, as `path:line:text`. The paths are relative to the
checkout of main at __CHECKOUT__. Read the declarations there, and whatever
around them you need to judge them. A hit is a lead found by name alone.

A milestone has landed only when main states what it describes: the same
objects, in the generality the description gives, with the theorems it asks for
and not only a definition of the same name. A declaration whose proof contains
`sorry` has not landed. When you are unsure, the answer is that it has not
landed.

Write __VERDICTS__, a JSON object with one entry per candidate slug and nothing
else:

{
  "<slug>": {"landed": true, "file": "TauCeti/Path/File.lean", "declaration": "Namespace.name"},
  "<slug>": {"landed": false, "missing": "one line on what main lacks"}
}

For `landed: true`, `file` is a path relative to the checkout and `declaration`
is the name of one declaration in that file that discharges the milestone. A
verdict whose file does not declare that name is discarded.
