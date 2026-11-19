"""Deleting an instance must show up on the list straight away.

The teardown runs as a background task and the redirect leaves before it finishes,
so without a mark set in the request itself the list re-renders with the instance
still alive - and nothing comes back to correct it. These pin both halves: the mark
that is set at once, and the condition that makes a list refresh itself while
anything is mid-flight.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

os.environ.setdefault("ORION_DATA_DIR", "/tmp/orion-test-data")
os.environ.setdefault("ORION_SECRET_KEY", "testkey")

from orion.routers import client  # noqa: E402


class LiveList(unittest.TestCase):
    def _rows(self, *statuses):
        return [{"status": s} for s in statuses]

    def test_a_settled_list_does_not_refresh_itself(self):
        for status in ("running", "stopped", "destroyed", "error", "awaiting_payment"):
            self.assertFalse(client._live(self._rows(status)), status)

    def test_a_moving_list_refreshes_itself(self):
        for status in ("provisioning", "destroying"):
            self.assertTrue(client._live(self._rows("running", status)), status)

    def test_the_mark_is_set_before_the_task_starts(self):
        source = Path(client.__file__).read_text()
        route = source.split('async def instance_destroy', 1)[1].split("async def ", 1)[0]

        marked = route.index("'destroying'")
        started = route.index('tasks.start("destroy",')
        self.assertLess(marked, started,
                        "la marque doit être posée dans la requête, pas par la tâche")

    def test_the_list_still_hides_what_is_gone(self):
        source = Path(client.__file__).read_text()
        listing = source.split('async def instances', 1)[1].split("async def ", 1)[0]

        self.assertIn("i.status != 'destroyed'", listing)
        self.assertNotIn("'destroying'", listing,
                         "une instance en cours de suppression doit rester visible, le temps qu'elle parte")


if __name__ == "__main__":
    unittest.main()
