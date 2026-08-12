import os
import re
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
