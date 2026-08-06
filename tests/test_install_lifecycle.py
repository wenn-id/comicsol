import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from comic_sol_product.install_lifecycle import (
    install_archive,
    read_active_version,
    uninstall_runtime,
)


class NativeInstallLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.install_root = self.root / "app"
        self.projects = self.root / "projects"
        self.projects.mkdir()
        (self.projects / "keep.txt").write_text("project-data", encoding="utf-8")

    def archive(self, version: str, payload: bytes = b"runtime") -> Path:
        archive = self.root / f"comic-sol-{version}.zip"
        executable = "comic-sol.exe" if os.name == "nt" else "comic-sol"
        with zipfile.ZipFile(archive, "w") as writer:
            info = zipfile.ZipInfo(f"comic-sol/{executable}")
            info.external_attr = 0o755 << 16
            writer.writestr(info, payload)
            writer.writestr("comic-sol/_internal/version.txt", version)
        return archive

    def test_fresh_install_activates_version_and_stable_launcher(self):
        result = install_archive(
            self.archive("2.0.0rc1"),
            self.install_root,
            "2.0.0rc1",
            verifier=lambda executable: executable.read_bytes() == b"runtime",
        )
        self.assertEqual("installed", result.status)
        self.assertEqual("2.0.0rc1", read_active_version(self.install_root))
        self.assertTrue(result.executable.is_file())
        self.assertEqual(b"runtime", result.executable.read_bytes())
        self.assertEqual("project-data", (self.projects / "keep.txt").read_text())

    def test_reinstall_is_idempotent_without_duplicate_version(self):
        archive = self.archive("2.0.0rc1")
        first = install_archive(archive, self.install_root, "2.0.0rc1", verifier=lambda _: True)
        second = install_archive(archive, self.install_root, "2.0.0rc1", verifier=lambda _: True)
        self.assertEqual("installed", first.status)
        self.assertEqual("unchanged", second.status)
        self.assertEqual(1, len(list((self.install_root / "versions").iterdir())))

    def test_upgrade_preserves_previous_version_for_rollback(self):
        install_archive(self.archive("1.9.0", b"old"), self.install_root, "1.9.0", verifier=lambda _: True)
        result = install_archive(
            self.archive("2.0.0rc1", b"new"),
            self.install_root,
            "2.0.0rc1",
            verifier=lambda executable: executable.read_bytes() == b"new",
        )
        self.assertEqual("upgraded", result.status)
        self.assertEqual("1.9.0", result.previous_version)
        self.assertTrue((self.install_root / "versions/1.9.0").is_dir())
        self.assertTrue((self.install_root / "versions/2.0.0rc1").is_dir())
        self.assertEqual("2.0.0rc1", read_active_version(self.install_root))

    def test_failed_upgrade_rolls_back_pointer_and_removes_failed_version(self):
        install_archive(self.archive("1.9.0", b"old"), self.install_root, "1.9.0", verifier=lambda _: True)
        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            install_archive(
                self.archive("2.0.0rc1", b"broken"),
                self.install_root,
                "2.0.0rc1",
                verifier=lambda _: False,
            )
        self.assertEqual("1.9.0", read_active_version(self.install_root))
        self.assertFalse((self.install_root / "versions/2.0.0rc1").exists())
        executable = "comic-sol.exe" if os.name == "nt" else "comic-sol"
        self.assertEqual(b"old", (self.install_root / "bin" / executable).read_bytes())

    def test_uninstall_removes_runtime_only_and_preserves_projects(self):
        install_archive(self.archive("2.0.0rc1"), self.install_root, "2.0.0rc1", verifier=lambda _: True)
        result = uninstall_runtime(self.install_root)
        self.assertEqual("removed", result.status)
        self.assertFalse(self.install_root.exists())
        self.assertEqual("project-data", (self.projects / "keep.txt").read_text())

    def test_public_installers_verify_checksum_health_and_preserve_projects(self):
        root = Path(__file__).resolve().parents[1]
        posix = (root / "installers/install.sh").read_text(encoding="utf-8")
        powershell = (root / "installers/install.ps1").read_text(encoding="utf-8")
        for script in (posix, powershell):
            self.assertIn("SHA256", script.upper())
            self.assertIn("doctor", script)
            self.assertIn("active-version", script)
            self.assertIn("unsigned", script.lower())
            self.assertNotIn("Comic Sol Projects", script)
        self.assertIn("sha256sum", posix)
        self.assertNotIn("python", posix.lower())
        self.assertIn("validate_zip", posix)
        self.assertIn("unsafe archive member", posix)
        self.assertIn("Test-UnsafeArchive", powershell)
        self.assertIn("Expand-Archive", powershell)
        self.assertIn("unsafe archive member", powershell)
        self.assertIn("Get-FileHash", powershell)
        self.assertIn("-Uninstall", powershell)
        self.assertIn("--uninstall", posix)


if __name__ == "__main__":
    unittest.main()
