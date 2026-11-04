#!/usr/bin/env python3
"""Lean pressure gating and compiler handoff; no installed toolchain is modified."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/tauceti-lean"
loader = importlib.machinery.SourceFileLoader("lean_pressure", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
gate = importlib.util.module_from_spec(spec)
loader.exec_module(gate)


class LeanPressure(unittest.TestCase):
    def test_optional_observer_permissions_do_not_fail_compilation(self):
        with patch.object(gate, "Path", side_effect=PermissionError("inaccessible observer")):
            self.assertIsNone(gate.Trace().client)

    def test_language_server_does_not_pollute_compilation_timings(self):
        with (
            patch.object(gate, "real_lean", return_value=Path("/real/lean")),
            patch.object(gate, "Trace") as trace,
            patch.object(gate, "wait_for_capacity") as wait,
            patch.object(gate, "exec_lean", side_effect=SystemExit(0)),
        ):
            with self.assertRaises(SystemExit):
                gate.main(["--server"])
            trace.assert_not_called()
            wait.assert_called_once()

    def setUp(self):
        for name in ("SIGPIPE", "SIGXFSZ"):
            if number := getattr(gate.signal, name, None):
                self.addCleanup(gate.signal.signal, number, gate.signal.getsignal(number))

    def test_non_linux_alias_forwards_probes_and_compilation_without_procfs(self):
        with (
            patch.object(gate.sys, "platform", "darwin"),
            patch.object(gate, "real_lean", return_value=Path("/real/lean")),
            patch.object(gate, "wait_for_capacity") as wait,
            patch.object(gate.os, "execv", side_effect=SystemExit(0)) as execute,
        ):
            for args in (["--print-prefix"], ["Main.lean"]):
                with self.assertRaises(SystemExit):
                    gate.main(args)
                execute.assert_called_with("/real/lean", ["/real/lean", *args])
            wait.assert_not_called()

    def test_headroom_is_more_than_twice_worst_rss_and_floor_is_explicit(self):
        with patch.object(gate, "WORST_FILE_RSS_BYTES", 8 * 1024**3):
            for gib in (64, 128, 256):
                total = gib * 1024**3
                threshold = gate.memory_limit(total)
                self.assertGreater((100 - threshold) * total / 100, 2 * gate.WORST_FILE_RSS_BYTES)
            self.assertEqual(gate.memory_limit(16 * 1024**3), 60)

    def test_cpu_uses_recent_busy_ticks(self):
        self.assertEqual(gate.cpu_percent((100, 10), (200, 20)), 90)
        self.assertEqual(gate.cpu_percent((100, 10), (200, 110)), 0)
        with self.assertRaises(ValueError):
            gate.cpu_percent((100, 10), (100, 10))

    def test_memory_respects_ancestor_pressure_and_reclaims_clean_inactive_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = root / "proc"
            (proc / "self").mkdir(parents=True)
            (proc / "meminfo").write_text("MemTotal: 67108864 kB\nMemAvailable: 50331648 kB\n")
            (proc / "self/cgroup").write_text("0::/user/worker\n")
            cg = root / "sys/fs/cgroup"
            (cg / "user/worker").mkdir(parents=True)
            (cg / "memory.max").write_text("max\n")
            gib = 1024**3
            for name, maximum, used, cold, dirty in (
                ("user", 8, 7, 0, 0),
                ("user/worker", 6, 5, 2, 0.5),
            ):
                group = cg / name
                (group / "memory.max").write_text(str(maximum * gib))
                (group / "memory.current").write_text(str(used * gib))
                (group / "memory.stat").write_text(
                    f"inactive_file {cold * gib}\nfile {cold * gib}\nfile_dirty {int(dirty * gib)}\n"
                )
            # /proc values and cgroup files come from an isolated fixture.
            with patch.object(gate, "Path", side_effect=lambda path: root / str(path).lstrip("/")):
                total, available = gate.memory()
            self.assertEqual(total, 6 * gib)
            self.assertEqual(available, gib)
            (cg / "user/memory.stat").write_text(f"inactive_file 0\nfile {3 * gib}\nfile_dirty 0\n")
            with patch.object(gate, "Path", side_effect=lambda path: root / str(path).lstrip("/")):
                _, available = gate.memory()
            self.assertEqual(available, int(2.5 * gib))
            (cg / "user/worker/memory.high").write_text(str(4 * gib))
            with patch.object(gate, "Path", side_effect=lambda path: root / str(path).lstrip("/")):
                self.assertEqual(gate.memory(), (6 * gib, int(2.5 * gib)))
            (proc / "self/cgroup").write_text("1:memory:/legacy\n")
            with patch.object(gate, "Path", side_effect=lambda path: root / str(path).lstrip("/")):
                self.assertEqual(gate.memory(), (64 * gib, 48 * gib))

    def test_waits_for_either_pressure_source_then_exec_can_proceed(self):
        # First interval is CPU-bound; the next is memory-bound; the third is safe.
        with (
            patch.object(gate, "cpu_sample", side_effect=[(0, 0), (100, 5), (200, 50), (300, 100)]),
            patch.object(gate, "memory", side_effect=[(100, 50), (100, 5), (100, 50)]),
            patch.object(gate, "memory_limit", return_value=80),
            patch.object(gate.random, "uniform", side_effect=[0.8, 2.1, 4.7]),
            patch.object(gate.time, "sleep") as sleep,
            patch.dict(os.environ, TAUCETI_LEAN_PRESSURE_DEBUG="1"),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            gate.wait_for_capacity()
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.8, 2.1, 4.7])
        self.assertIn("waiting", stderr.getvalue())
        self.assertIn("starting Lean", stderr.getvalue())

    def test_probe_never_waits_and_compile_preserves_every_argument(self):
        with (
            patch.object(gate, "real_lean", return_value=Path("/real/lean")),
            patch.object(gate, "wait_for_capacity") as wait,
            patch.object(gate.os, "execv", side_effect=SystemExit(23)) as execute,
        ):
            with self.assertRaises(SystemExit):
                gate.main(["--githash"])
            wait.assert_not_called()
            args = ["File With Spaces.lean", "--setup", "config.json", "-o", "out.olean", "--json"]
            with self.assertRaises(SystemExit):
                gate.main(args)
            wait.assert_called_once()
            execute.assert_called_with("/real/lean", ["/real/lean", *args])

    def test_missing_telemetry_does_not_start_lean(self):
        with (
            patch.object(gate, "real_lean", return_value=Path("/real/lean")),
            patch.object(gate, "wait_for_capacity", side_effect=OSError("missing telemetry")),
            patch.object(gate.os, "execv") as execute,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(gate.main(["Main.lean"]), 2)
            execute.assert_not_called()

    def test_watchdog_install_keeps_original_unchanged_and_survives_cleared_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "compiler"
            original.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\nexit 23\n')
            original.chmod(0o755)
            before = original.read_bytes()
            watchdog = root / "watchdog"
            (watchdog / "bin").mkdir(parents=True)
            binary = watchdog / "bin/lean-real"
            binary.symlink_to(original)
            gate.install_watchdog(binary)
            gate.install_watchdog(binary)
            self.assertEqual(original.read_bytes(), before)
            self.assertEqual(json.loads((watchdog / gate.MARKER).read_text())["lean"], str(original))
            for args, rc in [(["--print-prefix"], 0), (["--githash"], 23)]:
                result = subprocess.run(
                    [str(binary), *args], env={"PATH": os.environ["PATH"]}, text=True, capture_output=True
                )
                self.assertEqual(result.returncode, rc, result.stderr)
                self.assertEqual(result.stdout.strip(), str(watchdog) if rc == 0 else "--githash")
            # A later helper revision must not replace an already-installed helper
            # or save it over the actual compiler, regardless of header wording.
            previous = binary.read_text().replace('"""Wait for spare RAM/CPU', '"""Earlier helper')
            binary.write_text(previous)
            gate.install_watchdog(binary)
            self.assertEqual(binary.read_text(), previous)
            self.assertEqual(original.read_bytes(), before)

    def test_saved_wrapper_cycle_fails_before_replacing_the_compiler(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            binary = root / "bin/lean"
            original = root / "bin/lean-unthrottled"
            for path in (binary, original):
                path.write_bytes(SCRIPT.read_bytes())
            (root / gate.MARKER).write_text(json.dumps(dict(lean=str(original))))
            with self.assertRaisesRegex(ValueError, "cycle"):
                gate.unwrapped(binary)

    def test_overlay_keeps_libraries_and_compiler_on_the_wrapper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            toolchain = root / "original"
            (toolchain / "bin").mkdir(parents=True)
            (toolchain / "lib").mkdir()
            lean = toolchain / "bin/lean"
            lean.write_text("real compiler")
            original_metadata = json.dumps(dict(lean=str(lean), installed=True))
            (toolchain / gate.MARKER).write_text(original_metadata)
            with patch.dict(os.environ, {"XDG_CACHE_HOME": str(root / "cache")}):
                result = gate.overlay(lean)
                self.assertEqual(gate.overlay(lean), result)
            self.assertEqual((result / "lib").resolve(), toolchain / "lib")
            self.assertEqual((toolchain / gate.MARKER).read_text(), original_metadata)
            self.assertEqual((result / "bin/lean").read_bytes(), SCRIPT.read_bytes())
            probe = subprocess.run(
                [str(result / "bin/lean"), "--print-prefix"],
                env={"PATH": os.environ["PATH"]},
                text=True,
                capture_output=True,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertEqual(probe.stdout.strip(), str(result))

    def test_without_elan_path_discovery_skips_a_materialized_wrapper_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alias = root / "alias"
            actual = root / "actual"
            alias.mkdir()
            actual.mkdir()
            (alias / "lean").write_bytes(SCRIPT.read_bytes())
            (alias / "lean").chmod(0o755)
            (actual / "lean").write_text("#!/bin/sh\nexit 0\n")
            (actual / "lean").chmod(0o755)
            with (
                patch.object(gate.shutil, "which", return_value=None),
                patch.object(gate.os, "get_exec_path", return_value=[str(alias), str(actual)]),
            ):
                self.assertEqual(gate.real_lean(), actual / "lean")


if __name__ == "__main__":
    unittest.main()
