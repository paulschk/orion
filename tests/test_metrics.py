"""CPU as a share of the instance's own cores, not of the host."""

import os
import unittest

os.environ.setdefault("ORION_DATA_DIR", "/tmp/orion-test-data")
os.environ.setdefault("ORION_SECRET_KEY", "testkey")

from orion.metrics import cpu_percent, percent, sample  # noqa: E402

MIB = 1024 * 1024
GIB = 1024 * 1024 * 1024
NS_S = 1_000_000_000


class Shares(unittest.TestCase):
    def test_a_share_of_the_quota(self):
        self.assertAlmostEqual(percent(256 * MIB, 1024 * MIB), 25.0)

    def test_nothing_to_divide_by_is_not_a_zero(self):
        # A dash and a 0% bar mean different things to a client.
        self.assertIsNone(percent(10, 0))
        self.assertIsNone(percent(10, None))
        self.assertIsNone(percent(None, 100))

    def test_clamped_to_a_sane_bar(self):
        self.assertEqual(percent(200, 100), 100.0)
        self.assertEqual(percent(-5, 100), 0.0)


class Cpu(unittest.TestCase):
    def test_two_cores_flat_out_reads_100_not_200(self):
        # Two vCPUs saturated for a second burn 2e9 ns. Reporting 200% would be
        # a bar off the end of its track.
        self.assertAlmostEqual(cpu_percent(0, 2 * NS_S, 1.0, 2), 100.0)

    def test_one_core_of_two_is_half(self):
        self.assertAlmostEqual(cpu_percent(0, 1 * NS_S, 1.0, 2), 50.0)

    def test_a_single_core_instance_is_not_halved(self):
        self.assertAlmostEqual(cpu_percent(0, NS_S, 1.0, 1), 100.0)

    def test_no_previous_reading_means_no_rate(self):
        self.assertIsNone(cpu_percent(None, 5 * NS_S, 1.0, 1))

    def test_a_counter_that_went_backwards_is_discarded(self):
        # Can happen when the instance is restarted between two polls.
        self.assertIsNone(cpu_percent(500, 400, 1.0, 1))

    def test_a_zero_interval_cannot_divide(self):
        self.assertIsNone(cpu_percent(0, NS_S, 0.0, 1))


# Captured verbatim from /1.0/instances/<name>/state on the live host. The
# first version of this module assumed disk.root.usage was a {used, total}
# mapping and shipped a 500 on the metrics card - the pure-function tests all
# passed, because they never saw the payload.
REAL_STATE = {
    "cpu": {"usage": 146478889000},
    "memory": {"usage": 475942912, "usage_peak": 0, "total": 1073741824},
    "disk": {"root": {"usage": 2623209472, "total": 10737418240}},
    "processes": 9,
}


class PayloadShape(unittest.TestCase):
    def test_reads_the_shape_the_daemon_actually_returns(self):
        out = sample("shape-test", vcpu=1, state=REAL_STATE)
        self.assertEqual(out["memory_used"], 475942912)
        self.assertEqual(out["memory_total"], 1073741824)
        self.assertEqual(out["disk_used"], 2623209472)
        self.assertEqual(out["disk_total"], 10737418240)
        self.assertEqual(out["processes"], 9)
        self.assertAlmostEqual(out["disk_used"] / out["disk_total"] * 100, 24.4, places=1)

    def test_the_plan_is_only_a_fallback_for_the_memory_cap(self):
        out = sample("fallback-test", memory_mb=512, state={"memory": {"usage": 10}})
        self.assertEqual(out["memory_total"], 512 * 1024 * 1024)

    def test_an_empty_state_is_not_an_exception(self):
        out = sample("empty-test", state={})
        self.assertEqual(out["disk_used"], 0)
        self.assertEqual(out["memory_total"], 0)
        self.assertIsNone(out["cpu_pct"])

    def test_a_missing_state_returns_nothing_rather_than_zeroes(self):
        self.assertEqual(sample("none-test", state=None), {})


if __name__ == "__main__":
    unittest.main()
