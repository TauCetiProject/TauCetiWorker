"""Use the existing qualified Forgejo Git lease for native branch work."""

from __future__ import annotations

import os

from tauceti_tools.claims import ForgejoGitClaim

from .config import SourceUnavailable
from .constants import CLAIM_TTL_S
from .round import Claims


def lease(source, cfg, resource, *, ttl=CLAIM_TTL_S):
    return ForgejoGitClaim(
        source.identity,
        resource,
        cfg.wid + "-" + os.environ.get("TAUCETI_SOURCE_CLAIM_NONCE", str(os.getpid())),
        ttl,
        source=source.identity,
        client=source.api,
        token=source.api._token,
        credential_origin=source.repository.forge,
        cache_base=cfg.state / "claims",
    )


class SourceClaims(Claims):
    def __init__(self, cfg, ctx, source):
        super().__init__(cfg, ctx)
        self.source = source
        self.lease = None

    def begin_branch_work(self, pr, head, refname, owner, repo):
        from tauceti_tools.forge import RepositoryRef

        current = self.source.live_pull(pr)
        actor = self.source.authenticated_actor()
        if current is None or actor is None or current["head"]["sha"] != head:
            return False
        if current["user"]["id"] != int(actor.principal.native_id):
            return False
        repository = RepositoryRef(self.source.repository.forge, owner + "/" + repo)
        if current["head"]["repo"]["full_name"].casefold() != repository.name.casefold():
            return False
        self.lease = lease(self.source, self.cfg, f"branch/{pr}")
        result = self.lease.acquire()
        if result == 1:
            self.collisions += 1
            return False
        if result != 0:
            raise SourceUnavailable("Forgejo branch claim unavailable before starting repair")
        self.held = (f"branch/{pr}", self.source.repository.key)
        self.start_heartbeat(f"branch/{pr}", self.source.repo)
        self.ctx.add_cleanup(self.release)
        os.environ.update(
            TAUCETI_PUSH_REF=refname,
            TAUCETI_PUSH_EXPECT=head,
            TAUCETI_PUSH_REMOTE=repository.clone_url,
            TAUCETI_CLAIM_SH=str(self.cfg.state / "bin/claim-source"),
            TAUCETI_CLAIM_KEY=f"branch/{pr}",
            TAUCETI_CLAIM_REPO=self.source.repository.key,
        )
        return True

    def begin_target_work(self, area, slug):
        # Author leases stay in the shared GitHub claim namespace on every source.
        from .worker_sources import shared_github_environment

        with shared_github_environment():
            return super().begin_target_work(area, slug)

    def release(self):
        self.stop_heartbeat()
        lease, self.lease = self.lease, None
        if self.held and self.held[0].startswith("author/"):
            from .config import log
            from .worker_sources import shared_github_environment

            try:
                with shared_github_environment():
                    super().release()
            except SourceUnavailable:
                log(f"claim {self.held[0]}: not released (shared GitHub credentials unavailable); it expires")
        elif lease is not None:
            lease.release()
        self.held = None
        for key in ("TAUCETI_CLAIM_KEY", "TAUCETI_CLAIM_REPO"):
            os.environ.pop(key, None)


def main():
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    from .source_cli import load_source

    if len(sys.argv) < 3 or sys.argv[1] not in {"acquire", "renew", "holds", "release"}:
        return 2
    if sys.argv[2].startswith("author/"):
        from tauceti_tools.claims import GitClaim, script_path

        from .constants import TAUCETI
        from .worker_sources import shared_github_environment

        with shared_github_environment():
            ttl = int(sys.argv[3]) if len(sys.argv) > 3 else CLAIM_TTL_S
            claim = GitClaim(os.environ.get("CLAIM_REPO", TAUCETI), sys.argv[2], os.environ["TAUCETI_WORKER_ID"], ttl)
            claim.script = str(script_path())
            return claim.command(sys.argv[1])
    source = load_source()
    cfg = SimpleNamespace(state=Path(os.environ["TAUCETI_SOURCE_STATE"]), wid=os.environ["TAUCETI_WORKER_ID"])
    ttl = int(sys.argv[3]) if len(sys.argv) > 3 else CLAIM_TTL_S
    claim = lease(source, cfg, sys.argv[2], ttl=ttl)
    return claim.command(sys.argv[1])


if __name__ == "__main__":
    raise SystemExit(main())
