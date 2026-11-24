"""Route familiar gh operations inside a selected Forgejo worker round."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from tauceti_tools.forge import ForgeError, Unsupported


def execute(args, source, *, cwd=None):
    env = dict(
        os.environ,
        TAUCETI_SOURCE_BINDING=json.dumps(source.identity.record()),
        TAUCETI_FORGEJO_TOKEN_FILE=str(source.token_file),
        TAUCETI_FORGEJO_ORIGIN=source.repository.forge.url,
    )
    # -P: the working directory may be an untrusted PR checkout, which must
    # never shadow this runtime's own modules on sys.path.
    return subprocess.run(
        [sys.executable, "-P", "-m", "tauceti_worker.source_cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )


def load_source():
    from tauceti_tools.ci_candidate import RepositoryIdentity
    from tauceti_tools.forge import Forgejo
    from tauceti_tools.git_credentials import read_private_token

    from .forgejo_worker import ForgejoWorkerSource

    identity = RepositoryIdentity.parse(json.loads(os.environ["TAUCETI_SOURCE_BINDING"]))
    if identity.repository.forge.url != os.environ["TAUCETI_FORGEJO_ORIGIN"]:
        raise ValueError("Forgejo credential origin differs from the selected source")
    path = Path(os.environ["TAUCETI_FORGEJO_TOKEN_FILE"])
    token = read_private_token(path)
    source = ForgejoWorkerSource(identity, Forgejo(identity.repository.forge, token=token), token_file=path)
    source.validate_binding()
    expected = os.environ.get("TAUCETI_FORGEJO_USER_ID")
    if expected is not None:
        actor = source.authenticated_actor()
        if actor is None or actor.principal.native_id != expected:
            raise ValueError("Forgejo actor differs from the selected worker")
    return source


def checkout(args, source):
    parser = argparse.ArgumentParser(prog="gh pr checkout")
    parser.add_argument("number", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--branch", "-b")
    parser.add_argument("--repo", "-R")
    opts = parser.parse_args(args)
    if opts.repo and opts.repo not in (
        source.repo,
        source.repository.url,
        urlsplit(source.repository.forge.url).netloc + "/" + source.repo,
    ):
        raise ValueError("checkout repository differs from selected source")
    row = source.api.pull(source.repository, opts.number)
    head = row["head"]
    from tauceti_tools.forge import RepositoryRef

    repository = RepositoryRef(source.repository.forge, head["repo"]["full_name"])
    sha, remote_branch = head["sha"], head["ref"]

    # Round worktrees share one repository, so each worker names its own local branch.
    branch = opts.branch if opts.branch is not None else remote_branch
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("PR checkout requires an exact commit")
    for name in {remote_branch, branch}:
        subprocess.run(["git", "check-ref-format", "--branch", name], check=True, capture_output=True)
    subprocess.run(
        ["git", "fetch", "--no-tags", repository.clone_url, "refs/heads/" + remote_branch],
        check=True,
        capture_output=True,
        timeout=60,
    )
    fetched = subprocess.check_output(["git", "rev-parse", "FETCH_HEAD"], text=True).strip()
    if fetched != sha:
        raise ValueError("Forgejo PR head changed during checkout")
    command = ["git", "checkout", "-B", branch, sha]
    if opts.force:
        command.insert(2, "--force")
    subprocess.run(command, check=True, capture_output=True)
    # gh checkout normally establishes the PR branch's tracking destination.
    subprocess.run(["git", "config", "branch." + branch + ".remote", repository.clone_url], check=True)
    subprocess.run(["git", "config", "branch." + branch + ".merge", "refs/heads/" + remote_branch], check=True)
    return 0


# PR fields the issue index carries. Forgejo renders an issue row without the git work (head,
# base and diff stat) it does for each row of a pull listing.
INDEX_FIELDS = frozenset(
    {"number", "title", "body", "url", "state", "labels", "author", "createdAt", "updatedAt", "closedAt", "mergedAt"}
)


def list_command(command, source):
    """`gh pr list --json` without a native pull listing, when its fields allow.

    Agents list every open PR (often with `--limit 1000`) and the merged ones to avoid duplicate
    work, and each native pull listing costs Forgejo seconds of git work per page. Open PRs come
    from the host's shared survey inventory; other states, or fields it lacks, from the issue
    index, where "merged" includes PRs the queue landed. Filters, other fields and plain-text
    output go to Tools as before (None).
    """
    from tauceti_tools.forge import Incomplete
    from tauceti_tools.forge_read import project, pull_record

    from .github import BORS_MERGED_PREFIX

    options = command.options
    if command.args or "json" not in options or set(options) - {"json", "jq", "limit", "state", "repo"}:
        return None
    host_repo = urlsplit(source.repository.forge.url).netloc + "/" + source.repo
    # Only an explicitly selected source repository: without one, Tools picks the repository of
    # the working directory's checkout, which may be another.
    if (options.get("repo") or os.environ.get("GH_REPO")) not in (source.repo, source.repository.url, host_repo):
        return None
    fields, state, limit = options["json"].split(","), options.get("state", "open"), int(options.get("limit", "30"))
    if state == "open" and set(fields) <= set(source.inventory_fields):
        from .source_survey import discovery_inventory

        source.shared_inventory = True
        # The inventory keeps PR-number order (a corrected row is reinserted by number), which is
        # gh's newest-first order: only imported PRs can be created out of number order, and the
        # native listing of every open PR showed none (21 Nov: no inversion among 1,272).
        rows = discovery_inventory(source)
        if rows is None:
            raise ForgeError("Forgejo open inventory unavailable")
    elif state in ("open", "closed", "merged", "all") and set(fields) <= INDEX_FIELDS:
        from urllib.parse import urlencode

        rows, seen = [], set()
        query = urlencode({"type": "pulls", "state": "closed" if state == "merged" else state})
        for batch in source.api.pages(f"repos/{source.repo}/issues?{query}"):
            for issue in batch:
                pull = issue.get("pull_request") if isinstance(issue, dict) else None
                if not isinstance(pull, dict) or not isinstance(pull.get("merged"), bool):
                    raise Incomplete("issue index did not identify a PR")
                # The queue lands a PR by squashing it and closing it unmerged under the Bors
                # prefix (as ForgejoWorkerSource.merged_prs counts landings), so natively merged
                # PRs are rare here and "merged" means landed. Landed PRs are not "closed".
                landed = pull["merged"] or (
                    issue.get("state") == "closed" and str(issue.get("title") or "").startswith(BORS_MERGED_PREFIX)
                )
                if issue["number"] in seen or state in ("closed", "merged") and landed != (state == "merged"):
                    continue
                seen.add(issue["number"])
                merged_at = pull.get("merged_at") or (issue.get("closed_at") if landed else None)
                rows.append(pull_record({**issue, "merged": landed, "merged_at": merged_at}, source.repository, fields))
                if len(rows) == limit:
                    break
            if len(rows) == limit:
                break
    else:
        return None
    rendered = json.dumps([project(row, fields) for row in rows[:limit]], ensure_ascii=False) + "\n"
    if options.get("jq"):
        if options["jq"].startswith("-"):
            return None  # Tools explains this
        result = subprocess.run(["jq", "-r", options["jq"]], input=rendered, text=True, capture_output=True)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode
    sys.stdout.write(rendered)
    return 0


def api_command(command, source):
    """Only worker-specific thread reads and comment writes bypass shared routing."""
    options = command.options
    endpoint = command.args[0].lstrip("/")
    prefix = "repos/" + source.repo
    if endpoint != "user" and not (endpoint == prefix or endpoint.startswith(prefix + "/")):
        return None
    if options.get("hostname") and options["hostname"] != urlsplit(source.repository.forge.url).netloc:
        return None
    data = {}
    for flag in ("raw_field", "field"):
        for item in options.get(flag, []):
            key, separator, value = item.partition("=")
            if not separator:
                raise ValueError("API fields require key=value")
            if flag == "field" and value.startswith("@"):
                value = Path(value[1:]).read_text()
            data[key] = value
    if options.get("input"):
        with open(options["input"]) as stream:
            data.update(json.load(stream))
    method = options.get("method", "POST" if data else "GET").upper()
    path = endpoint.split("?", 1)[0]
    comments = re.fullmatch(re.escape(prefix) + r"/pulls/([1-9][0-9]*)/comments", path)
    reply = re.fullmatch(re.escape(prefix) + r"/pulls/([1-9][0-9]*)/comments/([1-9][0-9]*)/replies", path)
    if comments and method == "GET":
        value = source.review_comments(int(comments[1]))
        if value is None:
            raise ForgeError("Forgejo review comments unavailable")
    elif reply and method == "POST":
        from .source_publication import reply_to_finding

        value = reply_to_finding(source, int(reply[1]), int(reply[2]), data["body"])
    elif method == "GET":
        # Tools owns REST read projection, bounds and synthetic pagination. In
        # particular, native issue comments do not accept page/limit parameters.
        return None
    elif re.fullmatch(re.escape(prefix) + r"/issues(?:/[1-9][0-9]*/comments|/comments/[1-9][0-9]*)", path):
        if method not in ("POST", "PATCH", "DELETE"):
            raise Unsupported("unsupported comment operation")
        value = source.api.request(method, endpoint, data or None)
    else:
        return None
    rendered = json.dumps(value)
    if options.get("jq"):
        result = subprocess.run(["jq", "-r", options["jq"]], input=rendered, text=True, capture_output=True)
        if result.returncode:
            raise ValueError("jq filter failed")
        rendered = result.stdout.rstrip()
    if options.get("include"):
        print("HTTP/1.1 200 OK\n")
    if not options.get("silent"):
        print(rendered)
    return 0


def main(argv=None):
    from tauceti_tools.forge_cli import run
    from tauceti_tools.forge_command import parse_command

    args = list(sys.argv[1:] if argv is None else argv)
    try:
        source = load_source()
        if args[:2] == ["pr", "checkout"]:
            return checkout(args[2:], source)
        command = parse_command(args)
        if command.key == ("pr", "list"):
            result = list_command(command, source)
            if result is not None:
                return result
        if command.group == "api":
            result = api_command(command, source)
            if result is not None:
                return result
        # Qualify content explicitly. Shared roadmap/tool and CI destinations keep
        # their independently configured GitHub identities.
        host_repo = urlsplit(source.repository.forge.url).netloc + "/" + source.repo
        for index, value in enumerate(args[:-1]):
            if value in ("--repo", "-R") and args[index + 1] == source.repo:
                args[index + 1] = host_repo
        if command.group == "api":
            endpoint = command.args[0].lstrip("/").split("?", 1)[0]
            prefix = "repos/" + source.repo
            if not command.options.get("hostname") and (endpoint == prefix or endpoint.startswith(prefix + "/")):
                args.extend(["--hostname", urlsplit(source.repository.forge.url).netloc])
        configuration = os.environ.get("TAUCETI_SOURCE_GH_CONF")
        if not configuration:
            raise ValueError("Forgejo gh routing configuration unavailable")
        environment = dict(os.environ, TAUCETI_CONF=configuration)
        return run(args, environment=environment)
    except Unsupported as error:
        # Adapter policy diagnostics are safe, local explanations (for example
        # unsupported fields), unlike raw transport/request failure context.
        print(f"gh: {error}", file=sys.stderr)
        return 1
    except (ForgeError, ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        # Never print raw Git or HTTP diagnostics carrying request context.
        print(f"gh: source operation unavailable ({type(error).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
