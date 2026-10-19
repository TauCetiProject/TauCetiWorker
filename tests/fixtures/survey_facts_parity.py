"""Deterministic inputs/expected facts for the TypeScript differential test."""

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tauceti_worker.survey_facts import in_scope, summarize, target_focuses


def cases():
    rng = random.Random(256)
    output = []
    for i in range(240):
        issues = []
        for j in range(rng.randrange(8)):
            payload = rng.choice(
                [
                    None,
                    [],
                    "bad",
                    {},
                    {
                        "head_sha": str(i),
                        "runs": [
                            {
                                "provider": "claude",
                                "verdict": rng.choice(["approve", "error", "request_changes"]),
                                "tok": {"in": 100},
                            }
                        ],
                        "states": {"reuse": "blocking_request"},
                        "submitted_by": "author",
                    },
                ]
            )
            body = (
                rng.choice(["plain", "<!--tauceti-scoreboard-->"])
                + "\n<!--tauceti-meta:v1 "
                + json.dumps(payload)
                + "-->"
            )
            if rng.choice([True, False]):
                body += (
                    "\n<!--tauceti-review-in-progress "
                    + json.dumps(
                        {
                            "head": str(i),
                            "expires_at": rng.choice([100, 200, True, "bad", 100.0, 1e3, 1e30]),
                            "providers": rng.choice([["claude", 7], [], "bad"]),
                        }
                    )
                    + "-->"
                )
            issues.append(
                {"id": str(j + 1), "body": body, "updated_at": str(rng.randrange(3)), "user": {"login": "peer"}}
            )
        issues.append(
            {
                "id": "99",
                "body": "Merge-queue recovery for head `x`\r\n<!--tauceti-rebase:v1 " + "a" * 40 + "-->\r\n",
                "user": {"login": "tauceti-review-bot[bot]"},
                "updated_at": "4",
            }
        )
        for k, prefix in enumerate(("", "> ", "Quoted\n")):
            issues.append(
                {
                    "id": str(200 + k),
                    "body": prefix + "<!--tauceti-blocked:v1 " + "a" * 40 + "-->\nEvidence",
                    "user": {"login": "Author"},
                    "updated_at": "5",
                }
            )
        reviews = [{"id": "18446744073709551600", "body": "<!--tauceti-rubric:reuse -->"}]
        reviews.append({"id": None, "in_reply_to_id": "18446744073709551600", "body": "contest"})
        for j in range(rng.randrange(6)):
            reviews.append(
                {
                    "id": str(18446744073709551601 + j),
                    "in_reply_to_id": "18446744073709551600",
                    "body": rng.choice(
                        ["contest", "<!--tauceti-reply:reuse:through:1-->", "<!--tauceti-rubric:reuse -->"]
                    ),
                    "created_at": str(j),
                }
            )
        labels = rng.choice(
            [
                [],
                ["roadmap/PDE"],
                ["roadmap/Topology"],
                ["roadmap/PDE", "roadmap/Unknown"],
                ["roadmap/Unknown"],
                ["roadmap/none"],
            ]
        )
        body = "<!--tauceti-target:v1 " + json.dumps({"focus": rng.choice(["PDE", "Topology", "auto"])}) + "-->"
        row = {
            "labels": [{"name": label} for label in labels],
            "body": body,
            "headRefName": rng.choice(["roadmap/work", "feature"]),
        }
        scope = rng.choice(["PDE", "Topology", "any"])
        skip = rng.choice([[], ["PDE"]])
        expected = summarize(issues, reviews)
        if expected["contest"]:
            expected["contest"]["id"] = str(expected["contest"]["id"])
        output.append(
            {
                "issues": issues,
                "reviews": reviews,
                "row": row,
                "scope": scope,
                "skip": skip,
                "expected": expected,
                "focuses": target_focuses(body),
                "included": in_scope(labels, target_focuses(body), row["headRefName"], scope, skip),
            }
        )
    return output


if __name__ == "__main__":
    print(json.dumps(cases()))
