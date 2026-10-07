"""Command interface to the shared local ledger."""

import json
from datetime import UTC, datetime

from .budget import Budget, BudgetError, money


def add_budget_parser(sub):
    parser = sub.add_parser("budget", help="shared local Claude API grants and append-only history")
    amounts = parser.add_mutually_exclusive_group()
    amounts.add_argument("--grant", metavar="USD", help="add a one-off grant")
    amounts.add_argument("--set-grant", metavar="USD", help="set current balance through a logged adjustment")
    parser.add_argument("--grant-rate", metavar="USD_PER_HOUR", help="set automatic replenishment; 0 disables it")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--log", action="store_true", help="show the append-only transaction history")
    parser.add_argument("--reconcile", metavar="INVOCATION", help="record or correct a session's cost")
    parser.add_argument("--cost", metavar="USD", help="reconciled cost")
    parser.add_argument("--note", default="", help="explanation required for reconciliation or ledger repair")
    parser.add_argument("--repair-ledger", action="store_true", help="archive a torn write and audit recovery")


def cmd_budget(args):
    budget = Budget()
    changes = any(v is not None for v in (args.grant, args.set_grant, args.grant_rate))
    if sum((changes, bool(args.reconcile), args.repair_ledger, args.log)) > 1:
        raise BudgetError("choose one of grant changes, reconciliation, repair, or history")
    if args.cost is not None and not args.reconcile:
        raise BudgetError("--cost requires --reconcile")
    if changes:
        snapshot = budget.configure(grant=args.grant, set_grant=args.set_grant, rate=args.grant_rate)
    elif args.reconcile:
        if args.cost is None:
            raise BudgetError("--reconcile requires --cost and --note")
        budget.reconcile(args.reconcile, args.cost, args.note)
        snapshot = budget.snapshot()
    elif args.repair_ledger:
        snapshot = budget.repair(args.note)
    elif args.log:
        with budget.locked():
            records = budget.records()
        for record in records:
            print(
                json.dumps(record)
                if args.json
                else f"{record['seq']:6} {record['timestamp']:.3f} {json.dumps(record['events'])}"
            )
        return 0
    else:
        snapshot = budget.snapshot()
    if args.json:
        print(json.dumps(snapshot, indent=2))
        return 0
    print("Local budget (USD, estimated session costs)")
    for label, field in (
        ("Balance", "balance"),
        ("Grant rate / hour", "grant_rate"),
        ("Recorded spend", "spent"),
        ("Pending estimates", "pending"),
        ("Available for admissions", "available"),
    ):
        print(f"  {label:26} ${money(snapshot[field]):.2f}")
    print(f"  Active / unresolved         {len(snapshot['active_sessions'])} / {len(snapshot['unresolved'])}")
    for session in snapshot["active_sessions"]:
        print(f"    {session['id']} {session['worker']} {session['status']}")
    for waiting in snapshot["waiting"]:
        print(f"  {waiting['worker']}: {waiting['reason']} (estimate ${money(waiting['estimate']):.2f})")
        if waiting.get("funding_at"):
            print("    Estimated funding at " + datetime.fromtimestamp(waiting["funding_at"], UTC).isoformat())
    if snapshot["balance_zero_at"]:
        print("  Balance reaches zero       " + datetime.fromtimestamp(snapshot["balance_zero_at"], UTC).isoformat())
    if snapshot["clock_backwards"]:
        print("  Clock moved backward; accrual waits for the persisted watermark")
    print(f"  Ledger                     {snapshot['ledger']}")
    return 0
