"""Tests for rtsp-record. The ffmpeg binary is mocked through PATH so the
tests run on a headless box and never touch the network."""

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "rtsp-record"


def _load_module():
    loader = SourceFileLoader("rtsp_record", str(SCRIPT))
    spec = importlib.util.spec_from_loader("rtsp_record", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


rr = _load_module()


# ---------------------------------------------------------------------------
# Fake-ffmpeg machinery
# ---------------------------------------------------------------------------

# A minimal ffmpeg replacement. Reads the output pattern from argv[-1], emits
# fake "[segment @ ...] Opening 'NAME' for writing" lines, and creates real
# files so the test can inspect them. Settings below come from the env so the
# same script can be parameterized per test:
#   FAKE_FFMPEG_INTERVAL  — seconds between segment opens (default 0.05)
#   FAKE_FFMPEG_MAX       — hard cap on segments emitted (safety, default 50)
#   FAKE_FFMPEG_EXIT      — exit code on natural termination (default 0)
#   FAKE_FFMPEG_FAIL_FAST — if "1", exit immediately with FAKE_FFMPEG_EXIT
FAKE_FFMPEG = textwrap.dedent("""\
    #!/usr/bin/env python3
    import os, sys, signal, time, datetime

    pattern = sys.argv[-1]
    interval = float(os.environ.get("FAKE_FFMPEG_INTERVAL", "0.05"))
    cap = int(os.environ.get("FAKE_FFMPEG_MAX", "50"))
    fail_fast = os.environ.get("FAKE_FFMPEG_FAIL_FAST", "") == "1"
    exit_code = int(os.environ.get("FAKE_FFMPEG_EXIT", "0"))

    if fail_fast:
        sys.stderr.write("fake-ffmpeg: deliberate failure\\n")
        sys.stderr.flush()
        sys.exit(exit_code)

    stop = [False]
    def handler(sig, frame):
        stop[0] = True
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    seq = 0
    while not stop[0] and seq < cap:
        seq += 1
        # strftime + a sequence injection for uniqueness across rapid opens
        ts = datetime.datetime.now().strftime(pattern)
        # Make filenames distinct even for sub-second runs
        base, dot, ext = ts.rpartition('.')
        name = f"{base}-{seq:03d}{dot}{ext}" if dot else f"{ts}-{seq:03d}"
        sys.stderr.write(f"[segment @ 0xdeadbeef] Opening '{name}' for writing\\n")
        sys.stderr.flush()
        with open(name, "w") as f:
            f.write("fake")
        # Sleep in small chunks so SIGINT is responsive
        end = time.monotonic() + interval
        while not stop[0] and time.monotonic() < end:
            time.sleep(0.01)

    sys.stderr.write("fake-ffmpeg: exiting\\n")
    sys.exit(exit_code)
    """)


class _FakeFfmpegPath:
    """Context manager: installs the fake ffmpeg as the only thing in PATH."""

    def __init__(self):
        self.dir = None
        self.old_path = None

    def __enter__(self):
        self.dir = tempfile.mkdtemp()
        ff = os.path.join(self.dir, "ffmpeg")
        with open(ff, "w") as f:
            f.write(FAKE_FFMPEG)
        os.chmod(ff, 0o755)
        self.old_path = os.environ.get("PATH")
        return self

    def env(self, **extra):
        e = dict(os.environ)
        # Keep python's bin in PATH so the shebang resolves; we just put the
        # fake ffmpeg first.
        e["PATH"] = self.dir + os.pathsep + (self.old_path or "")
        e.update(extra)
        return e

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)


def _run(args, env=None, stdin_text=None, cwd=None, timeout=15):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True,
        env=env, input=stdin_text, cwd=cwd, timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------

class ValidatePatternTests(unittest.TestCase):
    def test_strftime_present_ok(self):
        with tempfile.TemporaryDirectory() as d:
            rr._validate_pattern(os.path.join(d, "rec-%Y%m%d.mkv"))

    def test_no_strftime_rejected(self):
        with self.assertRaises(rr._Err) as cm:
            rr._validate_pattern("rec.mkv")
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("strftime placeholder", cm.exception.msg)

    def test_missing_dir_rejected(self):
        with self.assertRaises(rr._Err) as cm:
            rr._validate_pattern("/no/such/dir/rec-%Y.mkv")
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("output directory does not exist", cm.exception.msg)


