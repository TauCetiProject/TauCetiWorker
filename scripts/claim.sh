#!/usr/bin/env bash
# claim.sh — optional, cooperative task de-contention for Tau Ceti agents.
#
# A claim is a custom git ref `refs/tauceti-claims/<key>` in the work repo, pointing at an orphan
# commit whose message is a JSON lease {owner, expires_at, ...}. Acquire/renew/takeover/release are
# all done with ONE atomic GitHub primitive — `git push --force-with-lease=<ref>:[<oid>]`:
#   * expected EMPTY  → create-only (succeeds iff the ref does not exist)
#   * expected <oid>  → succeeds iff the ref still points at <oid> (compare-and-swap)
# (Validated against real GitHub: a second create is rejected "stale info"; a CAS with the wrong
# old-oid is rejected; with the right old-oid it forces. So races have exactly one winner.)
#
# This is [COOP] in the coordination contract: honoring claims only avoids DUPLICATE work. It is
# NOT the safety mechanism — the branch-level `--force-with-lease` in git-safe-push is. A claim can
# expire (TTL) so a dead holder never blocks anyone; takeover of an expired claim is itself a CAS,
# so two reclaimers can't both win.
#
# Usage:
#   claim.sh acquire <key> [ttl_seconds]   # 0 acquired (or renewed mine) · 1 held by another · 2 error
#   claim.sh renew   <key> [ttl_seconds]   # 0 renewed · 1 lost (taken over / gone) · 2 error
#   claim.sh release <key>                 # 0 released (or wasn't mine / already gone)
#   claim.sh holds   <key>                 # 0 I hold it and it's unexpired · 1 otherwise
#   claim.sh read    <key>                 # print the lease JSON (empty if unclaimed)
#   claim.sh list    [--full]              # list live claim refs (--full fetches each lease)
#   claim.sh gc                            # CAS-delete expired claims
#
# Env: CLAIM_REPO (default TauCetiProject/TauCeti), TAUCETI_WORKER_ID (default host-pid),
#      CLAIM_TTL (default 1500), CLAIM_GITDIR_BASE (per-repo scratch parent),
#      CLAIM_GITDIR (explicit scratch object store override).
set -uo pipefail

REPO="${CLAIM_REPO:-${TAUCETI_REPO:-TauCetiProject/TauCeti}}"
URL="https://github.com/$REPO"
WID="${TAUCETI_WORKER_ID:-$(hostname)-$$}"
DEFAULT_TTL="${CLAIM_TTL:-1500}"
GITDIR="${CLAIM_GITDIR:-${CLAIM_GITDIR_BASE:-$HOME/.cache/tauceti-claims}/${REPO//\//__}.git}"
NS="refs/tauceti-claims"
export GIT_AUTHOR_NAME="tauceti-claim" GIT_AUTHOR_EMAIL="claim@tauceti.invalid"
export GIT_COMMITTER_NAME="tauceti-claim" GIT_COMMITTER_EMAIL="claim@tauceti.invalid"

now() { date +%s; }
ref_of() { printf '%s/%s' "$NS" "$1"; }

# A private scratch repo just for building + pushing claim objects (no work-repo checkout needed).
ensure_repo() {
    if [[ ! -d "$GITDIR" ]]; then
        mkdir -p "$(dirname "$GITDIR")"
        git init -q --bare "$GITDIR"
    fi
    git -C "$GITDIR" remote get-url origin >/dev/null 2>&1 \
        || git -C "$GITDIR" remote add origin "$URL"
    git -C "$GITDIR" remote set-url origin "$URL"
}
g() { git -C "$GITDIR" "$@"; }

empty_tree() { g hash-object -t tree -w /dev/null; }

# remote_oid REF — current oid of REF on origin, or "" if absent.
remote_oid() { g ls-remote origin "$1" 2>/dev/null | awk 'NR==1{print $1}'; }

# lease_json OID — the JSON lease stored in the orphan commit OID (fetched on demand).
lease_json() {
    local oid="$1"
    g cat-file -e "$oid" 2>/dev/null || g fetch -q --no-tags origin "$2" 2>/dev/null || true
    g cat-file commit "$oid" 2>/dev/null | sed '1,/^$/d'
}

# build_oid JSON — write an orphan commit (empty tree) whose message is JSON; print its oid.
build_oid() { printf '%s' "$1" | g commit-tree "$(empty_tree)"; }

# payload KEY EXPIRES — the lease JSON for a claim I'm taking now.
payload() {
    local n; n=$(now)
    jq -nc --arg s "tauceti-claim/v1" --arg o "$WID" --arg h "$(hostname)" \
        --argjson pid "$$" --argjson aq "$n" --argjson ex "$2" --arg res "$1" \
        --arg observed "${CLAIM_OBSERVED_OID:-}" \
        '{schema:$s, owner:$o, host:$h, pid:$pid, acquired_at:$aq, expires_at:$ex,
          resource:$res, observed_branch_oid:($observed | if . == "" then null else . end)}'
}

