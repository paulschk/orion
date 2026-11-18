"""A restart in the middle of a provisioning leaves the row saying so forever.

The job that owned it died with the panel, so nothing else will ever write it:
the instance shows a loader that cannot finish, reports itself as not running,
gives no live figure, and keeps its vCPU out of the sellable pool. What the row
should become is a pure function of what the daemon reports, so it is testable
without a daemon.
"""

import os
import unittest

os.environ.setdefault("ORION_DATA_DIR", "/tmp/orion-test-data")
os.environ.setdefault("ORION_SECRET_KEY", "testkey")

from orion import tasks  # noqa: E402


class SettleProvisioning(unittest.TestCase):
    def test_a_running_container_means_the_provision_finished(self):
        status, message = tasks.settle_provisioning({"status": "Running"})
        self.assertEqual(status, "running")
        self.assertEqual(message, "")

    def test_a_missing_container_is_the_interrupted_case(self):
        status, message = tasks.settle_provisioning({})
        self.assertEqual(status, "error")
        self.assertIn("interrompu", message)

    def test_a_stopped_container_is_stopped(self):
        self.assertEqual(tasks.settle_provisioning({"status": "Stopped"})[0], "stopped")

    def test_a_payload_without_a_status_does_not_read_as_running(self):
        # The daemon's payload is not to be trusted for shape: a state that
        # carries no status must not settle the row as a running instance.
        self.assertEqual(tasks.settle_provisioning({"cpu": {"usage": 1}})[0], "stopped")


if __name__ == "__main__":
    unittest.main()
