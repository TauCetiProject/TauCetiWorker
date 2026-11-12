"""Primary/follower operations for the existing mixed queue and publication outbox."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from tauceti_tools.forge import Forgejo

from . import chain
from .forgejo_git import ForgejoGitSource
from .mixed_profile import credential
from .stage1_mirror import MainMirror


class MainWriter(ForgejoGitSource):
    def __init__(self, identity, clone, token_file, actor_id):
        token = credential(token_file)
        super().__init__(identity, Forgejo(identity.repository.forge, token=token), clone, token=token)
        self.actor_id = actor_id

    def check_actor(self):
        actor = self.client.current_user()
        if type(actor.get("id")) is not int or actor["id"] != self.actor_id:
            raise ValueError("Forgejo main writer differs from its configured native actor ID")


def configure(registry):
    value = registry.profile.raw.get("mirror")
    if isinstance(value, dict) and "approved_caller" in value:
        raise ValueError("remove mirror.approved_caller; mirror checks use the trusted GitHub main base")
    if not isinstance(value, dict) or set(value) != {
        "forgejo_token_file",
        "forgejo_writer_id",
        "github_credentials_dir",
    }:
        raise ValueError("mixed admission requires separate main writer and mirror roles")
    writer = value["forgejo_writer_id"]
    if (
        type(writer) is not int
        or not 0 < writer < 2**63
        or writer in registry.profile.reviewers | {registry.profile.publisher}
    ):
        raise ValueError("Forgejo main writer must be a separate configured native actor")
    for key in ("forgejo_token_file", "github_credentials_dir"):
        if not isinstance(value[key], str) or not Path(value[key]).is_absolute():
            raise ValueError("mirror credentials require explicit absolute paths")
    if registry.branch != "main":
        raise ValueError("mixed primary changes currently require protected main on both hosts")
    registry.native_main = MainWriter(registry.profile.forgejo, registry.clone, value["forgejo_token_file"], writer)
    registry.mirror = MainMirror(
        replace(registry.ci.ledger.policy, execution=registry.profile.github, execution_branch="main"),
        registry.clone,
        value["github_credentials_dir"],
    )


def tips(registry):
    """Fetch by bound source into disjoint local refs; never infer origin from a number."""
    found = {}
    for name, source, repository in (
        ("github", registry.github, registry.repository),
        ("forgejo", registry.native, registry.profile.forgejo.repository.name),
    ):
        ref = "refs/tauceti-queue/source-main/" + name
        source.fetch_refs(repository, [f"+refs/heads/{registry.branch}:{ref}"])
        found[name] = chain.resolve_commit(registry.clone, ref)
    return found


def preflight(registry, expected, candidate):
    registry.check_native()
    registry.check_writer()
    registry.check_github()
    if registry.primary == "forgejo":
        check_actor = getattr(registry.native_main, "check_actor", None)
        if check_actor is not None:
            check_actor()
        current = tips(registry)
        if current != {"github": expected, "forgejo": expected}:
            raise ValueError("repair main synchronization before the next Forgejo-primary landing")
        registry.mirror.preflight(registry.native, expected, candidate, current["github"])


def synchronize(registry):
    registry.check_native()
    registry.check_writer()
    registry.check_github()
    current = tips(registry)
    primary = current[registry.primary]
    if registry.primary == "forgejo":
        registry.mirror.publish(registry.native, primary, current["github"])
    elif current["forgejo"] != primary:
        old = current["forgejo"]
        if not chain.is_ancestor(registry.clone, old, primary):
            raise ValueError("Forgejo follower diverged or is ahead; preserve both histories")
        check_actor = getattr(registry.native_main, "check_actor", None)
        if check_actor is not None:
            check_actor()
        result = registry.native_main.fast_forward(
            registry.profile.forgejo.repository.name, registry.branch, old, primary
        )
        if not result.ok:
            raise ValueError("Forgejo follower refused its expected-old fast-forward")
    confirmed = tips(registry)
    if confirmed != {"github": primary, "forgejo": primary}:
        raise ValueError("main synchronization has not yet been confirmed; reconcile from the current primary")
    result = {"primary": registry.primary, "generation": registry.generation, "main": primary}
    registry.store.set_setting("mixed:last_sync", result, "queue mirror", registry.controller.clock())
    return result
