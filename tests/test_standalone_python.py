import hashlib
import importlib.util
import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path, PurePosixPath

INSTALLER_PATH = (
    Path(__file__).resolve().parents[1] / ".github/scripts/install_standalone_python.py"
)
INSTALLER_SPEC = importlib.util.spec_from_file_location("install_standalone_python", INSTALLER_PATH)
if INSTALLER_SPEC is None or INSTALLER_SPEC.loader is None:
    raise RuntimeError("could not load the standalone CPython installer")
INSTALLER = importlib.util.module_from_spec(INSTALLER_SPEC)
sys.modules[INSTALLER_SPEC.name] = INSTALLER
INSTALLER_SPEC.loader.exec_module(INSTALLER)

PYTHON_VERSION = INSTALLER.PYTHON_VERSION
RELEASE_TAG = INSTALLER.RELEASE_TAG
RUNTIME_ASSETS = INSTALLER.RUNTIME_ASSETS
asset_url = INSTALLER.asset_url
runtime_asset = INSTALLER.runtime_asset
verify_sha256 = INSTALLER.verify_sha256
extract_archive = INSTALLER.extract_archive
validate_link_target = INSTALLER._validate_link_target


def _write_archive(path, entries):
    """Write minimal test entries to a gzip tar archive."""
    with tarfile.open(path, "w:gz") as archive:
        for entry in entries:
            member = tarfile.TarInfo(entry["name"])
            if "linkname" in entry:
                member.type = entry.get("type", tarfile.SYMTYPE)
                member.linkname = entry["linkname"]
                archive.addfile(member)
            else:
                data = entry.get("data", b"")
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))


class StandalonePythonTests(unittest.TestCase):
    def test_native_release_assets_are_pinned_to_verified_31118_builds(self):
        """Keep every native runtime tied to its reviewed asset digest."""
        expected = {
            ("linux", "x86_64"): (
                "cpython-3.11.18+20261026-x86_64-unknown-linux-gnu-install_only.tar.gz",
                "a87ee63646c8f8438b7af3954b2135ed2af91d033c631f12a8f20af903046d06",
            ),
            ("macos", "arm64"): (
                "cpython-3.11.18+20261026-aarch64-apple-darwin-install_only.tar.gz",
                "cba3455e18516fbe8668cff2820c9b6a53447c552d612443c2b2fb60d390a908",
            ),
            ("windows", "x86_64"): (
                "cpython-3.11.18+20261026-x86_64-pc-windows-msvc-install_only.tar.gz",
                "75cb501dc11c7b55c6b447c58e4910bce5b1f609f1017328b4f3a858dfaf5428",
            ),
        }
        self.assertEqual("20261026", RELEASE_TAG)
        self.assertEqual("3.11.18", PYTHON_VERSION)
        self.assertEqual(set(expected), set(RUNTIME_ASSETS))
        for target, values in expected.items():
            asset = runtime_asset(*target)
            self.assertEqual(values, (asset.filename, asset.sha256))

    def test_asset_url_encodes_plus_in_release_filename(self):
        """Keep the release filename's plus sign URL-encoded."""
        url = asset_url(runtime_asset("macos", "arm64"))
        self.assertIn("/20261026/", url)
        self.assertIn("%2B20261026-", url)

    def test_digest_verifier_accepts_matching_file(self):
        """Accept downloaded bytes only when their pinned digest matches."""
        payload = b"standalone runtime asset"
        expected = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "asset.tar.gz"
            path.write_bytes(payload)
            self.assertEqual(expected, verify_sha256(path, expected))

    def test_digest_verifier_rejects_mismatch_and_invalid_pin(self):
        """Fail closed for changed asset bytes and malformed digest pins."""
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "asset.tar.gz"
            path.write_bytes(b"asset")
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                verify_sha256(path, "0" * 64)
            with self.assertRaisesRegex(ValueError, "64 lowercase hexadecimal"):
                verify_sha256(path, "BAD")

    def test_unconfigured_target_is_rejected(self):
        """Reject runtime targets without a reviewed asset pin."""
        with self.assertRaisesRegex(ValueError, "no pinned standalone"):
            runtime_asset("freebsd", "x86_64")


