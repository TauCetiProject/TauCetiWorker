#!/usr/bin/env python3
"""The repair upload skips artifacts already in the bucket and otherwise uploads as `lake cache put-staged` does."""

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_queue import repair_publish, repair_upload

BASE = "https://acct.r2.cloudflarestorage.com/tauceti-repair"
REPO = "TauCetiProject/TauCeti"
TOOLCHAIN = "leanprover/lean4:v4.37.0-rc1"
REV = "b47acdf66e235638a04bd704795f331ebc623318"

# An S3 bucket in $BUCKET: HEAD answers 200 for an existing object, PUT writes it. Every request is
# appended to $BUCKET/requests.log; $FAIL_PUT names a URL whose PUT answers 403 and $FAIL_MAP makes
# every revision-map PUT answer 403.
CURL = r"""#!/usr/bin/env python3
import json, os, shutil, sys
from pathlib import Path
bucket = Path(os.environ["BUCKET"])
config = sys.stdin.read().splitlines()
assert "--config" in sys.argv and "-" in sys.argv, sys.argv
assert not any("secret" in a for a in sys.argv), "the key never reaches the command line"
def value(line):
    return json.loads(line.split("=", 1)[1].strip())
head = "head" in config
assert any(l.startswith("user =") and value(l) == "id:secret" for l in config)
assert 'aws-sigv4 = "aws:amz:auto:s3"' in config
content_type = next((value(l).removeprefix("Content-Type: ") for l in config if l.startswith("header =")), None)
transfers, upload = [], None
for line in config:
    if line.startswith("upload-file ="):
        upload = value(line)
    elif line.startswith("url ="):
        transfers.append((value(line), upload)); upload = None
log = (bucket / "requests.log").open("a")
for i, (url, path) in enumerate(transfers):
    key = bucket / url.split("/tauceti-repair/", 1)[1]
    if head:
        log.write(f"HEAD {url}\n")
        code = 200 if key.is_file() else 404
    else:
        log.write(f"PUT {url} {content_type}\n")
        if url == os.environ.get("FAIL_PUT") or ("/revisions/" in url and os.environ.get("FAIL_MAP")):
            code = 403
        else:
            key.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, key)
            code = 200
    print(json.dumps({"urlnum": i, "http_code": code, "exitcode": 0}))
"""


class Urls(unittest.TestCase):
    def test_urls_are_the_ones_lake_put_staged_uses(self):
        # As logged by `lake cache put-staged ... --repo TauCetiProject/TauCeti
        # --toolchain=leanprover/lean4:v4.37.0-rc1` on tauchonki2 (2026-11-25).
        self.assertEqual(
            repair_upload.artifact_url(BASE, REPO, "e297ab947a1dbdb5"),
            f"{BASE}/artifacts/TauCetiProject/TauCeti/e297ab947a1dbdb5.art",
        )
        self.assertEqual(
            repair_upload.revision_url(BASE, REPO, TOOLCHAIN, REV),
            f"{BASE}/revisions/TauCetiProject/TauCeti/tc/leanprover--lean4---v4.37.0-rc1/{REV}.jsonl",
        )

    def test_scope_components_are_escaped_as_lake_escapes_them(self):
        self.assertEqual(repair_upload.scope_path("a b/c+d~e_f.g-h"), "a%20b/c%2Bd~e_f.g-h")


