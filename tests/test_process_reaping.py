"""Offline tests for Chromium process reaping and Docker zombie prevention (#68).

Run: python tests/test_process_reaping.py
"""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from engine import MuseEngine, MuseGenerationError  # noqa: E402


class FakeProc:
    def __init__(self, *, alive=True, timeout_on_first_wait=False):
        self.alive = alive
        self.timeout_on_first_wait = timeout_on_first_wait
        self.calls: list[tuple] = []

    def poll(self):
        self.calls.append(("poll",))
        return None if self.alive else 0

    def terminate(self):
        self.calls.append(("terminate",))

    def kill(self):
        self.calls.append(("kill",))
        self.alive = False

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        if self.timeout_on_first_wait:
            self.timeout_on_first_wait = False
            raise subprocess.TimeoutExpired(cmd="chromium", timeout=timeout)
        self.alive = False
        return 0


class FakeLog:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class ProcessReapingTests(unittest.TestCase):
    def _make_engine(self, tmpdir: str) -> MuseEngine:
        cfg = Config(base_dir=tmpdir, profile_root=tmpdir,
                     home_dir=tmpdir, chromium="chromium")
        return MuseEngine(cfg)

    def test_stop_reaps_process_after_kill(self):
        with tempfile.TemporaryDirectory(prefix="muse-reap-") as tmpdir:
            eng = self._make_engine(tmpdir)
            proc = FakeProc(alive=True, timeout_on_first_wait=True)
            log_file = FakeLog()
            eng.proc = proc
            eng._log = log_file

            eng.stop()

            self.assertIsNone(eng.proc)
            self.assertIsNone(eng._log)
            self.assertTrue(log_file.closed)
            kill_idx = proc.calls.index(("kill",))
            waits_after_kill = [c for c in proc.calls[kill_idx + 1:] if c[0] == "wait"]
            self.assertEqual(len(waits_after_kill), 1,
                             f"expected wait() after kill(), got {proc.calls}")

    def test_stop_reaps_already_exited_process_and_closes_log(self):
        with tempfile.TemporaryDirectory(prefix="muse-reap-") as tmpdir:
            eng = self._make_engine(tmpdir)
            proc = FakeProc(alive=False)
            log_file = FakeLog()
            eng.proc = proc
            eng._log = log_file

            eng.stop()

            self.assertIsNone(eng.proc)
            self.assertTrue(log_file.closed)
            self.assertEqual(proc.calls, [("poll",)])

    def test_failed_start_reaps_spawned_process(self):
        with tempfile.TemporaryDirectory(prefix="muse-reap-") as tmpdir:
            eng = self._make_engine(tmpdir)
            proc = FakeProc(alive=True, timeout_on_first_wait=True)
            with patch("engine.http_json", side_effect=OSError("no cdp")), \
                    patch("engine.subprocess.Popen", return_value=proc), \
                    patch("engine.time.sleep", return_value=None):
                with self.assertRaises(MuseGenerationError):
                    eng.start()

            self.assertIsNone(eng.proc)
            self.assertIsNone(eng._log)
            kill_idx = proc.calls.index(("kill",))
            waits_after_kill = [c for c in proc.calls[kill_idx + 1:] if c[0] == "wait"]
            self.assertEqual(len(waits_after_kill), 1)

    def test_docker_files_configure_pid1_reaper(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(compose, r"(?m)^\s+init:\s*true\s*$")
        self.assertIn("tini", dockerfile)
        self.assertIn('ENTRYPOINT ["/usr/bin/tini", "--"]', dockerfile)


if __name__ == "__main__":
    unittest.main()
