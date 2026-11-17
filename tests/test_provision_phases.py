"""The declared phases and the script's progress() calls must not drift apart.

"Step N of M" is only honest if M is the number of phases the script really
walks through. The panel would happily print a wrong total forever, so the two
lists are compared here.
"""

import os
import re
import unittest
from pathlib import Path

os.environ.setdefault("ORION_DATA_DIR", "/tmp/orion-test-data")
os.environ.setdefault("ORION_SECRET_KEY", "testkey")

from orion import provision  # noqa: E402


class DeclaredPhases(unittest.TestCase):
    def test_the_thresholds_in_the_source_are_the_declared_ones(self):
        source = Path(provision.__file__).read_text()
        found = sorted({int(m) for m in re.findall(r"^\s*progress\((\d+)", source, re.M)})
        self.assertEqual(
            found, list(provision.PHASE_STEPS),
            "PHASE_STEPS and the script's progress() calls have diverged",
        )

    def test_branches_do_not_change_the_count(self):
        # A container being created and one being reused are two calls at the
        # same threshold: the number of *phases* must not depend on the branch.
        source = Path(provision.__file__).read_text()
        calls = re.findall(r"^\s*progress\((\d+)", source, re.M)
        phases = set(calls)
        self.assertLess(len(phases), len(calls), "les branches ont disparu : verifier le comptage")
        self.assertEqual(len(provision.PHASE_STEPS), len(phases))

    def test_the_total_is_the_number_of_thresholds(self):
        self.assertEqual(provision.STEP_TOTAL, len(provision.PHASE_STEPS))
        self.assertGreaterEqual(provision.STEP_TOTAL, 5)

    def test_the_last_threshold_is_completion(self):
        self.assertEqual(max(provision.PHASE_STEPS), 100)


if __name__ == "__main__":
    unittest.main()
