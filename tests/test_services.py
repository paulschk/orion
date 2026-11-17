"""The /proc/net/tcp reader: no daemon involved, so it is worth testing directly."""

import os
import unittest

os.environ.setdefault("ORION_DATA_DIR", "/tmp/orion-test-data")
os.environ.setdefault("ORION_SECRET_KEY", "testkey")

from orion.services import INTERNAL_PORTS, address_for, parse_proc_net  # noqa: E402

# Two real rows: one IPv4 listener on 80 (0x50), one IPv6 listener on 8080
# (0x1F90), plus a non-listening row that must be ignored.
SAMPLE = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt
   0: 00000000:0050 00000000:0000 0A 00000000:00000000 00:00000000 00000000
   1: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000
   2: 00000000:0016 00000000:0000 01 00000000:00000000 00:00000000 00000000
   3: 0100007F:0035 00000000:0000 0A 00000000:00000000 00:00000000 00000000
"""


class ParseProcNet(unittest.TestCase):
    def test_reads_listening_ports_in_decimal(self):
        self.assertEqual(parse_proc_net(SAMPLE), {80, 8080, 53})

    def test_ignores_everything_that_is_not_listening(self):
        # The 0x16 row is state 01 - established, not listening.
        self.assertNotIn(22, parse_proc_net(SAMPLE))

    def test_survives_a_short_or_broken_line(self):
        self.assertEqual(parse_proc_net("header\n  garbage\n"), set())
        self.assertEqual(parse_proc_net(""), set())


class Addresses(unittest.TestCase):
    def test_web_ports_keep_the_plain_name(self):
        self.assertEqual(address_for("demo", 80, "hosting.example"), "demo.hosting.example")
        self.assertEqual(address_for("demo", 443, "hosting.example"), "demo.hosting.example")

    def test_other_ports_are_suffixed(self):
        self.assertEqual(address_for("demo", 8080, "hosting.example"),
                         "demo-8080.hosting.example")

    def test_ssh_is_never_offered_as_a_service(self):
        self.assertIn(22, INTERNAL_PORTS)


if __name__ == "__main__":
    unittest.main()
