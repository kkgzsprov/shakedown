"""Transient Windows fixture cleanup must remain bounded and strict."""

import unittest
from unittest.mock import Mock, patch

from tests import cleanup_temp_directory


def sharing_error():
    error = PermissionError("temporary sharing violation")
    error.winerror = 32
    return error


class CleanupTests(unittest.TestCase):
    def test_only_a_transient_sharing_violation_is_retried(self):
        directory = Mock()
        directory.cleanup.side_effect = [sharing_error(), None]
        with patch("tests.time.monotonic", side_effect=[0, .1]), patch("tests.time.sleep") as sleep:
            cleanup_temp_directory(directory)
        self.assertEqual(directory.cleanup.call_count, 2)
        sleep.assert_called_once_with(.01)

    def test_persistent_lock_remains_a_failure_after_the_deadline(self):
        directory = Mock()
        directory.cleanup.side_effect = sharing_error()
        with patch("tests.time.monotonic", side_effect=[0, 1]), patch("tests.time.sleep") as sleep:
            with self.assertRaises(PermissionError):
                cleanup_temp_directory(directory)
        sleep.assert_not_called()

    def test_other_permission_errors_are_not_retried(self):
        directory = Mock()
        directory.cleanup.side_effect = PermissionError("access denied")
        with patch("tests.time.sleep") as sleep:
            with self.assertRaises(PermissionError):
                cleanup_temp_directory(directory)
        self.assertEqual(directory.cleanup.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
