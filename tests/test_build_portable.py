import tempfile
import unittest
from pathlib import Path

from scripts.build_portable import (
    PYTHON_LICENSE_RELATIVE,
    copy_python_license,
    find_python_license,
)


class BuildPortableLicenseTests(unittest.TestCase):
    def test_standard_library_license_is_preferred(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            stdlib = root / "lib" / "python3.11"
            prefix = root / "python"
            stdlib.mkdir(parents=True)
            prefix.mkdir()
            expected = stdlib / "LICENSE.txt"
            expected.write_text("CPython license", encoding="utf-8")
            (prefix / "LICENSE.txt").write_text("other file", encoding="utf-8")
            self.assertEqual(expected, find_python_license(stdlib, prefix))

    def test_runtime_prefix_license_is_used_when_stdlib_has_none(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            stdlib = root / "Lib"
            prefix = root / "python"
            stdlib.mkdir()
            prefix.mkdir()
            expected = prefix / "LICENSE.txt"
            expected.write_text("CPython license", encoding="utf-8")
            self.assertEqual(expected, find_python_license(stdlib, prefix))

    def test_missing_python_license_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            stdlib = root / "lib"
            prefix = root / "python"
            stdlib.mkdir()
            prefix.mkdir()
            with self.assertRaisesRegex(FileNotFoundError, "Python runtime LICENSE.txt"):
                find_python_license(stdlib, prefix)

    def test_license_is_copied_into_portable_runtime(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "LICENSE.txt"
            source.write_text("Python Software Foundation License", encoding="utf-8")
            runtime = root / "runtime" / "comic-sol"
            runtime.mkdir(parents=True)
            destination = copy_python_license(source, runtime)
            self.assertEqual(runtime / PYTHON_LICENSE_RELATIVE, destination)
            self.assertEqual(source.read_bytes(), destination.read_bytes())


if __name__ == "__main__":
    unittest.main()
