import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SourceEngineNamespaceTests(unittest.TestCase):
    def _conflicting_scripts_environment(self, root: Path) -> dict[str, str]:
        conflicting_package = root / "external" / "scripts"
        conflicting_package.mkdir(parents=True)
        (conflicting_package / "__init__.py").write_text(
            'raise RuntimeError("the unrelated scripts package was imported")\n',
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join((str(conflicting_package.parent), str(ROOT)))
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return environment

    def test_source_cli_loader_uses_namespaced_engine(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            environment = self._conflicting_scripts_environment(root)
            command = (
                "import sys; from pathlib import Path; "
                "from comic_sol_product.cli import _engine_package, _load_engine_module; "
                "module = _load_engine_module('comic_sol'); "
                "assert _engine_package() == 'comic_sol_product.engine'; "
                f"assert Path(module.__file__).resolve() == "
                f"Path({str(ROOT / 'scripts' / 'comic_sol.py')!r}).resolve(); "
                "assert 'scripts' not in sys.modules"
            )
            result = subprocess.run(
                [sys.executable, "-c", command],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_direct_comic_sol_script_path_survives_conflicting_scripts_package(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            environment = self._conflicting_scripts_environment(root)
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "comic_sol.py"), "--help"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("usage:", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
