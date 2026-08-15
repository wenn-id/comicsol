import os
import subprocess
import sys
import unittest
from pathlib import Path


class IsolatedDiscoveryTests(unittest.TestCase):
    def test_each_test_module_discovers_in_a_fresh_process(self):
        root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)

        for test_file in sorted((root / "tests").glob("test_*.py")):
            if test_file.resolve() == Path(__file__).resolve():
                continue
            with self.subTest(module=test_file.stem):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "unittest",
                        "discover",
                        "-s",
                        "tests",
                        "-p",
                        test_file.name,
                        "-q",
                    ],
                    cwd=root,
                    env=environment,
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=600,
                )
                self.assertEqual(
                    0,
                    completed.returncode,
                    f"{test_file.name} failed isolated discovery\n"
                    f"stdout:\n{completed.stdout}\n"
                    f"stderr:\n{completed.stderr}",
                )


if __name__ == "__main__":
    unittest.main()
