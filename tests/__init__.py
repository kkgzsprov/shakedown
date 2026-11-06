"""Make the source package available without installing it."""

from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def cleanup_temp_directory(directory, timeout_s=1):
    """Allow brief Windows sharing violations, but still fail a persistent lock.

    A telemetry sharing violation can briefly outlast observed process exit.
    Retry only that observed error, never ignore
    arbitrary cleanup errors or weaken the process-lifetime assertions.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            directory.cleanup()
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) != 32 or time.monotonic() >= deadline:
                raise
            time.sleep(.01)