class BuildCmdTests(unittest.TestCase):
    def test_flags_present_in_order(self):
        cmd = rr.build_ffmpeg_cmd("rtsp://x/y", "tcp", 600, "out-%Y.mkv")
        self.assertEqual(cmd[0], "ffmpeg")
        # Critical contract pieces:
        self.assertIn("-rtsp_transport", cmd)
        self.assertEqual(cmd[cmd.index("-rtsp_transport") + 1], "tcp")
        self.assertEqual(cmd[cmd.index("-i") + 1], "rtsp://x/y")
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-f") + 1], "segment")
        self.assertEqual(cmd[cmd.index("-segment_time") + 1], "600")
        self.assertEqual(cmd[cmd.index("-strftime") + 1], "1")
        self.assertEqual(cmd[cmd.index("-reset_timestamps") + 1], "1")
        # The pattern must be the very last argument so ffmpeg parses it as
        # the output URL.
        self.assertEqual(cmd[-1], "out-%Y.mkv")


# ---------------------------------------------------------------------------
# CLI usage-error paths
# ---------------------------------------------------------------------------

class UsageErrorTests(unittest.TestCase):
    def test_pattern_without_placeholder(self):
        r = _run(["-o", "rec.mkv", "rtsp://x/y"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("strftime placeholder", r.stderr)

    def test_missing_output_directory(self):
        r = _run(["-o", "/nonexistent-dir-xyz/rec-%Y.mkv", "rtsp://x/y"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("output directory does not exist", r.stderr)

    def test_malformed_url(self):
        r = _run(["http://not-rtsp/x"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("not an RTSP URL", r.stderr)

    def test_empty_stdin(self):
        r = _run([], stdin_text="")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no RTSP URL on stdin", r.stderr)

    def test_ffmpeg_missing(self):
        # Empty PATH so shutil.which("ffmpeg") returns None.
        env = {"PATH": ""}
        if "SystemRoot" in os.environ:
            env["SystemRoot"] = os.environ["SystemRoot"]
        r = _run(["rtsp://x/y"], env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ffmpeg not found", r.stderr)

    def test_negative_duration(self):
        r = _run(["-d", "0", "rtsp://x/y"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("duration must be > 0", r.stderr)


# ---------------------------------------------------------------------------
# Integration with the fake ffmpeg
# ---------------------------------------------------------------------------

class IntegrationTests(unittest.TestCase):
    def test_max_segments_stops_recording(self):
        with _FakeFfmpegPath() as fp, tempfile.TemporaryDirectory() as d:
            pattern = os.path.join(d, "rec-%Y%m%d-%H%M%S.mkv")
            r = _run(
                ["-o", pattern, "--max-segments", "2", "-d", "1",
                 "rtsp://x/y"],
                env=fp.env(FAKE_FFMPEG_INTERVAL="0.05",
                           FAKE_FFMPEG_MAX="20"),
            )
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            # Two complete segments are guaranteed; ffmpeg may have rolled
            # over into segment 3 (the partial that triggered our SIGINT).
            files = sorted(os.listdir(d))
            self.assertGreaterEqual(len(files), 2)
            self.assertLessEqual(len(files), 3)

    def test_verbose_logs_segments(self):
        with _FakeFfmpegPath() as fp, tempfile.TemporaryDirectory() as d:
            pattern = os.path.join(d, "rec-%Y%m%d-%H%M%S.mkv")
            r = _run(
                ["-v", "-o", pattern, "--max-segments", "1",
                 "rtsp://x/y"],
                env=fp.env(FAKE_FFMPEG_INTERVAL="0.05"),
            )
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertRegex(r.stderr, r"rtsp-record: segment \d+:")

    def test_ffmpeg_failure_propagates(self):
        with _FakeFfmpegPath() as fp, tempfile.TemporaryDirectory() as d:
            pattern = os.path.join(d, "rec-%Y%m%d-%H%M%S.mkv")
            r = _run(
                ["-o", pattern, "rtsp://x/y"],
                env=fp.env(FAKE_FFMPEG_FAIL_FAST="1", FAKE_FFMPEG_EXIT="3"),
            )
            self.assertEqual(r.returncode, 4)
            self.assertIn("ffmpeg exited with code", r.stderr)

    def test_url_from_stdin(self):
        with _FakeFfmpegPath() as fp, tempfile.TemporaryDirectory() as d:
            pattern = os.path.join(d, "rec-%Y%m%d-%H%M%S.mkv")
            r = _run(
                ["-o", pattern, "--max-segments", "1"],
                env=fp.env(FAKE_FFMPEG_INTERVAL="0.05"),
                stdin_text="rtsp://x/y\n",
            )
            self.assertEqual(r.returncode, 0, msg=r.stderr)


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

class MetaTests(unittest.TestCase):
    def test_version(self):
        r = _run(["-V"])
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), f"{rr.PROG} {rr.VERSION}")

    def test_help(self):
        r = _run(["-h"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("RTSP", r.stdout)


if __name__ == "__main__":
    unittest.main()
