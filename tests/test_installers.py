import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class PublicInstallerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.posix = (cls.root / "installers/install.sh").read_text(encoding="utf-8")
        cls.powershell = (cls.root / "installers/install.ps1").read_text(encoding="utf-8")

    def run_uninstall(self, install_root):
        if os.name == "nt":
            shell = shutil.which("pwsh") or shutil.which("powershell")
            self.assertIsNotNone(shell, "PowerShell is required for the Windows installer test")
            command = [
                shell,
                "-NoProfile",
                "-File",
                str(self.root / "installers/install.ps1"),
                "-InstallRoot",
                str(install_root),
                "-Uninstall",
            ]
        else:
            command = [
                "sh",
                str(self.root / "installers/install.sh"),
                "--install-root",
                str(install_root),
                "--uninstall",
            ]
        return subprocess.run(command, capture_output=True, text=True, check=False)

    @staticmethod
    def write_marker(install_root, marker_root=None):
        version = "2.0.0rc4"
        (install_root / "active-version").write_text(f"{version}\n", encoding="utf-8")
        (install_root / ".comic-sol-install").write_text(
            f"comic-sol-install-v1\n{version}\n{marker_root or install_root.resolve()}\n",
            encoding="utf-8",
        )

    def test_installers_discover_runtime_version_and_roll_back(self):
        for script in (self.posix, self.powershell):
            self.assertIsNone(re.search(r"\b\d+\.\d+\.\d+(?:rc\d+)?\b", script))
            self.assertIn("--version", script)
        self.assertIn("rollback()", self.posix)
        self.assertIn("Restore-Install", self.powershell)

    def test_installers_verify_checksum_health_and_preserve_projects(self):
        for script in (self.posix, self.powershell):
            self.assertIn("SHA256", script.upper())
            self.assertIn("doctor", script)
            self.assertIn("active-version", script)
            self.assertIn("unsigned", script.lower())
            self.assertNotIn("Comic Sol Projects", script)
        self.assertIn("sha256sum", self.posix)
        self.assertNotIn("python", self.posix.lower())
        self.assertIn("validate_zip", self.posix)
        self.assertIn("unsafe archive member", self.posix)
        self.assertIn("Test-UnsafeArchive", self.powershell)
        self.assertIn("Expand-Archive", self.powershell)
        self.assertIn("unsafe archive member", self.powershell)
        self.assertIn("Get-FileHash", self.powershell)
        self.assertIn("-Uninstall", self.powershell)
        self.assertIn("--uninstall", self.posix)

    def test_installers_serialize_install_root_mutations(self):
        self.assertIn("INSTALL_LOCK_DIR", self.posix)
        self.assertIn("mkdir --", self.posix)
        self.assertIn("release_install_lock", self.posix)
        self.assertIn("Mutex", self.powershell)
        self.assertIn("WaitOne", self.powershell)
        self.assertIn("ReleaseMutex", self.powershell)
        self.assertIn("InstallRoot", self.powershell)

    def test_native_uninstall_refuses_root_without_marker(self):
        with tempfile.TemporaryDirectory() as raw:
            install_root = Path(raw) / "unmarked-install"
            install_root.mkdir()
            foreign = install_root / "foreign.txt"
            foreign.write_text("keep me", encoding="utf-8")

            result = self.run_uninstall(install_root)

            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertTrue(foreign.is_file())
            self.assertEqual("keep me", foreign.read_text(encoding="utf-8"))

    def test_native_uninstall_refuses_marker_for_another_root(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            install_root = root / "marked-install"
            install_root.mkdir()
            foreign = install_root / "foreign.txt"
            foreign.write_text("keep me", encoding="utf-8")
            self.write_marker(install_root, root / "different-install")

            result = self.run_uninstall(install_root)

            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertTrue(foreign.is_file())
            self.assertEqual("keep me", foreign.read_text(encoding="utf-8"))

    def test_native_uninstall_removes_only_managed_children(self):
        with tempfile.TemporaryDirectory() as raw:
            install_root = Path(raw) / "managed-install"
            for directory in ("bin", "versions/2.0.0rc4", ".bin.rollback", "bin.new"):
                path = install_root / directory
                path.mkdir(parents=True)
                (path / "runtime.txt").write_text("managed", encoding="utf-8")
            foreign = install_root / "foreign.txt"
            foreign.write_text("keep me", encoding="utf-8")
            (install_root / "active-version.new").write_text("staged\n", encoding="utf-8")
            self.write_marker(install_root)

            result = self.run_uninstall(install_root)

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertTrue(foreign.is_file())
            self.assertEqual("keep me", foreign.read_text(encoding="utf-8"))
            for name in (
                "bin",
                "versions",
                ".bin.rollback",
                "bin.new",
                "active-version",
                "active-version.new",
                ".comic-sol-install",
            ):
                self.assertFalse((install_root / name).exists(), name)

    def test_native_uninstall_refuses_sensitive_project_root(self):
        with tempfile.TemporaryDirectory() as raw:
            install_root = Path(raw) / "project"
            (install_root / ".git").mkdir(parents=True)
            foreign = install_root / "foreign.txt"
            foreign.write_text("keep me", encoding="utf-8")
            self.write_marker(install_root)

            result = self.run_uninstall(install_root)

            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertTrue(foreign.is_file())
            self.assertEqual("keep me", foreign.read_text(encoding="utf-8"))
            self.assertTrue((install_root / ".git").is_dir())

    @unittest.skipUnless(os.name != "nt", "POSIX installer test")
    def test_posix_installer_refuses_active_install_root_lock(self):
        with tempfile.TemporaryDirectory() as raw:
            install_root = Path(raw) / "locked-install"
            lock = Path(f"{install_root}.lock")
            lock.mkdir(parents=True)
            (lock / "pid").write_text(f"{os.getpid()}\n", encoding="ascii")
            result = subprocess.run(
                [
                    "sh", str(self.root / "installers/install.sh"),
                    "--install-root", str(install_root), "--uninstall",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("another Comic Sol installer is using this install root", result.stderr)
            self.assertTrue(lock.is_dir())


if __name__ == "__main__":
    unittest.main()
