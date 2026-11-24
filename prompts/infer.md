Decide whether milestones of a formalization plan have already landed on the base
branch of Tau Ceti, the branch its pull requests merge into. This is a read-only
investigation. Do not change any file outside the one result file, do not build,
and do not use Git, GitHub or the network.

Read __CANDIDATES__. Its `goal` is what the whole plan is for, in its author's
words. Each candidate has a `slug`, a roadmap `area`, the `milestone` as the
plan's author described it, its `line` in the plan with the milestones it needs,
and `readme`: the path of the roadmap's README, or null when the worker has
none. When there is a README, it is the authoritative statement of the
milestone: find the section for the candidate's `layer` (the `Layer N` its
description begins with, when it has one) and the part of it the milestone
names, and judge against what the README asks for there, not only against the
one-line description. `leads` maps a Lean name the description quotes, or the
slug itself, to lines of the base branch that declare a declaration of that
name, as `path:line:text`. Leads are found by name alone and may be empty; an
item with none may still have landed under other names, so search the source
for it. All paths in `leads` are relative to the checkout of the base branch
at __CHECKOUT__.

A milestone has landed only when the base branch states what it describes: the
same objects, in the generality the README and the description give, with the
theorems they ask for and not only a definition of the same name. A
declaration whose proof contains `sorry`, or that depends on one that does,
has not landed. When you are unsure, the answer is that it has not landed.

Write __VERDICTS__, a JSON object with one entry per candidate slug and nothing
else:

{
  "<slug>": {
    "landed": true,
    "evidence": [{"file": "TauCeti/Path/File.lean", "declaration": "Namespace.name"}],
    "why": "one sentence on why these declarations discharge the milestone"
  },
  "<slug>": {"landed": false, "missing": "one line on what the base branch lacks"}
}

For `landed: true`, `evidence` lists one to five declarations that together
discharge the milestone, each a path relative to the checkout and the name of a
declaration in that file. A verdict is discarded unless every file named
declares its name. A candidate's `line` may carry a `reopened:` clause: the
evidence it names was found wanting by later work, and a verdict that names no
declaration beyond it is discarded too.