class StandalonePythonArchiveTests(unittest.TestCase):
    """Exercise path, link, and file-type policy before extracting an archive."""

    def test_extracts_regular_members_under_the_python_root(self):
        """Extract a normal runtime member beneath the expected root."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "runtime.tar.gz"
            staging = root / "staging"
            _write_archive(
                archive,
                [{"name": "python/bin/python.exe", "data": b"runtime"}],
            )

            runtime = extract_archive(archive, staging)

            self.assertEqual(b"runtime", (runtime / "bin/python.exe").read_bytes())

    def test_rejects_unsafe_member_paths_before_extracting(self):
        """Reject absolute, traversal, Windows-drive, and separator paths."""
        unsafe_names = (
            "python/../../outside.txt",
            "/outside.txt",
            r"python\..\outside.txt",
            "python/C:/outside.txt",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for index, name in enumerate(unsafe_names):
                with self.subTest(name=name):
                    archive = root / f"unsafe-{index}.tar.gz"
                    staging = root / f"staging-{index}"
                    _write_archive(archive, [{"name": name, "data": b"untrusted"}])

                    with self.assertRaisesRegex(ValueError, "unsafe archive member path"):
                        extract_archive(archive, staging)

                    self.assertEqual([], list(staging.iterdir()))
                    self.assertFalse((root / "outside.txt").exists())

    def test_rejects_link_escape_before_extracting_following_members(self):
        """Reject a directly escaping link before writing later members."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "unsafe-link.tar.gz"
            staging = root / "staging"
            _write_archive(
                archive,
                [
                    {
                        "name": "python/escape",
                        "type": tarfile.SYMTYPE,
                        "linkname": "../../outside",
                    },
                    {"name": "python/escape/payload", "data": b"untrusted"},
                ],
            )

            with self.assertRaisesRegex(ValueError, "unsafe archive link target"):
                extract_archive(archive, staging)

            self.assertEqual([], list(staging.iterdir()))
            self.assertFalse((root / "outside").exists())

    def test_rejects_hard_links_outside_the_runtime_root(self):
        """Reject hard-link targets outside the standalone runtime root."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "unsafe-hardlink.tar.gz"
            staging = root / "staging"
            _write_archive(
                archive,
                [
                    {
                        "name": "python/hardlink",
                        "type": tarfile.LNKTYPE,
                        "linkname": "../../outside",
                    }
                ],
            )

            with self.assertRaisesRegex(ValueError, "unsafe archive link target"):
                extract_archive(archive, staging)

            self.assertEqual([], list(staging.iterdir()))
            self.assertFalse((root / "outside").exists())

    def test_rejects_members_nested_under_archive_symlinks(self):
        """Prevent a symlinked parent from redirecting later members outside Python."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "chained-parent.tar.gz"
            staging = root / "staging"
            _write_archive(
                archive,
                [
                    {"name": "python/sub", "type": tarfile.SYMTYPE, "linkname": "."},
                    {
                        "name": "python/sub/escape",
                        "type": tarfile.SYMTYPE,
                        "linkname": "../outside",
                    },
                    {"name": "python/sub/escape/payload", "data": b"untrusted"},
                ],
            )

            with self.assertRaisesRegex(ValueError, "traverses a symbolic link"):
                extract_archive(archive, staging)

            self.assertEqual([], list(staging.iterdir()))
            self.assertFalse((staging / "outside").exists())

    def test_rejects_link_targets_that_traverse_archive_symlinks(self):
        """Prevent a relative target from escaping through an earlier symlink."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "chained-target.tar.gz"
            staging = root / "staging"
            _write_archive(
                archive,
                [
                    {"name": "python/sub", "type": tarfile.SYMTYPE, "linkname": "."},
                    {
                        "name": "python/alias",
                        "type": tarfile.SYMTYPE,
                        "linkname": "sub/../outside",
                    },
                ],
            )

            with self.assertRaisesRegex(ValueError, "link target traverses a symbolic link"):
                extract_archive(archive, staging)

            self.assertEqual([], list(staging.iterdir()))
            self.assertFalse((staging / "outside").exists())

    def test_allows_relative_links_resolving_inside_the_runtime_root(self):
        """Keep legitimate relative links that stay within Python."""
        validate_link_target(
            PurePosixPath("python/share/terminfo/1/1178"),
            "../a/adm1178",
            is_hard_link=False,
        )


if __name__ == "__main__":
    unittest.main()