# push_cas REF EXPECTED NEWOID — CAS push (EXPECTED="" ⇒ create-only). 0 win, 1 lost/rejected.
push_cas() {
    local out
    out=$(g push --force-with-lease="$1:$2" origin "$3:$1" 2>&1)
    if [[ $? -eq 0 ]]; then return 0; fi
    grep -qiE 'rejected|stale info|failed to push' <<<"$out" && return 1
    echo "claim: unexpected push error on $1: $out" >&2; return 2
}
push_delete() { g push --force-with-lease="$1:$2" origin ":$1" >/dev/null 2>&1; }

cmd_acquire() {
    local key="$1" ttl="${2:-$DEFAULT_TTL}" ref cur js owner exp n
    ref=$(ref_of "$key"); n=$(now); ensure_repo
    cur=$(remote_oid "$ref")
    if [[ -n "$cur" ]]; then
        js=$(lease_json "$cur" "$ref"); owner=$(jq -r '.owner // ""' <<<"$js" 2>/dev/null)
        exp=$(jq -r '.expires_at // 0' <<<"$js" 2>/dev/null)
        if [[ "$owner" != "$WID" && "$exp" =~ ^[0-9]+$ && "$exp" -gt "$n" ]]; then
            return 1   # someone else holds a live lease
        fi
        # mine (renew) or expired (takeover): CAS against the observed oid
        local oid; oid=$(build_oid "$(payload "$key" "$((n+ttl))")")
        push_cas "$ref" "$cur" "$oid"; return $?
    fi
    local oid; oid=$(build_oid "$(payload "$key" "$((n+ttl))")")
    push_cas "$ref" "" "$oid"   # create-only
}

cmd_renew() {
    local key="$1" ttl="${2:-$DEFAULT_TTL}" ref cur js owner n
    ref=$(ref_of "$key"); n=$(now); ensure_repo
    cur=$(remote_oid "$ref"); [[ -z "$cur" ]] && return 1
    js=$(lease_json "$cur" "$ref"); owner=$(jq -r '.owner // ""' <<<"$js" 2>/dev/null)
    [[ "$owner" == "$WID" ]] || return 1   # lost / taken over
    local oid; oid=$(build_oid "$(payload "$key" "$((n+ttl))")")
    push_cas "$ref" "$cur" "$oid"
}

cmd_release() {
    local key="$1" ref cur js owner
    ref=$(ref_of "$key"); ensure_repo
    cur=$(remote_oid "$ref"); [[ -z "$cur" ]] && return 0
    js=$(lease_json "$cur" "$ref"); owner=$(jq -r '.owner // ""' <<<"$js" 2>/dev/null)
    [[ "$owner" == "$WID" ]] || return 0   # not mine — leave it
    push_delete "$ref" "$cur"; return 0
}

cmd_holds() {
    local key="$1" ref cur js owner exp n
    ref=$(ref_of "$key"); n=$(now); ensure_repo
    cur=$(remote_oid "$ref"); [[ -z "$cur" ]] && return 1
    js=$(lease_json "$cur" "$ref"); owner=$(jq -r '.owner // ""' <<<"$js" 2>/dev/null)
    exp=$(jq -r '.expires_at // 0' <<<"$js" 2>/dev/null)
    [[ "$owner" == "$WID" && "$exp" =~ ^[0-9]+$ && "$exp" -gt "$n" ]]
}

cmd_read() {
    local ref cur; ref=$(ref_of "$1"); ensure_repo
    cur=$(remote_oid "$ref"); [[ -z "$cur" ]] && return 0
    lease_json "$cur" "$ref"
}

cmd_list() {
    ensure_repo
    g ls-remote origin "$NS/*" 2>/dev/null | while read -r oid ref; do
        local key="${ref#"$NS"/}"
        if [[ "${1:-}" == "--full" ]]; then
            printf '%s\t%s\n' "$key" "$(lease_json "$oid" "$ref" | tr -d '\n')"
        else
            printf '%s\t%s\n' "$key" "$oid"
        fi
    done
}

cmd_gc() {
    local n; n=$(now); ensure_repo
    g ls-remote origin "$NS/*" 2>/dev/null | while read -r oid ref; do
        local js exp; js=$(lease_json "$oid" "$ref"); exp=$(jq -r '.expires_at // 0' <<<"$js" 2>/dev/null)
        if [[ "$exp" =~ ^[0-9]+$ && "$exp" -le "$n" ]]; then
            push_delete "$ref" "$oid" && echo "gc: deleted expired $ref" >&2
        fi
    done
}

cmd="${1:-}"; shift || true
case "$cmd" in
    acquire) cmd_acquire "$@";;
    renew)   cmd_renew "$@";;
    release) cmd_release "$@";;
    holds)   cmd_holds "$@";;
    read)    cmd_read "$@";;
    list)    cmd_list "$@";;
    gc)      cmd_gc "$@";;
    *) echo "usage: claim.sh {acquire|renew|release|holds|read|list|gc} <key> [ttl]" >&2; exit 64;;
esac
