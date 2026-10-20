#!/usr/bin/env python3
"""--account asserts WHICH ChatGPT account a Codex round will spend under, and stops the round when it
is the wrong one. Nothing else can answer that question: `codex login status` prints only "Logged in
using ChatGPT" (no email), and the model cannot introspect its own account — so the identity is read
out of $CODEX_HOME/auth.json, which is the file `codex` itself authenticates with.

The properties that matter, and why each is a property rather than an implementation detail:
  - identity is CORRELATED per token, never unioned field-by-field: an account id from one token beside
    an email from another names an account that need not exist, and --account would pass for it;
  - tokens that disagree are reported, not resolved (a half-written credential must not be confidently
    labelled as one account or the other);
  - an API-key credential has no account identity at all, and says so instead of reporting a mismatch;
  - a credential codex would not even use — $CODEX_API_KEY / $CODEX_ACCESS_TOKEN outrank auth.json —
    is refused rather than certified, because a pass must mean the checked account is the paying one;
  - malformed shapes fail closed instead of raising: this runs in a doctor row and a preflight;
  - a mismatch NEVER switches accounts — it fails with instructions, which is the whole contract;
  - the message points at the operator's REAL ~/.codex under an isolated home, not at the mirror the
    worker reads, and names $CODEX_HOME in the commands so a copy-paste cannot revoke another account.

Exit 0 = every case agrees; 1 = a mismatch.
"""

import base64
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


def check_in(name, needle, haystack):
    global fails
    ok = needle in (haystack or "")
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: {'found' if ok else 'MISSING'} {needle!r}")


def jwt(claims: dict) -> str:
    """A JWT with a real payload and junk header/signature — the reader must never verify a signature
    (it reads identity, not authorization), so unsigned fixtures are the honest test input."""
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


def auth_json(*, email="a@example.com", acct="acct-1", plan="pro", id_email=None, id_acct=None):
    """A ChatGPT-mode auth.json in codex's real shape. id_* override the id_token's claims so the two
    tokens can be made to disagree."""
    return {
        "OPENAI_API_KEY": None,
        "auth_mode": "chatgpt",
        "tokens": {
            "account_id": acct,
            "access_token": jwt(
                {
                    "https://api.openai.com/auth": {"chatgpt_account_id": acct, "chatgpt_plan_type": plan},
                    "https://api.openai.com/profile": {"email": email},
                }
            ),
            "id_token": jwt(
                {
                    "email": id_email or email,
                    "https://api.openai.com/auth": {"chatgpt_account_id": id_acct or acct},
                }
            ),
            "refresh_token": "rt",
        },
    }


tmp = tempfile.mkdtemp(prefix="tauceti-account-")
home = Path(tmp) / "home"
codex = home / ".codex"
codex.mkdir(parents=True)
_saved_env = os.environ.get("CODEX_HOME")
os.environ.pop("CODEX_HOME", None)  # codex_dir() must resolve <home>/.codex, not the runner's own


class Cfg:
    home = home
    quota_cache = Path(tmp) / "quota-cache"  # never written here (no usage fetch), but Quota reads it
    checkout = Path(tmp) / "checkout"  # host authoring checkout; a project config layer would live here


def write(auth):
    (codex / "auth.json").write_text(json.dumps(auth))


def q():
    return tc.Quota(Cfg)


