"""Creator-first WebMCP contract for ComicSol Studio."""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import ClassVar


WEB_ROOT = Path(__file__).resolve().parents[1]
STATIC = WEB_ROOT / "comic_sol_web" / "static"
APP = STATIC / "app.js"
BOOTSTRAP = STATIC / "creator-bootstrap.js"
INDEX = STATIC / "index.html"


class WebMcpCreatorFlowTests(unittest.TestCase):
    app: ClassVar[str]
    bootstrap: ClassVar[str]
    index: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = APP.read_text(encoding="utf-8")
        cls.bootstrap = BOOTSTRAP.read_text(encoding="utf-8") if BOOTSTRAP.exists() else ""
        cls.index = INDEX.read_text(encoding="utf-8")

    def test_app_registers_three_creator_first_tools(self) -> None:
        for name in ("get_comic_context", "create_comic", "revise_comic"):
            self.assertIn(f'name: "{name}"', self.app)
        self.assertIn("registerCreatorWebMcp", self.app)

    def test_creator_bootstrap_waits_for_core_surface_before_registration(self) -> None:
        self.assertTrue(BOOTSTRAP.is_file(), "creator-bootstrap.js must exist")
        self.assertIn('from "./app.js"', self.bootstrap)
        self.assertIn("registerCreatorWebMcp", self.bootstrap)
        self.assertIn("getTools", self.bootstrap)
        self.assertIn("CORE_TOOL_COUNT = 14", self.bootstrap)
        self.assertRegex(
            self.bootstrap,
            r"tools\.length\s*>=\s*CORE_TOOL_COUNT[\s\S]+await registerCreatorWebMcp\(\)",
        )
        self.assertIn('<script type="module" src="./creator-bootstrap.js"></script>', self.index)
        self.assertLess(self.index.index("./app.js"), self.index.index("./creator-bootstrap.js"))

    def test_creator_layer_reuses_existing_project_api(self) -> None:
        self.assertRegex(
            self.app,
            r'import\s*\{[^}]*createProject[^}]*getCurrentProject[^}]*updatePlan[^}]*\}\s*from\s*"\.\/api\.js"',
        )
        self.assertIn("createProject(", self.app)
        self.assertIn("getCurrentProject(", self.app)
        self.assertIn("updatePlan(", self.app)

    def test_creator_inputs_hide_low_level_revision_mechanics(self) -> None:
        create_block = re.search(r'name: "create_comic"(?P<body>.*?)execute:', self.app, re.DOTALL)
        revise_block = re.search(r'name: "revise_comic"(?P<body>.*?)execute:', self.app, re.DOTALL)
        self.assertIsNotNone(create_block)
        self.assertIsNotNone(revise_block)
        assert create_block is not None and revise_block is not None
        for forbidden in ("expected_revision", "idempotency_key", "provider", "job_id"):
            self.assertNotIn(forbidden, create_block.group("body"))
            self.assertNotIn(forbidden, revise_block.group("body"))

    def test_creator_flow_has_ephemeral_hosted_browser_fallback(self) -> None:
        self.assertIn("let browserLocalCreatorProject = null;", self.app)
        self.assertNotIn("localStorage", self.app)
        self.assertNotIn("sessionStorage", self.app)
        self.assertIn('mode: "browser-local"', self.app)
        self.assertIn("const CREATOR_PLAN_SCHEMA = creatorSchema(", self.app)
        create_block = re.search(r'name: "create_comic"(?P<body>.*?)execute:', self.app, re.DOTALL)
        self.assertIsNotNone(create_block)
        assert create_block is not None
        self.assertIn("plan: CREATOR_PLAN_SCHEMA", create_block.group("body"))

    def test_scripts_remain_valid_javascript(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for the WebMCP creator contract")
        assert node is not None
        for path in (APP, BOOTSTRAP):
            with self.subTest(path=path.name):
                completed = subprocess.run(
                    [node, "--check", str(path)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
