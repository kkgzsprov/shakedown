"""Bounded child processes shared by batches and adapter checks."""

import os
from pathlib import Path
import signal
import subprocess
import threading


class ProcessControl:
    """A batch cancellation boundary, independent of any individual game's PID."""

    def __init__(self):
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.processes = set()

    def register(self, process):
        with self.lock:
            self.processes.add(process)
            cancelled = self.cancelled.is_set()
        if cancelled:
            terminate_process(process)

    def unregister(self, process):
        with self.lock:
            self.processes.discard(process)

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            processes = list(self.processes)
        for process in processes:
            terminate_process(process)


class _ManagedProcess(subprocess.Popen):
    def __exit__(self, *arguments):
        try:
            return super().__exit__(*arguments)
        finally:
            close_process(self)


def start_process(command, *, control: ProcessControl | None = None, **kwargs):
    options = process_options()
    options.update(kwargs)
    job = None
    if os.name == "nt":
        from .windows_job import WindowsJob
        job = WindowsJob()
        options["creationflags"] = options.get("creationflags", 0) | 0x4  # CREATE_SUSPENDED
    process = None
    try:
        process = _ManagedProcess(command, **options)
        process._shakedown_job = job
        process._shakedown_control = control
        if job:
            job.attach(process)
        if control:
            control.register(process)
        if job and not (control and control.cancelled.is_set()):
            job.resume(process.pid)
        return process
    except BaseException:
        if process is not None:
            process.kill()
            process.wait(timeout=5)
            close_process(process)
        elif job:
            job.close()
        raise


def close_process(process):
    control = getattr(process, "_shakedown_control", None)
    if control:
        control.unregister(process)
    job = getattr(process, "_shakedown_job", None)
    if job:
        job.close()
    elif os.name != "nt":
        terminate_process(process)


def process_options() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate_process(process: subprocess.Popen) -> None:
    """Stop the process group/tree created with process_options()."""
    job = getattr(process, "_shakedown_job", None)
    if job:
        job.terminate()
    elif os.name == "nt":
        if process.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            # ESRCH means the group is gone; BSD reports EPERM instead when its
            # members are all zombies, which means the same thing here.
            pass
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass


def run_process(command: list[str], *, cwd: Path, env: dict,
                stdout_path: Path, timeout_s: float,
                input_data: bytes | None = None,
                control: ProcessControl | None = None) -> tuple[int, bool]:
    with stdout_path.open("wb") as output:
        if control is not None and control.cancelled.is_set():
            return -1, False
        process = start_process(command, control=control, cwd=cwd, env=env,
                                   stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT)
        timed_out = False
        try:
            try:
                process.communicate(input_data, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_process(process)
                process.communicate(timeout=5)
            return process.returncode, timed_out
        finally:
            if process.poll() is None:
                terminate_process(process)
                process.wait(timeout=5)
            close_process(process)
