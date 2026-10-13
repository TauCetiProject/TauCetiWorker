"""Shared query fields and REST routes; both the mirror and direct fallback consume this catalog."""

from __future__ import annotations

import json
from pathlib import Path

CATALOG = json.loads(Path(__file__).with_suffix(".json").read_text())


def prefer_rest():
    from .github_transport import observed_budget

    budgets = observed_budget()
    gql = budgets.get("graphql")
    rest = budgets.get("core")
    return bool(
        gql and (gql[0] == 0 and (not rest or rest[0] > 0) or rest and rest[0] > 0 and gql[0] / 5000 < rest[0] / 5000)
    )


def rest_collection(client, operation, **args):
    route = CATALOG["paths"][operation].format(**args)
    rows = []
    # Page locally so each retry resumes one read; a failed page cannot become an empty history.
    for page in range(1, 10001):
        p = client._gh(["api", f"/repos/{client.repo}{route}?per_page=100&page={page}"])
        if p.returncode:
            return None
        try:
            batch = json.loads(p.stdout)
        except ValueError:
            return None
        if not isinstance(batch, list):
            return None
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    return None