class Upload(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bucket = self.root / "bucket"
        self.bucket.mkdir()
        curl = self.root / "curl"
        curl.write_text(CURL)
        curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
        self.env = dict(os.environ, CURL=str(curl), BUCKET=str(self.bucket))
        self.record = self.root / "cache" / "repair-uploaded.sqlite3"
        self.hashes = ["00aa00aa00aa00aa", "11bb11bb11bb11bb", "22cc22cc22cc22cc"]

    def stage(self, hashes=None) -> Path:
        stage = self.root / f"stage-{time.monotonic_ns()}"
        stage.mkdir()
        rows = ['"2026-03-17"']
        for i, h in enumerate(hashes or self.hashes):
            (stage / f"{h}.ltar").write_bytes(h.encode() * 10)
            rows.append(json.dumps([f"{i:016x}", f"{h}.ltar"]))
        rows.append(json.dumps(["ffffffffffffffff", f"{(hashes or self.hashes)[0]}.ltar"]))  # a shared output
        (stage / "outputs.jsonl").write_text("\n".join(rows) + "\n")
        repair_publish.validate_native_stage(stage)
        return stage

    def upload(self, stage: Path, record: Path | None = None, **env) -> dict:
        return repair_upload.upload(
            stage,
            base=BASE,
            repository=REPO,
            toolchain=TOOLCHAIN,
            revision=REV,
            key="id:secret",
            env={**self.env, **env},
            log_path=self.root / "logs" / "cache-put.log",
            timeout=60,
            record_path=self.record if record is None else record,
        )

    def requests(self) -> list[str]:
        path = self.bucket / "requests.log"
        lines = path.read_text().splitlines() if path.exists() else []
        path.unlink(missing_ok=True)
        return lines

    def art(self, h: str) -> str:
        return f"{BASE}/artifacts/{REPO}/{h}.art"

    def map_url(self) -> str:
        return f"{BASE}/revisions/{REPO}/tc/leanprover--lean4---v4.37.0-rc1/{REV}.jsonl"

    def test_a_cold_upload_puts_every_artifact_once_and_then_the_map(self):
        result = self.upload(self.stage())
        self.assertEqual(result["rc"], 0, (self.root / "logs" / "cache-put.log").read_text())
        self.assertEqual((result["recorded"], result["present"], result["uploaded"]), (0, 0, 3))
        requests = self.requests()
        self.assertEqual(requests[:3], [f"HEAD {self.art(h)}" for h in self.hashes])
        self.assertEqual(
            sorted(requests[3:6]), sorted(f"PUT {self.art(h)} {repair_upload.ARTIFACT_TYPE}" for h in self.hashes)
        )
        self.assertEqual(requests[6:], [f"PUT {self.map_url()} {repair_upload.MAP_TYPE}"], "the map comes last")
        stored = self.bucket / "revisions" / REPO / "tc" / "leanprover--lean4---v4.37.0-rc1" / f"{REV}.jsonl"
        self.assertEqual(stored.read_text().count("\n"), 5, "the full map, not just the uploaded artifacts")
        self.assertEqual(
            (self.bucket / "artifacts" / REPO / "11bb11bb11bb11bb.art").read_bytes(), b"11bb11bb11bb11bb" * 10
        )

    def test_the_host_record_skips_artifacts_uploaded_before_without_a_request(self):
        self.upload(self.stage())
        self.requests()
        result = self.upload(self.stage([*self.hashes, "33dd33dd33dd33dd"]))
        self.assertEqual(result["rc"], 0)
        self.assertEqual((result["recorded"], result["present"], result["uploaded"]), (3, 0, 1))
        self.assertEqual(
            self.requests(),
            [
                f"HEAD {self.art('33dd33dd33dd33dd')}",
                f"PUT {self.art('33dd33dd33dd33dd')} {repair_upload.ARTIFACT_TYPE}",
                f"PUT {self.map_url()} {repair_upload.MAP_TYPE}",
            ],
        )

    def test_without_a_record_artifacts_in_the_bucket_are_found_by_head(self):
        self.upload(self.stage(), record=Path("/nonexistent/forbidden/record.sqlite3"))
        self.requests()
        off = repair_publish.Settings(upload_record="off")
        self.assertIsNone(off.record_path())
        result = repair_upload.upload(
            self.stage(),
            base=BASE,
            repository=REPO,
            toolchain=TOOLCHAIN,
            revision=REV,
            key="id:secret",
            env=self.env,
            log_path=self.root / "logs" / "cache-put.log",
            timeout=60,
            record_path=None,
        )
        self.assertEqual(result["rc"], 0)
        self.assertEqual((result["recorded"], result["present"], result["uploaded"]), (0, 3, 0))
        self.assertEqual(
            self.requests(),
            [*(f"HEAD {self.art(h)}" for h in self.hashes), f"PUT {self.map_url()} {repair_upload.MAP_TYPE}"],
        )

    def test_an_expired_entry_is_checked_again(self):
        self.upload(self.stage())
        record = repair_upload.UploadRecord(self.record)
        urls = [self.art(h) for h in self.hashes]
        self.assertEqual(record.known(urls), set(urls))
        self.assertEqual(record.known(urls, now=time.time() + repair_upload.RECORD_TTL + 1), set())
        record.close()
        (self.bucket / "artifacts" / REPO / "00aa00aa00aa00aa.art").unlink()  # gone from the bucket
        self.requests()
        short = repair_upload.UploadRecord(self.record, ttl=0)
        short.prune(now=time.time() + 1)
        short.close()
        result = self.upload(self.stage())
        self.assertEqual((result["recorded"], result["present"], result["uploaded"]), (0, 2, 1))
        self.assertTrue((self.bucket / "artifacts" / REPO / "00aa00aa00aa00aa.art").is_file(), "uploaded again")

    def test_prune_keeps_the_newest_entries_up_to_the_limit(self):
        record = repair_upload.UploadRecord(self.record, limit=2)
        record.add(["a"], now=100.0)
        record.add(["b"], now=200.0)
        record.add(["c"], now=300.0)
        record.prune(now=300.0)
        self.assertEqual(record.known(["a", "b", "c"], now=300.0), {"b", "c"})
        record.close()

    def test_a_failed_artifact_upload_leaves_no_map_and_no_record_of_it(self):
        result = self.upload(self.stage(), FAIL_PUT=self.art("11bb11bb11bb11bb"))
        self.assertEqual(result["rc"], 1)
        self.assertEqual(result["uploaded"], 2)
        self.assertFalse((self.bucket / "revisions").exists(), "no map for a partial set")
        record = repair_upload.UploadRecord(self.record)
        self.assertEqual(
            record.known([self.art(h) for h in self.hashes]),
            {self.art("00aa00aa00aa00aa"), self.art("22cc22cc22cc22cc")},
        )
        record.close()
        self.assertIn("failed to upload", (self.root / "logs" / "cache-put.log").read_text())

    def test_a_failed_map_upload_fails_the_publication(self):
        result = self.upload(self.stage(), FAIL_MAP="1")
        self.assertEqual(result["rc"], 1)
        self.assertFalse((self.bucket / "revisions").exists())

    def test_the_record_is_shared_by_concurrent_publishers(self):
        import threading

        errors = []

        def run(i):
            record = repair_upload.UploadRecord(self.record)
            try:
                for n in range(50):
                    record.add([f"u{i}-{n}"])
                    record.known([f"u{i}-{n}"])
                    record.prune()
            finally:
                if record.error:
                    errors.append(record.error)
                record.close()

        threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        record = repair_upload.UploadRecord(self.record)
        self.assertEqual(len(record.known([f"u{i}-{n}" for i in range(4) for n in range(50)])), 200)
        record.close()

    def test_settings_choose_the_record(self):
        self.assertEqual(
            repair_publish.Settings.from_env({"TAUCETI_REPAIR_UPLOAD_RECORD": "/x/r.sqlite3"}).record_path(),
            Path("/x/r.sqlite3"),
        )
        self.assertIsNone(repair_publish.Settings.from_env({"TAUCETI_REPAIR_UPLOAD_RECORD": "0"}).record_path())
        settings = repair_publish.Settings.from_env({})
        self.assertEqual(settings.record_path(), repair_upload.default_record_path())
        self.assertEqual(repair_publish.Settings.from_json(settings.to_json()), settings)
        self.assertEqual(
            repair_upload.default_record_path({"HOME": "/home/op"}),
            Path("/home/op/.cache/tauceti-queue/repair-uploaded.sqlite3"),
        )


if __name__ == "__main__":
    unittest.main()