try:
    # --- reading the identity ---------------------------------------------------------------------
    write(auth_json(email="kim@example.com", acct="acct-kim", plan="pro"))
    a = q().codex_account()
    check("email from the access token", a.email, "kim@example.com")
    check("account id", a.account_id, "acct-kim")
    check("plan", a.plan, "pro")
    check("no conflict when the tokens agree", a.conflict, False)
    check("describes an account", a.describes_an_account, True)
    check("describe() is operator-readable", a.describe(), "kim@example.com (plan: pro)")

    # Matching accepts either identifier, and is case-insensitive: an operator typing their own email
    # in a different case has not asked for a different account.
    check("matches the email", a.matches("kim@example.com"), True)
    check("matches the email, any case", a.matches("Kim@Example.COM"), True)
    check("matches the account id", a.matches("acct-kim"), True)
    check("does not match another account", a.matches("other@example.com"), False)
    check("empty string matches nothing", a.matches("  "), False)

    # The id_token alone must be enough: codex refreshes tokens, and a credential written by some other
    # tool need not carry the same fields.
    only_id = auth_json(email="kim@example.com", acct="acct-kim")
    del only_id["tokens"]["access_token"]
    del only_id["tokens"]["account_id"]
    write(only_id)
    check("email from the id token alone", q().codex_account().email, "kim@example.com")

    # --- disagreement is reported, not resolved ----------------------------------------------------
    write(auth_json(email="kim@example.com", id_email="someone-else@example.com"))
    check("conflicting emails -> conflict", q().codex_account().conflict, True)
    write(auth_json(acct="acct-kim", id_acct="acct-other"))
    check("conflicting account ids -> conflict", q().codex_account().conflict, True)
    write(auth_json(email="kim@example.com", id_email="KIM@EXAMPLE.COM"))
    check("case-only email difference is NOT a conflict", q().codex_account().conflict, False)

    # A truncated/garbage token degrades to "unknown", never raises: the pacer and the dashboard read
    # this on every refresh, and a crash there would take down paths that have nothing to do with it.
    broken = auth_json()
    broken["tokens"]["access_token"] = "not-a-jwt"
    broken["tokens"]["id_token"] = "also.not.valid"
    write(broken)
    a = q().codex_account()
    check("unparseable tokens do not raise", a.email, None)
    check("unparseable tokens still yield the account id", a.account_id, "acct-1")

    # --- the enforcement contract ------------------------------------------------------------------
    write(auth_json(email="kim@example.com", acct="acct-kim", plan="pro"))
    check("requested account present -> no problem", q().codex_account_problem("kim@example.com"), None)
    check("matched by id -> no problem", q().codex_account_problem("acct-kim"), None)

    msg = q().codex_account_problem("other@example.com")
    check("wrong account -> a problem", msg is None, False)
    check_in("names the requested account", "other@example.com", msg)
    check_in("names the account actually authenticated", "kim@example.com", msg)
    check_in("states that TauCeti will not switch", "will not switch accounts", msg)
    check_in("gives the switch command", "codex login", msg)
    # The account picker is the step that actually fails for someone with several ChatGPT accounts:
    # codex's OAuth flow sends no prompt=select_account, so it completes as whoever the browser is.
    check_in("warns there is no account picker", "no account picker", msg)
    check_in("warns that logout revokes the other session", "revokes", msg)
    check_in("offers an isolated CODEX_HOME", "CODEX_HOME=", msg)

    # An API key is a legitimate way to run codex, but carries no ChatGPT account identity. Reporting a
    # MISMATCH there would name an account that does not exist; say what is actually wrong instead.
    write({"OPENAI_API_KEY": "sk-test", "auth_mode": "apikey", "tokens": {}})
    a = q().codex_account()
    check("api key -> describes no account", a.describes_an_account, False)
    msg = q().codex_account_problem("kim@example.com")
    check_in("api key -> explains there is no identity to check", "no ChatGPT account identity", msg)
    check_in("api key -> still says how to get one", "codex login", msg)

    # No credential at all.
    (codex / "auth.json").unlink()
    check("no credential -> codex_account() is None", q().codex_account(), None)
    check_in("no credential -> says so", "no Codex credential", q().codex_account_problem("kim@example.com"))

    # --- the message must point at the OPERATOR's file, not the worker's mirror --------------------
    # Under an isolated home the worker reads a copy; mirror_creds() overwrites it from the original
    # every round, so re-authenticating into the copy would be silently undone.
    real = Path(tmp) / "real-home" / ".codex"
    real.mkdir(parents=True)
    write(auth_json(email="kim@example.com"))
    (codex / ".tauceti-creds-source").write_text(str(real))
    msg = q().codex_account_problem("other@example.com")
    check_in("isolated home -> names the real credential source", str(real), msg)
    check("isolated home -> does NOT name the mirror", str(codex / "auth.json") in msg, False)

    # The stale-mirror false alarm: the operator switches their real ~/.codex to the account they want,
    # but the isolated worker still holds the OLD mirrored copy (run_round only re-mirrors later). If the
    # check read the mirror, it would tell them to switch to the account they had just switched to —
    # which would teach them to distrust the check. It must sync from the source before deciding.
    (real / "auth.json").write_text(json.dumps(auth_json(email="switched-to@example.com", acct="acct-new")))
    write(auth_json(email="stale@example.com", acct="acct-old"))
    check(
        "stale mirror + freshly switched source -> no false alarm",
        q().codex_account_problem("switched-to@example.com"),
        None,
    )
    check(
        "stale mirror -> the OLD account is not accepted either",
        q().codex_account_problem("stale@example.com") is None,
        False,
    )
    (codex / ".tauceti-creds-source").unlink()

    # --- identity must be CORRELATED, not unioned field-by-field -----------------------------------
    # An account id from one token beside an email from another describes an account that need not
    # exist. Here the access token (what actually spends) is acct-A with no email, and the id token
    # carries an email but no account id: nothing ties that address to acct-A, so it must not be
    # accepted as acct-A's, or --account would pass for an account the credential does not spend under.
    uncorrelated = {
        "auth_mode": "chatgpt",
        "tokens": {
            "account_id": "acct-A",
            "access_token": jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-A"}}),
            "id_token": jwt({"email": "b@example.com"}),
        },
    }
    write(uncorrelated)
    a = q().codex_account()
    check("uncorrelated email is not adopted", a.email, None)
    check("the spending account id is still reported", a.account_id, "acct-A")
    check(
        "--account with the uncorrelated email is REFUSED",
        q().codex_account_problem("b@example.com") is None,
        False,
    )
    check("--account with the real account id still passes", q().codex_account_problem("acct-A"), None)

    # An email IS adopted from a token whose own account id agrees with the authoritative one.
    write(auth_json(email="kim@example.com", acct="acct-kim"))
    check("corroborated email is adopted", q().codex_account().email, "kim@example.com")

    # --- malformed credential shapes fail closed, never crash --------------------------------------
    # Valid JSON of the wrong shape is what a half-written file or a future codex schema looks like.
    # A traceback out of a doctor row or a preflight check would be worse than "unknown account".
    for label, blob in [
        ("top-level list", "[]"),
        ("tokens is a list", '{"tokens": []}'),
        ("numeric access_token", '{"tokens": {"access_token": 123}}'),
        ("null tokens", '{"tokens": null}'),
        (
            "claim namespace is a string",
            '{"tokens": {"access_token": "%s"}}' % jwt({"https://api.openai.com/auth": "x"}),
        ),
    ]:
        (codex / "auth.json").write_text(blob)
        try:
            acct = q().codex_account()
            problem = q().codex_account_problem("kim@example.com")
            ok = acct is not None and not acct.describes_an_account and problem is not None
            check(f"malformed ({label}) -> fails closed, no crash", ok, True)
        except Exception as e:
            check(f"malformed ({label}) -> fails closed, no crash", f"CRASH {type(e).__name__}: {e}", True)

    # Unreadable-but-present must not be reported as "no credential": the remedy differs, and logging
    # in over a corrupt file could destroy a live session.
    (codex / "auth.json").write_text("{not json")
    check_in(
        "corrupt credential is distinguished from absent",
        "could not be read",
        q().codex_account_problem("kim@example.com"),
    )

    # --- an environment credential outranks the file we check --------------------------------------
    # codex reads CODEX_API_KEY / CODEX_ACCESS_TOKEN ahead of auth.json, and TauCeti's launcher clears
    # only OPENAI_API_KEY. Certifying the file while one of these is set would be a false pass.
    write(auth_json(email="kim@example.com", acct="acct-kim"))
    for var in ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
        os.environ[var] = "sk-something"
        try:
            check_in(f"${var} set -> refuses to certify", var, q().codex_account_problem("kim@example.com"))
        finally:
            os.environ.pop(var, None)
    check("with the env clear, the same credential passes", q().codex_account_problem("kim@example.com"), None)

    # --- a non-file credential store means codex does not read auth.json at all --------------------
    # Verified against codex 0.146.0: with cli_auth_credentials_store = "ephemeral", `codex login
    # status` reports "Not logged in" while a perfectly good auth.json sits on disk; with "keyring" it
    # reads the keyring and does not fall back to the file. A file left over from before the switch
    # would therefore let TauCeti certify an account codex has stopped using — a fail-OPEN, which is the
    # one direction this check must never fail. Unset resolves to `file` (codex doctor reports
    # "auth storage mode = File"), which is why the default path needs no config at all.
    write(auth_json(email="kim@example.com", acct="acct-kim"))
    check("no config.toml -> verifiable", q().codex_account_problem("kim@example.com"), None)
    for mode, verifiable in [("file", True), ("keyring", False), ("ephemeral", False), ("auto", False)]:
        (codex / "config.toml").write_text(f'cli_auth_credentials_store = "{mode}"\n')
        problem = q().codex_account_problem("kim@example.com")
        check(f'store "{mode}" -> {"verifiable" if verifiable else "REFUSED"}', problem is None, verifiable)
        if not verifiable:
            check_in(f'store "{mode}" names the setting', "cli_auth_credentials_store", problem)
            check_in(f'store "{mode}" gives the one-line fix', 'cli_auth_credentials_store = "file"', problem)

    # A future variant must fail closed, not be silently trusted as if it were `file`.
    (codex / "config.toml").write_text('cli_auth_credentials_store = "some-future-store"\n')
    check("unknown store -> REFUSED", q().codex_account_problem("kim@example.com") is None, False)

    # A config we cannot READ is not the same as one that says `file`. "codex would fail to start on it
    # anyway" does not hold: a permission error can clear before launch, and the entitlement probe runs
    # with --ignore-user-config. None means "verified file-backed", so not-knowing must never return it.
    (codex / "config.toml").write_text("this is not valid toml [[[\n")
    check_in(
        "unreadable config -> REFUSED, not assumed file",
        "could not be read",
        q().codex_account_problem("kim@example.com"),
    )
    (codex / "config.toml").write_text("cli_auth_credentials_store = 7\n")
    check("non-string store value -> REFUSED", q().codex_account_problem("kim@example.com") is None, False)
    # codex itself rejects these, so normalising them to `file` would invent an agreement it does not have.
    for weird in ('" FILE "', '"File"', '""'):
        (codex / "config.toml").write_text(f"cli_auth_credentials_store = {weird}\n")
        check(
            f"store {weird} -> REFUSED (only exact `file` passes)",
            q().codex_account_problem("kim@example.com") is None,
            False,
        )
    (codex / "config.toml").unlink()

    # A trusted project config layer OVERRIDES the user one — verified against codex 0.146.0: a trusted
    # <project>/.codex/config.toml setting ephemeral makes doctor report Ephemeral even with the user
    # config on file. Host rounds run codex with cwd=checkout, so that layer reaches them. Resolving
    # codex's trust rules here would mean reimplementing them, so the file's presence is disqualifying.
    check("no project config -> verifiable", q().codex_account_problem("kim@example.com"), None)
    (Cfg.checkout / ".codex").mkdir(parents=True, exist_ok=True)
    (Cfg.checkout / ".codex" / "config.toml").write_text("# even an empty one is not resolvable here\n")
    problem = q().codex_account_problem("kim@example.com")
    check("project config layer present -> REFUSED", problem is None, False)
    check_in("names the project config that disqualified it", ".codex/config.toml", problem)
    shutil.rmtree(Cfg.checkout)
    check("project config removed -> verifiable again", q().codex_account_problem("kim@example.com"), None)
    from tauceti_worker.work_recovery import RecordError

    with patch("tauceti_worker.quota.active_checkout", side_effect=RecordError("invalid workspace selector")):
        check("invalid selector does not block credential-store admission", q()._codex_credentials_store(), None)
        check("invalid selector does not block account admission", q().codex_account_problem("kim@example.com"), None)
        (codex / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        check_in(
            "invalid selector still reports real user credential policy",
            "keyring",
            q().codex_account_problem("kim@example.com"),
        )
        (codex / "config.toml").unlink()

    # The PACER inherits the same assumption: it reads auth.json for the token it measures usage with.
    # Under a non-file store a leftover file would have it fetch and cache one account's usage while the
    # agent spends another. No --account is involved, so this guard has to live below it.
    (codex / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
    prov = q().codex()
    check("pacer reports codex unavailable under a non-file store", prov.available, False)
    check_in("pacer states the reason", "credential store", prov.error)
    (codex / "config.toml").unlink()

    # --- the gate ----------------------------------------------------------------------------------
    gate = tc.work_units.raise_on_account_mismatch
    write(auth_json(email="kim@example.com", acct="acct-kim"))
    gate(Cfg, "kim@example.com", "codex", "test")  # must not raise
    print("[OK ] matching account: the gate passes")

    raised = ""
    try:
        gate(Cfg, "other@example.com", "codex", "preflight")
    except tc.Die as e:
        raised = str(e)
    check("wrong account -> Die (exit, not a retry loop)", raised.startswith("preflight:"), True)
    check_in("the Die carries the actionable message", "codex login", raised)

    # No --account is the default and must stay entirely inert.
    gate(Cfg, None, "codex", "test")
    print("[OK ] no --account: the gate is inert")

    # --account is Codex-only; a non-codex round must not be failed by a check it cannot perform.
    gate(Cfg, "other@example.com", "claude", "test")
    print("[OK ] non-codex round: the codex credential check does not apply")

    # --- the loop driver must FORWARD --account to the child that actually spends -------------------
    # The loop builds its child argv explicitly rather than inheriting it, so an omission here is a
    # check that silently does not run for the whole loop — the mode most people use.
    import inspect

    loop_src = inspect.getsource(tc.loop.cmd_loop)
    check("cmd_loop forwards --account to the round child", '"--account", account' in loop_src, True)

    # And the flag must survive an argv round-trip, so the child parses back what the parent sent.
    ns = tc.cli.build_parser().parse_args(["_round", "--agent", "codex", "--account", "kim@example.com"])
    check("the round subcommand accepts --account", getattr(ns, "account", None), "kim@example.com")
finally:
    if _saved_env is not None:
        os.environ["CODEX_HOME"] = _saved_env

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
