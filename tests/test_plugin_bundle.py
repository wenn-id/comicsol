import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PluginBundleTests(unittest.TestCase):
    def test_generated_plugin_bundle_matches_canonical_root(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/sync_plugin_bundle.py"), "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
