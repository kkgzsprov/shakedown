"""Process-tree lifetime and cancellation, including an already-exited root."""

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import tests  # noqa: F401
from shakedown.bridge import run_bridge
from shakedown.deciders import make_decider
from shakedown.processes import ProcessControl, run_process


def alive(pid):
    if os.name == "nt":
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return api.WaitForSingleObject(handle, 0) == 258
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = Path(f"/proc/{pid}/stat")
    return not (status.exists() and status.read_text().split()[2] == "Z")


class ProcessTests(unittest.TestCase):
    def test_bridge_timeout_kills_stdout_inheritor_after_root_exits(self):
        script = ("import subprocess,sys,json; "
                  "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(5)']); "
                  "print(json.dumps({'child':child.pid}),flush=True)")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            started = time.monotonic()
            run_bridge([sys.executable, "-c", script], directory, make_decider("mock"),
                       decider_name="mock", wall_timeout_s=.5)
            self.assertLess(time.monotonic() - started, 3)
            summary = json.loads((directory / "bridge_summary.json").read_text())
            self.assertTrue(summary["wall_timeout"])
            child = json.loads((directory / "stdout.log").read_text())["child"]
            deadline = time.monotonic() + 2
            while alive(child) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(alive(child), "timed-out game descendant is still alive")

    def test_normal_process_completion_closes_remaining_descendants(self):
        script = ("import subprocess,sys,json; "
                  "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(5)']); "
                  "print(json.dumps({'child':child.pid}),flush=True)")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            code, timed_out = run_process([sys.executable, "-c", script], cwd=directory,
                                          env=dict(os.environ), stdout_path=directory / "log",
                                          timeout_s=3)
            self.assertEqual((code, timed_out), (0, False))
            child = json.loads((directory / "log").read_text())["child"]
            deadline = time.monotonic() + 2
            while alive(child) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(alive(child))

    def test_cancelled_control_prevents_a_suspended_child_from_acting(self):
        control = ProcessControl()
        control.cancel()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            code, _ = run_process([sys.executable, "-c", "open('acted','w').write('yes')"],
                                  cwd=directory, env=dict(os.environ),
                                  stdout_path=directory / "log", timeout_s=3, control=control)
            self.assertNotEqual(code, 0)
            self.assertFalse((directory / "acted").exists())


if __name__ == "__main__":
    unittest.main()
