import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


DISCOVERY_CODE = """
import sys
import unittest

loader = unittest.TestLoader()
suite = loader.discover(sys.argv[1], pattern=sys.argv[2])
if loader.errors:
    raise SystemExit("\\n".join(loader.errors))
if suite.countTestCases() == 0:
    raise SystemExit("no tests discovered")
"""


def discover_test_file(
    root: Path,
    test_file: Path,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    isolated_environment = dict(os.environ if environment is None else environment)
    isolated_environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-c", DISCOVERY_CODE, "tests", test_file.name],
        cwd=root,
        env=isolated_environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )


class IsolatedDiscoveryTests(unittest.TestCase):
    def test_fresh_discovery_does_not_execute_test_bodies(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            tests = root / "tests"
            tests.mkdir()
            test_file = tests / "test_probe.py"
            test_file.write_text(
                "import unittest\n"
                "class ProbeTests(unittest.TestCase):\n"
                "    def test_not_executed(self):\n"
                "        raise RuntimeError('test body ran')\n",
                encoding="utf-8",
            )

            completed = discover_test_file(root, test_file)

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_each_test_module_discovers_in_a_fresh_process(self):
        root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)

        for test_file in sorted((root / "tests").glob("test_*.py")):
            if test_file.resolve() == Path(__file__).resolve():
                continue
            with self.subTest(module=test_file.stem):
                completed = discover_test_file(root, test_file, environment)
                self.assertEqual(
                    0,
                    completed.returncode,
                    f"{test_file.name} failed isolated discovery\n"
                    f"stdout:\n{completed.stdout}\n"
                    f"stderr:\n{completed.stderr}",
                )


if __name__ == "__main__":
    unittest.main()
