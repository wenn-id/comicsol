import tempfile
import unittest
from pathlib import Path

from scripts.build_portable import (
    PYTHON_LICENSE_RELATIVE,
    copy_python_license,
    find_python_license,
)
from tests.support import make_symlink


class BuildPortableLicenseTests(unittest.TestCase):
    def test_standard_library_license_is_preferred(self):
        """Prefer the standard-library license when both copies exist."""
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
        """Use the runtime-prefix license when the standard library lacks one."""
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
        """Fail the build when neither supported license path exists."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            stdlib = root / "lib"
            prefix = root / "python"
            stdlib.mkdir()
            prefix.mkdir()
            with self.assertRaisesRegex(FileNotFoundError, "Python runtime LICENSE.txt"):
                find_python_license(stdlib, prefix)

    def test_license_is_copied_into_portable_runtime(self):
        """Place the verified interpreter license in the frozen runtime tree."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "LICENSE.txt"
            source.write_text("Python Software Foundation License", encoding="utf-8")
            runtime = root / "runtime" / "comic-sol"
            runtime.mkdir(parents=True)
            destination = copy_python_license(source, runtime)
            self.assertEqual(
                (runtime / PYTHON_LICENSE_RELATIVE).resolve(),
                destination.resolve(),
            )
            self.assertEqual(source.read_bytes(), destination.read_bytes())

    def test_license_parent_symlink_cannot_create_directories_outside_runtime(self):
        """Reject a parent symlink before creating license folders outside the runtime."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "LICENSE.txt"
            source.write_text("Python Software Foundation License", encoding="utf-8")
            runtime = root / "runtime"
            runtime.mkdir()
            outside = root / "outside"
            outside.mkdir()
            make_symlink(self, runtime / "_internal", outside, directory=True)

            with self.assertRaisesRegex(ValueError, "escapes the portable runtime"):
                copy_python_license(source, runtime)

            self.assertFalse((outside / "licenses").exists())

    def test_license_destination_symlink_is_not_overwritten(self):
        """Preserve an external file rather than copying through a destination symlink."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "LICENSE.txt"
            source.write_text("Python Software Foundation License", encoding="utf-8")
            runtime = root / "runtime"
            runtime.mkdir()
            parent = runtime / PYTHON_LICENSE_RELATIVE.parent
            parent.mkdir(parents=True)
            outside = root / "outside-license.txt"
            outside.write_text("preserve", encoding="utf-8")
            make_symlink(self, parent / PYTHON_LICENSE_RELATIVE.name, outside)

            with self.assertRaisesRegex(FileExistsError, "destination already exists"):
                copy_python_license(source, runtime)

            self.assertEqual("preserve", outside.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
