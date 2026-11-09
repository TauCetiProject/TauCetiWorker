"""Experimental local rebase driver. Not connected to queue admissions or worker execution.

Git performs ordinary three-way merges; every conflict is returned to a resolver. The driver
owns commits, attribution, explanations, an atomic progress journal, and a private Git ref.
Use a standalone scratch clone without remotes, with state outside the source checkout.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from . import chain

SCHEMA = "tauceti.rebase-driver/v1"
MARKER = re.compile(rb"^(?:<{7}|={7}|>{7})(?: |$)", re.MULTILINE)


class DriverError(RuntimeError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _atomic(path: Path, value):
    fd, temporary = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Driver:
    def __init__(self, state_dir: Path | str):
        self.directory = Path(state_dir).resolve()
        self.state_path = self.directory / "state.json"
        self.state = json.loads(self.state_path.read_text())
        if self.state.get("schema") != SCHEMA or _digest(self.state["plan"]) != self.state["binding"]:
            raise DriverError("checkpoint binding is invalid")
        self.repo = Path(self.state["plan"]["repo"])
        self._isolation()

    @classmethod
    def create(cls, repo, state_dir, base, tip, target, *, review_clean=False, protected_prs=()):
        repo, directory = Path(repo).resolve(), Path(state_dir).resolve()
        if directory.is_relative_to(repo) or repo.is_relative_to(directory):
            raise DriverError("state and checkout must be separate directories")
        if directory.exists() and any(directory.iterdir()):
            raise DriverError("state directory is not empty")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        obj = object.__new__(cls)
        obj.directory, obj.state_path, obj.repo = directory, directory / "state.json", repo
        obj._isolation()
        if obj.git("status", "--porcelain").strip():
            raise DriverError("scratch checkout must be clean")
        base, tip, target = (chain.resolve_commit(repo, rev) for rev in (base, tip, target))
        commits = chain.chain_commits(repo, base, tip)
        if any(len(c.parents) != 1 or not c.source_pr or not c.accepted_head for c in commits):
            raise DriverError("input must be a linear attributed chain")
        if len({c.source_pr for c in commits}) != len(commits):
            raise DriverError("input repeats a Source-PR")
        plan = {
            "repo": str(repo),
            "base": base,
            "input_tip": tip,
            "target": target,
            "members": [dataclasses.asdict(c) for c in commits],
            "review_clean": review_clean,
            "protected_prs": list(protected_prs),
            "landed": chain.landed_prs(repo, base, target),
        }
        ref = f"refs/tauceti-rebase-prototype/{uuid.uuid4().hex}/tip"
        obj.git("checkout", "--detach", "--force", target)
        shutil.copyfile(repo / ".git/index", directory / "checkout.index")
        obj.git("update-ref", ref, target, "0" * len(target))
        obj.state = {
            "schema": SCHEMA,
            "plan": plan,
            "binding": _digest(plan),
            "ref": ref,
            "cursor": 0,
            "tip": target,
            "checkout_head": target,
            "checkout_tree": chain.tree_of(repo, target),
            "phase": "running",
            "pending": None,
            "transition": None,
            "materializing": None,
            "records": [],
        }
        obj.save()
        obj._helpers()
        return obj

    def git(self, *args, input=None, env=None, check=True):
        controlled = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        controlled.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                **(env or {}),
            }
        )
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never", "-C", str(self.repo), *args],
            input=input,
            capture_output=True,
            env=controlled,
        )
        if check and result.returncode:
            raise DriverError(f"git {args[0]} failed: {result.stderr.decode(errors='replace').strip()}")
        return result.stdout.decode("utf-8", "surrogateescape") if check else result

    def _isolation(self):
        if not (self.repo / ".git").is_dir():
            raise DriverError("prototype requires a standalone scratch checkout, not a linked worktree")
        if self.git("remote").strip():
            raise DriverError("prototype requires a scratch clone without remotes")

    @contextlib.contextmanager
    def locked(self):
        with (self.directory / "driver.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            self.state = json.loads(self.state_path.read_text())
            if _digest(self.state["plan"]) != self.state["binding"]:
                raise DriverError("checkpoint binding is invalid")
            self.recover()
            self._guard()
            yield

    def save(self):
        _atomic(self.state_path, self.state)

    def _guard(self):
        if self.git("rev-parse", "HEAD").strip() != self.state["checkout_head"]:
            raise DriverError("checkout HEAD changed outside the driver")
        if self.git("rev-parse", self.state["ref"]).strip() != self.state["tip"]:
            raise DriverError("checkpoint ref changed outside the driver")

    def recover(self):
        if self.state["transition"]:
            self._finish_transition()
        if self.state["materializing"]:
            self._finish_materialization()
        if self.state["pending"]:
            packet = self.directory / "packets" / self.state["pending"]["id"] / "packet.json"
            if not packet.exists():
                self._export_packet()

    def _finish_transition(self):
        transition = self.state["transition"]
        ref = self.git("rev-parse", self.state["ref"]).strip()
        if ref == transition["before"]:
            self.git("update-ref", self.state["ref"], transition["after"], transition["before"])
        elif ref != transition["after"]:
            raise DriverError("checkpoint ref drift during commit publication")
        self.state["records"].append(transition["record"])
        self.state["tip"] = transition["after"]
        self.state["cursor"] += 1
        self.state.update(transition=None, pending=None, phase="running")
        if transition["record"]["packet_id"]:
            self.state["materializing"] = {"tree": transition["record"]["tree"], "head": transition["after"]}
        self.save()

    def _finish_materialization(self):
        material = self.state["materializing"]
        head = self.git("rev-parse", "HEAD").strip()
        if head not in {self.state["checkout_head"], material["head"]}:
            raise DriverError("checkout HEAD changed during materialization")
        if self.git("rev-parse", self.state["ref"]).strip() != self.state["tip"]:
            raise DriverError("checkpoint ref changed during materialization")
        # Keep a driver-owned index with stat information. Recreating an empty index
        # would rehash every source file at every conflict in a large repository.
        index = self.directory / "checkout.index"
        self.git("read-tree", "--reset", "-u", material["tree"], env={"GIT_INDEX_FILE": str(index)})
        shutil.copyfile(index, self.repo / ".git/index")
        self.git("update-ref", "HEAD", material["head"])
        self.state.update(checkout_head=material["head"], checkout_tree=material["tree"], materializing=None)
        self.save()

    def _materialize(self, tree):
        self.state["materializing"] = {"tree": tree, "head": self.state["tip"]}
        self.save()
        self._finish_materialization()

    def _clean_checkout(self):
        # Compare the files against the driver's recorded tree, not the agent-controlled index.
        with self._index(self.state["checkout_tree"]) as env:
            changed = self._working_changes(env)
        if changed:
            raise DriverError(f"unexpected source edits before replay: {changed[:5]}")

    @contextlib.contextmanager
    def _index(self, tree):
        fd, name = tempfile.mkstemp(prefix=".index-", dir=self.directory)
        os.close(fd)
        os.unlink(name)
        env = {"GIT_INDEX_FILE": name}
        try:
            shutil.copyfile(self.directory / "checkout.index", name)
            self.git("read-tree", "-m", tree, env=env)
            yield env
        finally:
            for suffix in ("", ".lock"):
                Path(name + suffix).unlink(missing_ok=True)

    def _working_changes(self, env):
        tracked = self.git("diff", "--name-only", "-z", env=env).split("\0")
        untracked = self.git("ls-files", "--others", "--exclude-standard", "-z", env=env).split("\0")
        return sorted(set(filter(None, tracked + untracked)))

    def _member(self):
        record = dict(self.state["plan"]["members"][self.state["cursor"]])
        record["parents"] = tuple(record["parents"])
        return chain.ChainCommit(**record)

    def _merge(self, member):
        landing = chain.landed_commit(self.state["plan"]["landed"], member.source_pr)
        if landing:
            return chain.tree_of(self.repo, self.state["tip"]), [], {}, landing
        result = self.git(
            "merge-tree",
            "--write-tree",
            "-z",
            f"--merge-base={member.parents[0]}",
            self.state["tip"],
            member.sha,
            check=False,
        )
        if result.returncode not in (0, 1):
            raise DriverError(result.stderr.decode(errors="replace"))
        records = result.stdout.decode("utf-8", "surrogateescape").split("\0")
        stages = {}
        if result.returncode:
            for item in records[1:]:
                if not item:
                    break
                metadata, path = item.split("\t", 1)
                mode, oid, stage = metadata.split()
                stages.setdefault(path, {})[stage] = {"mode": mode, "oid": oid}
            if not stages:
                raise DriverError("non-file merge conflict needs manual inspection; no member was committed")
        return records[0], sorted(stages), stages, None

    def advance(self, *, one_member=False):
        with self.locked():
            if self.state["pending"] or self.state["phase"] == "complete":
                return self.status()
            self._clean_checkout()
            while self.state["cursor"] < len(self.state["plan"]["members"]):
                member = self._member()
                tree, paths, stages, landing = self._merge(member)
                if paths or self.state["plan"]["review_clean"]:
                    self._pause(member, tree, paths, stages, landing)
                    return self.status()
                self._commit(member, tree, "", None, landing, changed=[])
                if one_member:
                    return self.status()
            self._materialize(chain.tree_of(self.repo, self.state["tip"]))
            self.state["phase"] = "complete"
            self.save()
            return self.status()

    def _pause(self, member, tree, paths, stages, landing):
        original_paths = list(
            filter(
                None,
                self.git("diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z", member.sha).split(
                    "\0"
                ),
            )
        )
        identity = {
            "binding": self.state["binding"],
            "position": self.state["cursor"],
            "prefix": self.state["tip"],
            "source": member.sha,
            "candidate": tree,
            "stages": stages,
        }
        pending = {
            "id": _digest(identity),
            "tree": tree,
            "paths": paths,
            "original_paths": original_paths,
            "source": member.sha,
            "landing": landing,
            "conflict": bool(paths),
            "stages": stages,
        }
        self.state.update(pending=pending, phase="conflict" if paths else "review")
        self._materialize(tree)
        self._export_packet()

    def _export_packet(self):
        pending = self.state["pending"]
        member = self._member()
        paths, stages = pending["paths"], pending["stages"]
        directory = self.directory / "packets" / pending["id"]
        directory.mkdir(parents=True, exist_ok=True)
        exported = {}
        for path, blobs in stages.items():
            exported[path] = {}
            for stage, info in blobs.items():
                data = self.git("cat-file", "blob", info["oid"], check=False)
                if data.returncode:
                    raise DriverError("conflict blob disappeared")
                name = directory / f"{info['oid']}.blob"
                name.write_bytes(data.stdout)
                exported[path][stage] = {**info, "file": str(name)}
        patch = self.git("show", "--format=fuller", "--no-ext-diff", member.sha)
        packet = {
            "id": pending["id"],
            "binding": self.state["binding"],
            "position": self.state["cursor"] + 1,
            "total": len(self.state["plan"]["members"]),
            "source_pr": member.source_pr,
            "accepted_head": member.accepted_head,
            "source_commit": member.sha,
            "prefix": self.state["tip"],
            "target": self.state["plan"]["target"],
            "conflicted_paths": paths,
            "member_paths": pending["original_paths"],
            "stages": exported,
            "original_patch": patch[:64000],
            "original_patch_truncated": len(patch) > 64000,
            "target_history": self.git(
                "log",
                "-8",
                "--format=%h %s",
                f"{self.state['plan']['base']}..{self.state['plan']['target']}",
                "--",
                *paths,
            )
            if paths
            else "",
        }
        _atomic(directory / "packet.json", packet)

    @staticmethod
    def _source_path(path):
        value = Path(path)
        return (
            not value.is_absolute()
            and ".." not in value.parts
            and path.endswith(".lean")
            and (path.startswith("TauCeti/") or path == "TauCeti.lean")
        )

    def resolve(self, packet_id, explanation, *, allow_paths=()):
        with self.locked():
            completed = next((r for r in self.state["records"] if r["packet_id"] == packet_id), None)
            if completed is not None:
                return {**self.status(), "already_resolved": completed["commit"]}
            pending = self.state["pending"]
            if pending is None or pending["id"] != packet_id:
                raise DriverError("stale or unknown conflict packet")
            member = self._member()
            extra = set(allow_paths)
            if any(not self._source_path(path) for path in extra):
                raise DriverError("additional edits must be TauCeti Lean source paths")
            allowed = set(pending["paths"] + pending["original_paths"]) | extra
            with self._index(pending["tree"]) as env:
                changed = self._working_changes(env)
                if set(changed) - allowed:
                    raise DriverError(
                        f"edits outside this member require explicit attribution: {set(changed) - allowed}"
                    )
                if (changed or pending["conflict"]) and not explanation.strip():
                    raise DriverError("a resolution needs an explanation")
                if (changed or pending["conflict"]) and int(member.source_pr.rsplit("#", 1)[1]) in self.state["plan"][
                    "protected_prs"
                ]:
                    raise DriverError("this member opted out of source repair")
                for path in changed:
                    file = self.repo / path
                    parents = [p for p in file.parents if p.is_relative_to(self.repo)]
                    if file.is_symlink() or any(p.is_symlink() for p in parents):
                        raise DriverError("symlinked resolution paths are not allowed")
                for path in set(pending["paths"] + changed):
                    file = self.repo / path
                    if file.is_file() and MARKER.search(file.read_bytes()):
                        raise DriverError(f"unresolved conflict markers in {path}")
                if changed:
                    self.git("add", "-A", "--", *changed, env=env)
                tree = self.git("write-tree", env=env).strip()
            self._commit(member, tree, explanation, pending["id"], pending["landing"], changed=changed)
            # Synchronize the real index without trusting anything the resolver staged there.
            self._finish_materialization()
            return self.status()

    def _commit(self, member, tree, explanation, packet_id, landing, *, changed):
        message = member.message
        if landing:
            message = chain.landed_message(message, landing)
        if explanation.strip():
            message = message.rstrip() + "\n\nRepair: " + explanation.strip() + "\n"
        env = {
            "GIT_AUTHOR_NAME": member.author_name,
            "GIT_AUTHOR_EMAIL": member.author_email,
            "GIT_AUTHOR_DATE": member.author_date,
            "GIT_COMMITTER_NAME": "TauCeti rebase prototype",
            "GIT_COMMITTER_EMAIL": "rebase-prototype@tauceti.invalid",
        }
        commit = self.git(
            "commit-tree",
            tree,
            "-p",
            self.state["tip"],
            "-F",
            "-",
            input=message.encode("utf-8", "surrogateescape"),
            env=env,
        ).strip()
        record = {
            "source": member.sha,
            "source_pr": member.source_pr,
            "accepted_head": member.accepted_head,
            "commit": commit,
            "tree": tree,
            "parent": self.state["tip"],
            "packet_id": packet_id,
            "explanation": explanation,
            "changed_paths": changed,
            "landing": landing,
            "conflict": bool(self.state["pending"] and self.state["pending"]["conflict"]),
        }
        self.state["transition"] = {"before": self.state["tip"], "after": commit, "record": record}
        self.save()
        self._finish_transition()

    def check(self):
        with self.locked():
            if self.state["phase"] != "complete":
                raise DriverError("replay is not complete")
            self._clean_checkout()
            members = self.state["plan"]["members"]
            report = chain.validate(
                self.repo,
                self.state["plan"]["target"],
                self.state["tip"],
                [chain.read_commit(self.repo, m["sha"]).source_pr for m in members],
            )
            errors = [v.detail for v in report.violations]
            commits = chain.chain_commits(self.repo, self.state["plan"]["target"], self.state["tip"])
            if len(self.state["records"]) != len(members) or len(commits) != len(members):
                errors.append("member/record/commit counts disagree")
            for source, record, new in zip(members, self.state["records"], commits, strict=False):
                original = chain.read_commit(self.repo, source["sha"])
                if (new.source_pr, new.accepted_head) != (original.source_pr, original.accepted_head):
                    errors.append(f"member order or accepted head changed at {source['sha']}")
                if (new.sha, new.tree, new.parents) != (record["commit"], record["tree"], (record["parent"],)):
                    errors.append(f"commit differs from journal at {source['sha']}")
                if (new.author_name, new.author_email, new.author_date) != (
                    source["author_name"],
                    source["author_email"],
                    source["author_date"],
                ):
                    errors.append(f"author changed at {source['sha']}")
                if not new.message.rstrip().startswith(source["message"].rstrip()):
                    errors.append(f"original message changed at {source['sha']}")
            return {
                "ok": not errors,
                "members": len(members),
                "tip": self.state["tip"],
                "errors": errors,
                "lean_validation": "not run by this experimental Git driver",
            }

    def status(self):
        pending = self.state["pending"]
        return {
            "phase": self.state["phase"],
            "completed": self.state["cursor"],
            "total": len(self.state["plan"]["members"]),
            "tip": self.state["tip"],
            "packet_id": pending["id"] if pending else None,
            "packet": str(self.directory / "packets" / pending["id"] / "packet.json") if pending else None,
        }

    def _helpers(self):
        directory = self.repo / ".tauceti-repair"
        directory.mkdir(exist_ok=True)
        exclude = self.repo / ".git/info/exclude"
        with exclude.open("a") as stream:
            stream.write("\n/.tauceti-repair/\n")
        root = str(Path(__file__).resolve().parent.parent)
        for name in ("resolve", "advance", "status", "check"):
            helper = directory / name
            helper.write_text(
                f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {root!r})\n"
                "from tauceti_queue.rebase_driver import main\n"
                f"raise SystemExit(main(['--state-dir', {str(self.directory)!r}, {name!r}, *sys.argv[1:]]))\n"
            )
            helper.chmod(0o755)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    for name in ("repo", "base", "tip", "target"):
        init.add_argument("--" + name, required=True)
    init.add_argument("--review-clean", action="store_true", help="historical oracle testing only")
    commands.add_parser("advance")
    commands.add_parser("status")
    commands.add_parser("check")
    resolve = commands.add_parser("resolve")
    resolve.add_argument("--conflict-id", required=True)
    resolve.add_argument("--explanation-file", type=Path)
    resolve.add_argument("--allow-path", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            driver = Driver.create(
                args.repo, args.state_dir, args.base, args.tip, args.target, review_clean=args.review_clean
            )
            result = driver.advance()
        else:
            driver = Driver(args.state_dir)
            if args.command == "resolve":
                note = args.explanation_file.read_text() if args.explanation_file else ""
                result = driver.resolve(args.conflict_id, note, allow_paths=args.allow_path)
                if "already_resolved" not in result:
                    result = driver.advance()
            elif args.command == "check":
                result = driver.check()
            elif args.command == "advance":
                result = driver.advance()
            else:
                with driver.locked():
                    result = driver.status()
        print(json.dumps(result, indent=2))
        return 1 if result.get("ok") is False else 0
    except (DriverError, chain.ChainError, OSError) as error:
        print(json.dumps({"error": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
