"""Our stylesheet must not style a class xterm owns.

`.terminal` is put on xterm's own root element. A rule of ours aimed at that
name silently resized the terminal: a 420px max-height against a 435px screen,
41px of overflow, a padding and a border on top of the pane's, and a font-size
and line-height that fought the ones terminal.js sets. The log block that wanted
that name is now `.job-log`.
"""

import os
import re
import unittest
from pathlib import Path

os.environ.setdefault("ORION_DATA_DIR", "/tmp/orion-test-data")
os.environ.setdefault("ORION_SECRET_KEY", "testkey")

ROOT = Path(__file__).resolve().parent.parent
# Classes xterm puts on elements it creates. Styling one of these by name
# reaches into the terminal, whatever we meant by it.
XTERM_CLASSES = ("terminal", "xterm", "xterm-viewport", "xterm-screen",
                 "xterm-rows", "xterm-helpers", "xterm-helper-textarea")


class NoThirdPartyClassClash(unittest.TestCase):
    def test_no_template_uses_a_class_xterm_owns(self):
        offenders = []
        for path in (ROOT / "orion" / "templates").rglob("*.html"):
            text = path.read_text()
            for match in re.findall(r'class="([^"]+)"', text):
                for name in match.split():
                    if name in XTERM_CLASSES:
                        offenders.append(f"{path.relative_to(ROOT)}: class={name}")
        self.assertEqual(offenders, [], "classe appartenant a xterm utilisee comme la notre")

    def test_the_stylesheet_only_targets_xterm_under_the_terminal_pane(self):
        css = (ROOT / "orion" / "static" / "css" / "orion.css").read_text()
        # Every rule mentioning an xterm class must be scoped to the pane.
        unscoped = [line.strip() for line in css.splitlines()
                    if re.match(r"^\s*[\.#][^\{]*xterm", line)
                    and ".term-pane" not in line and "#terminal-status" not in line]
        self.assertEqual(unscoped, [], "regle xterm non portee par .term-pane")

    def test_the_log_block_and_its_rule_agree_on_the_name(self):
        css = (ROOT / "orion" / "static" / "css" / "orion.css").read_text()
        self.assertIn(".job-log {", css)
        users = [p for p in (ROOT / "orion" / "templates").rglob("*.html")
                 if 'class="job-log"' in p.read_text()]
        self.assertGreaterEqual(len(users), 2, "le bloc de journal a perdu sa classe")


if __name__ == "__main__":
    unittest.main()
