"""Two elements with the same id on one page hand back whichever comes first.

That is how the terminal broke: the tile that anchors "Console" and the terminal
pane itself both carried `id="terminal"`, `getElementById` returned the tile, its
`dataset.state` was empty, the script took the "not running" branch - and the
exception it raised there (a `const` read before initialisation) ended the module
before the keyboard handler and the sizing were installed. The page looked fine;
the terminal simply did nothing, which is the worst shape a bug can have.

Per file, not per rendered page: the shell adds its own ids, so what this catches
is a template that declares the same one twice.
"""

import os
import re
import unittest
from pathlib import Path

os.environ.setdefault("ORION_DATA_DIR", "/tmp/orion-test-data")
os.environ.setdefault("ORION_SECRET_KEY", "testkey")

ROOT = Path(__file__).resolve().parent.parent
ID = re.compile(r'\sid="([^"{}]+)"')


class NoDuplicateIds(unittest.TestCase):
    def test_no_template_declares_the_same_id_twice(self):
        offenders = []
        for path in (ROOT / "orion" / "templates").rglob("*.html"):
            seen = {}
            for name in ID.findall(path.read_text()):
                seen[name] = seen.get(name, 0) + 1
            for name, count in seen.items():
                if count > 1:
                    offenders.append(f"{path.relative_to(ROOT)}: id={name} x{count}")
        self.assertEqual(offenders, [], "identifiant declare deux fois dans un gabarit")


if __name__ == "__main__":
    unittest.main()
