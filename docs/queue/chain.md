# Attributed chain tool

`tauceti_queue/chain.py` builds, replays, validates and compares the per-PR squash chains of the AI
merge queue (increment 0 of `docs/redesign/specs/ai-merge-queue.md` on the `overhaul` branch). It is
stdlib plus `git` subprocesses, usable from Python with plain dataclasses and from the command line
with JSON:

```sh
python -m tauceti_queue.chain build    --repo R --members members.json [--base SHA] [--repository owner/repo] [--chain-id ID] --json
python -m tauceti_queue.chain replay   --repo R --base SHA --tip SHA --onto SHA [--chain-id ID] --json
python -m tauceti_queue.chain validate --repo R --base SHA --tip SHA --members members.json [--repository owner/repo] --json
python -m tauceti_queue.chain compare  --repo R --prior-base SHA --prior-tip SHA --base SHA --tip SHA --json
```

Exit status is 0 for a clean chain, a valid chain or a comparison without violations; 1 when the
result reports a conflict or violations; 2 for a usage error (missing commit, non-linear chain
handed to `replay`, malformed members file), which is printed to stderr. Without `--json` a short
text summary is printed instead of the result structure.

## Commit format

A chain is a linear sequence of single-parent commits on a recorded base, exactly one commit per
member PR. Each commit's message is:

```text
<title, or "owner/repo#N at <head sha prefix>" when the title is unknown> (#N)

<description: the PR body, when known>

Co-authored-by: Name <email>        (zero or more)
Source-PR: TauCetiProject/TauCeti#123
Accepted-Head: <original accepted SHA>
```

The subject ends in Bors's ` (#N)` suffix (added once; a title already ending in it is kept as is),
so TauCetiTools `lint-scope.sh` and GitHub's commit-to-PR links attribute the commit to its PR.
`Source-PR` and `Accepted-Head` are always present; they are the member's identity and survive every
later rewrite. The commit author is the member record's author when known, otherwise the author of
the accepted head commit. Co-authors are the record's list when given; when the record carries none
(`coauthors=None`) they are the other distinct authors of the PR's commits, as GitHub's squash merge
does; an explicit empty list means none. A bare GitHub login in any identity field is given the
`login@users.noreply.github.com` address. The committer is the queue identity
(`TauCeti queue <tauceti-queue@users.noreply.github.com>`, overridable per call).

An agent that edits a commit appends its explanation at the end of the message, after the trailers.
The trailers are therefore located anywhere in the message, not only in the final paragraph; a
commit whose message carries two different values for one of them is reported as ambiguous.

A member whose squash adds nothing to the chain so far (its change is already in the base or in an
earlier member) is an explicit empty commit, never a dropped one. `replay` keeps empty commits empty
and records a commit that becomes empty on the new base as such.

## Member records

`Member(repository, number, head_sha, title="", description="", author="", coauthors=None, evidence={})`
describes an accepted PR revision. `evidence` is admission evidence passed through untouched; it is
reported in JSON and never enters a commit. `members_from_candidate(snapshot, repository)` turns a
Bors candidate journal entry (`base_sha`, `members[].pr`, `members[].head_sha`, plus whatever else
the entry carries, which lands in `evidence`) into `CandidateMembers(base_sha, members)`.
`load_members(data, repository)` accepts either a list of member dicts (the `Member.to_dict` form,
or journal-style entries with `pr`/`body`) or a snapshot with a `members` key; the CLI's
`--members` file is read with it.

## Operations

All operations run `git -C <repo>` with a controlled environment. Merges are computed with
`git merge-tree --write-tree` and commits written with `git commit-tree`, so no worktree, index or
checked-out branch is touched; the only ref written is `refs/tauceti-queue/<chain-id>/tip`. A
conflict therefore leaves nothing to clean up: the tree with conflict markers that `merge-tree`
writes is an unreferenced object that `git gc` collects.

**`build(repo, base, members, chain_id=None, committer=DEFAULT_COMMITTER) -> ChainResult`**
squashes each member head onto the chain in the given order: a three-way merge of the head onto the
current tip with the merge base git computes (what `git merge --squash` would stage), committed with
the message above. On a conflict the result has `status == "conflict"`, the conflicting member's
record carries `conflict_paths`, the members after it are `pending`, and `tip` is the last clean
commit (the base when none was made). `chain_id` defaults to a random one; duplicate members, an
unknown base or head, or an unsafe `chain_id` raise `ChainError`.

**`replay(repo, base, tip, onto, chain_id=None, committer=...) -> ChainResult`** rebases the chain
`base..tip` onto `onto`, one commit per commit, with the message, author and author date copied
verbatim (a cherry-pick of each commit onto the new tip, with the commit's own parent as merge base).
Conflicts are reported as in `build`. A mechanical replay changes every hash; `compare` still reports
it as clean.

**`validate(repo, base, tip, members) -> ValidationReport`** lists every violation rather than
stopping at the first. `members` may be `Member` records or `owner/repo#N` strings. Checks and their
codes:

| code | meaning |
| --- | --- |
| `missing-object` | base or tip is not a commit in the repository |
| `wrong-base` | base is not an ancestor of tip, or the first commit's parent is not the declared base |
| `merge-commit` | a commit has more or fewer than one parent |
| `missing-source-pr`, `ambiguous-source-pr` | no, or more than one distinct, `Source-PR` trailer |
| `missing-accepted-head`, `ambiguous-accepted-head` | the same for `Accepted-Head` |
| `unknown-member` | a commit's `Source-PR` is not in the recorded member list |
| `duplicate-member` | two commits carry the same PR (or the member list names one twice) |
| `missing-member` | a recorded member has no commit |
| `count-mismatch` | the commit count differs from the member count |
| `accepted-head-mismatch` | a `Member` record's head differs from the commit's `Accepted-Head` |

Membership is compared by PR identity, never by position, so a replacement chain whose members are
in another order is accepted. The report's `commits` list gives each commit's SHA, `Source-PR`,
`Accepted-Head` and whether it is empty.

**`compare(repo, prior_base, prior_tip, base, tip) -> ComparisonReport`** pairs the commits of a
prior chain and a replacement chain by `Source-PR` and reports a `MemberProvenance` per member:

- `clean`: the replacement commit's tree is exactly what replaying the prior commit onto the
  replacement's parent produces, and the message is unchanged. A mechanical rebase, and the replay
  of a member after an edited predecessor, are therefore clean.
- `edited`: the content differs from that mechanical replay (including a replay that would have
  conflicted), or text was appended to the message.
- `missing` / `added`: present only in the prior or only in the replacement chain.

`message_preserved` is the append-only check: the prior message, modulo trailing whitespace, must be
a prefix of the replacement's; `explanation` is the appended text, which is what the PR comment is
generated from. `message-not-preserved`, `member-missing` and `member-added` are violations in the
report, not exceptions.

Lower-level pieces the controller reuses: `chain_commits(repo, base, tip)` reads a chain as
`ChainCommit` records (SHA, tree, parents, author, message, with `source_pr` and `accepted_head`
properties); `merge_trees`, `commit_tree`, `squash_message`, `parse_trailers`, `unique_trailer`,
`message_preserved` and `appended_text` are the pure steps the operations are built from.
