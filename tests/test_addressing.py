"""Which address belongs to the instance, and which to something inside it."""

import os
import unittest

os.environ.setdefault("ORION_DATA_DIR", "/tmp/orion-test-data")
os.environ.setdefault("ORION_SECRET_KEY", "testkey")

from orion.incus import pick_primary_address  # noqa: E402


def iface(*entries):
    return {"addresses": [{"family": f, "scope": s, "address": a} for f, s, a in entries]}


# The shape a Docker instance actually reports: docker0 first, because the
# daemon serialises the map with sorted keys.
DOCKER_INSTANCE = {
    "network": {
        "docker0": iface(("inet", "global", "172.17.0.1")),
        "eth0": iface(("inet", "link", "10.42.0.5"), ("inet", "global", "10.42.0.5")),
        "lo": iface(("inet", "host", "127.0.0.1")),
        "vethecaa882": iface(("inet6", "link", "fe80::1")),
    }
}


class PrimaryAddress(unittest.TestCase):
    def test_prefers_the_bridge_interface_over_an_inner_one(self):
        self.assertEqual(pick_primary_address(DOCKER_INSTANCE), "10.42.0.5")

    def test_returns_nothing_when_only_an_inner_interface_has_an_address(self):
        only_inner = {"network": {"docker0": iface(("inet", "global", "172.17.0.1"))}}
        self.assertEqual(pick_primary_address(only_inner), "")

    def test_ignores_link_local_and_loopback(self):
        state = {"network": {"eth0": iface(("inet", "link", "169.254.1.1"))}}
        self.assertEqual(pick_primary_address(state), "")

    def test_accepts_a_differently_named_uplink(self):
        # Some images use enp0s3 or ens18 rather than eth0.
        state = {"network": {"ens18": iface(("inet", "global", "10.42.0.9"))}}
        self.assertEqual(pick_primary_address(state), "10.42.0.9")

    def test_tolerates_an_empty_state(self):
        self.assertEqual(pick_primary_address({}), "")


if __name__ == "__main__":
    unittest.main()
