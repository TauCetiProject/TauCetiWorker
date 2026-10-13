#!/usr/bin/env python3
"""Measure the production query catalog using a dedicated read-only installation.

The test installation owner performs every read, including discovery.
Results contain point charges, sizes, latency and overflow, never tokens or bodies.
Run on test by default. Failed/quota-limited samples remain visible as incomplete.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SERVICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE.parents[1]))


def requests_for(repo, prs, reactions=None):
    source = """import {build} from 'esbuild';
const built=await build({entryPoints:['src/catalog.ts'],bundle:true,platform:'node',format:'esm',write:false});
const module=await import('data:text/javascript;base64,'+Buffer.from(built.outputFiles[0].text).toString('base64'));
let data='';for await (const chunk of process.stdin) data+=chunk;
const {repository,prs,reactions}=JSON.parse(data),out=[];
for(const operation of ['open_prs','pr_delta','pr','issue_comments','review_comments','statuses']) {
 const args=operation==='statuses'?{sha:prs[0].head.sha}:['pr','issue_comments','review_comments'].includes(operation)?{number:prs[0].number}:{};
 for(const recipe of module.recipes(repository,{operation,args})) out.push({query_data:{repository,operation,args},name:operation+'_'+recipe.api,api:recipe.api,path:recipe.path,query:recipe.query,variables:recipe.variables});
}
for(const operation of ['issue_comments','review_threads','statuses']) for(const n of [1,10,25,50,100]) {
 if(prs.length<n) continue;
 const jobs=prs.slice(0,n).map(p=>({query:{operation,args:operation==='statuses'?{sha:p.head.sha}:{number:p.number}}}));
 const profiles=operation==='review_threads'?[3,5,10,20]:[20];
 for(const threadPage of profiles) out.push({name:operation+'_batch_'+n+(operation==='review_threads'?'_threads_'+threadPage:''),api:'graphql',path:'/graphql',operation,n,thread_page:threadPage,jobs,query_data:{repository,...jobs[0].query},profile:{threadPage},...module.batchQuery(repository,jobs,{allowLarge:true,threadPage})});
}
for(const n of [1,10,25,50,100]) if(reactions.length>=n) out.push({name:'reactions_batch_'+n,api:'graphql',path:'/graphql',operation:'reactions',n,jobs:reactions.slice(0,n).map((node_id,i)=>({node_id,query:{operation:'reactions',args:{id:String(i+1)}}})),query_data:{repository,operation:'reactions',args:{id:'1'}},...module.batchQuery(repository,reactions.slice(0,n).map(node_id=>({node_id,query:{operation:'reactions'}})),{allowLarge:true})});
process.stdout.write(JSON.stringify(out));"""
    r = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=SERVICE,
        input=json.dumps({"repository": repo, "prs": prs, "reactions": reactions or []}),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(r.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("production", "test"), default="test")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--filter", help="Regular expression selecting query profile names")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error("--repeats must be between 1 and 10")
    spec = importlib.util.spec_from_file_location("github_state_setup", SERVICE / "scripts/setup.py")
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    directory = Path.home() / (".config/tauceti-github-state" + ("-test" if args.environment == "test" else ""))
    installed = json.loads((directory / "installation.json").read_text())
    if args.environment != "test":
        raise SystemExit("Measurement reads use the test quota owner; production reference access stays disabled")
    from tauceti_worker.paths import ensure_ssl_cert_file

    ensure_ssl_cert_file()
    endpoint = "https://github-state-test.taucetiproject.org/admin/reference"
    headers = {
        "Authorization": "Bearer " + (directory / "admin-token").read_text().strip(),
        "User-Agent": "TauCetiWorker/1.0",
        "Content-Type": "application/json",
    }
    repo = installed["repository"]

    def fetch_owned(request):
        deadline = time.monotonic() + 45
        while True:
            with urllib.request.urlopen(request, timeout=20) as response:
                value = json.load(response)
            if value.get("category") != "capacity" or time.monotonic() >= deadline:
                return value
            time.sleep(max(0.05, min(1, (value.get("retry_at", time.time() * 1000 + 250) / 1000) - time.time())))

    def reference(operation, **query_args):
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(
                {"query": {"repository": repo, "operation": operation, "args": query_args}, "api": "core"}
            ).encode(),
            headers=headers,
        )
        value = fetch_owned(request)
        if "data" not in value:
            raise SystemExit("Test reference read unavailable: " + str(value.get("category", "upstream")))
        return value["data"]

    canonical = reference("open_prs")
    prs = [{"number": p["number"], "head": {"sha": p["headRefOid"]}} for p in canonical]
    if not prs:
        raise SystemExit("No open PRs available for measurements")
    reactions = []
    if not args.filter or "reaction" in args.filter:
        for pr in prs[:40]:
            comments = reference("review_comments", number=pr["number"])
            reactions = list(dict.fromkeys([*reactions, *(c["node_id"] for c in comments if c.get("node_id"))]))
            if len(reactions) >= 100:
                break
    recipes = requests_for(repo, prs, reactions)
    if args.filter:
        recipes = [r for r in recipes if re.search(args.filter, r["name"])]
    samples = []
    blocked = set()
    for recipe in recipes:
        if recipe["api"] in blocked:
            continue
        for repeat in range(args.repeats):
            body = {"query": recipe["query_data"], "api": recipe["api"]}
            if recipe.get("jobs"):
                body.update(jobs=recipe["jobs"], profile=recipe.get("profile", {}))
            request = urllib.request.Request(endpoint, headers=headers, data=json.dumps(body).encode())
            began = time.monotonic()
            try:
                value = fetch_owned(request)
            except (urllib.error.URLError, OSError) as error:
                samples.append({"name": recipe["name"], "repeat": repeat, "transport_error": type(error).__name__})
                continue
            sample = {
                "name": recipe["name"],
                "api": recipe["api"],
                "repeat": repeat,
                "status": value.get("status", 200 if "data" in value else 503),
                "points": value.get("points"),
                "seconds": value.get("seconds", round(value.get("elapsed_ms", 0) / 1000, 4)),
                "client_seconds": round(time.monotonic() - began, 4),
                "bytes": value.get("bytes"),
                "connection_overflows": value.get("connection_overflows", int(bool(value.get("cursor")))),
                "nested_overflows": value.get("nested_overflows", int(bool(value.get("continuation")))),
                "batch_items": recipe.get("n"),
                "query_sha256": value.get(
                    "query_sha256", hashlib.sha256((recipe.get("query") or recipe["path"]).encode()).hexdigest()
                ),
                "error_types": value.get("error_types", []),
                "remaining": value.get("remaining"),
                "limit": value.get("limit"),
                "deferred": value.get("deferred", False),
                "category": value.get("category"),
            }
            samples.append(sample)
            print(
                json.dumps(
                    {
                        k: sample[k]
                        for k in (
                            "name",
                            "points",
                            "seconds",
                            "connection_overflows",
                            "nested_overflows",
                            "error_types",
                            "deferred",
                        )
                    }
                ),
                flush=True,
            )
            if sample["deferred"] or sample["status"] in (403, 429):
                blocked.add(recipe["api"])
                break
    groups = {}
    for name in sorted({s["name"] for s in samples}):
        selected = [s for s in samples if s["name"] == name and s.get("status") == 200 and not s.get("error_types")]
        groups[name] = {
            "successful_samples": len(selected),
            "median_points": statistics.median(
                [s["points"] for s in selected if isinstance(s.get("points"), (int, float))]
            )
            if any(isinstance(s.get("points"), (int, float)) for s in selected)
            else None,
            "median_seconds": statistics.median([s["seconds"] for s in selected]) if selected else None,
            "max_connection_overflows": max([s["connection_overflows"] for s in selected], default=0),
            "max_nested_overflows": max([s["nested_overflows"] for s in selected], default=0),
        }
    result = {
        "version": 1,
        "sampled_at": time.time(),
        "environment": args.environment,
        "repository": repo,
        "sample_pr_numbers": [p["number"] for p in prs[:25]],
        "groups": groups,
        "samples": samples,
        "coverage": "recently updated open PRs; cost/overflow depend on history density; repeats are samples, not sustained load",
    }
    output = args.output or directory / "query-measurements.json"
    setup.private(output, json.dumps(result, indent=2))
    print("Measurements saved:", output)


if __name__ == "__main__":
    main()
