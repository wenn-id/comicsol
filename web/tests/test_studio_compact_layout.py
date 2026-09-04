"""Static contracts for the compact single-viewport Studio layout."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import ClassVar


WEB_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = WEB_ROOT / "comic_sol_web" / "static"


class StudioCompactLayoutTests(unittest.TestCase):
    start: ClassVar[str]
    styles: ClassVar[str]
    activity: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.start = (STATIC_ROOT / "views" / "start.js").read_text(encoding="utf-8")
        cls.styles = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")
        cls.activity = (STATIC_ROOT / "activity.js").read_text(encoding="utf-8")

    def test_start_view_exposes_compact_layout_hooks(self) -> None:
        self.assertIn('className: "card start-create-card"', self.start)
        self.assertIn('id: "create-project-form", className: "start-create-form"', self.start)
        self.assertGreaterEqual(self.start.count('className: "compact-fields"'), 2)

    def test_desktop_layout_uses_single_viewport_with_scroll_fallback(self) -> None:
        self.assertIn("@media (min-width: 64rem) and (min-height: 42rem)", self.styles)
        desktop = self.styles.split("@media (min-width: 64rem) and (min-height: 42rem)", 1)[1]
        self.assertRegex(desktop, r"html,\s*body\s*\{[^}]*height:\s*100%")
        self.assertRegex(desktop, r"body\s*\{[^}]*overflow:\s*hidden")
        self.assertRegex(desktop, r"#studio-shell\s*\{[^}]*min-height:\s*0[^}]*height:\s*100%")
        self.assertRegex(desktop, r"\.start-create-form\s*\{[^}]*display:\s*grid")
        self.assertRegex(desktop, r"\.compact-fields\s*\{[^}]*grid-template-columns:\s*repeat\(2")
        self.assertRegex(
            self.styles,
            r"@media \(max-width: 63\.99rem\)[\s\S]*body\s*\{[^}]*overflow-y:\s*auto",
        )

    def test_responsive_overrides_follow_the_base_drawer_rules(self) -> None:
        drawer_rule = self.styles.rfind("#activity-drawer {")
        self.assertGreater(drawer_rule, -1)
        self.assertGreater(self.styles.rfind("@media (max-width: 63.99rem)"), drawer_rule)
        self.assertGreater(
            self.styles.rfind("@media (min-width: 64rem) and (min-height: 42rem)"),
            drawer_rule,
        )

    def test_activity_drawer_defaults_collapsed_until_user_opens_it(self) -> None:
        self.assertIn("if (!raw) return { collapsed: true, width: 420 };", self.activity)
        self.assertGreaterEqual(
            len(re.findall(r"return \{ collapsed: true, width: 420 \};", self.activity)),
            2,
        )
        self.assertIn('#activity-drawer[data-collapsed="true"]', self.styles)
        self.assertRegex(
            self.styles,
            r'#activity-drawer\[data-collapsed="true"\]\s*\{[^}]*width:\s*auto\s*!important',
        )


if __name__ == "__main__":
    unittest.main()
