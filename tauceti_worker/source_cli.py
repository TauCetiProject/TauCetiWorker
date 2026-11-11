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


def execute(args, source):
    env = dict(
        os.environ,
        TAUCETI_SOURCE_BINDING=json.dumps(source.identity.record()),
        TAUCETI_FORGEJO_TOKEN_FILE=str(source.token_file),
        TAUCETI_FORGEJO_ORIGIN=source.repository.forge.url,
    )
    return subprocess.run(
        [sys.executable, "-m", "tauceti_worker.source_cli", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )


def load_source():
    from tauceti_tools.ci_candidate import RepositoryIdentity
    from tauceti_tools.forge import Forgejo

    from tauceti_queue.control_policy_file import read_control_policy

    from .forgejo_worker import ForgejoWorkerSource

    identity = RepositoryIdentity.parse(json.loads(os.environ["TAUCETI_SOURCE_BINDING"]))
    if identity.repository.forge.url != os.environ["TAUCETI_FORGEJO_ORIGIN"]:
        raise ValueError("Forgejo credential origin differs from the selected source")
    path = Path(os.environ["TAUCETI_FORGEJO_TOKEN_FILE"])
    token = read_control_policy(path).decode("ascii").strip()
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
    sha, branch = head["sha"], head["ref"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("PR checkout requires an exact commit")
    subprocess.run(["git", "check-ref-format", "--branch", branch], check=True, capture_output=True)
    subprocess.run(
        ["git", "fetch", "--no-tags", repository.clone_url, "refs/heads/" + branch],
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
    subprocess.run(["git", "config", "branch." + branch + ".merge", "refs/heads/" + branch], check=True)
    return 0


def api_command(command, source):
    """Native API escape hatch for the worker's existing comment helpers."""
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
        value = source.api.collection(endpoint) if options.get("paginate") else source.api.request("GET", endpoint)
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
            if command.args[0].lstrip("/").startswith("repos/" + source.repo + "/"):
                args.extend(["--hostname", urlsplit(source.repository.forge.url).netloc])
        configuration = os.environ.get("TAUCETI_SOURCE_GH_CONF")
        if not configuration:
            raise ValueError("Forgejo gh routing configuration unavailable")
        environment = dict(os.environ, TAUCETI_CONF=configuration)
        return run(args, environment=environment)
    except (ForgeError, ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        # Never print raw Git or HTTP diagnostics carrying request context.
        print(f"gh: source operation unavailable ({type(error).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
