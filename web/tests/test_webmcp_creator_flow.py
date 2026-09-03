"""Creator-first WebMCP contract for ComicSol Studio."""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path


WEB_ROOT = Path(__file__).resolve().parents[1]
APP = WEB_ROOT / "comic_sol_web" / "static" / "app.js"


class WebMcpCreatorFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = APP.read_text(encoding="utf-8")

    def test_app_registers_three_creator_first_tools(self) -> None:
        for name in ("get_comic_context", "create_comic", "revise_comic"):
            self.assertIn(f'name: "{name}"', self.app)
        self.assertIn("registerCreatorWebMcp", self.app)
        self.assertIn("void registerCreatorWebMcp();", self.app)

    def test_creator_layer_reuses_existing_project_api(self) -> None:
        self.assertRegex(self.app, r'import\s*\{[^}]*createProject[^}]*getCurrentProject[^}]*updatePlan[^}]*\}\s*from\s*"\.\/api\.js"')
        self.assertIn("createProject(", self.app)
        self.assertIn("getCurrentProject(", self.app)
        self.assertIn("updatePlan(", self.app)

    def test_creator_inputs_hide_low_level_revision_mechanics(self) -> None:
        create_block = re.search(
            r'name: "create_comic"(?P<body>.*?)execute:', self.app, re.DOTALL
        )
        revise_block = re.search(
            r'name: "revise_comic"(?P<body>.*?)execute:', self.app, re.DOTALL
        )
        self.assertIsNotNone(create_block)
        self.assertIsNotNone(revise_block)
        assert create_block is not None and revise_block is not None
        for forbidden in ("expected_revision", "idempotency_key", "provider", "job_id"):
            self.assertNotIn(forbidden, create_block.group("body"))
            self.assertNotIn(forbidden, revise_block.group("body"))

    def test_app_remains_valid_javascript(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for the WebMCP creator contract")
        assert node is not None
        completed = subprocess.run(
            [node, "--check", str(APP)],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
