import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile
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

    def test_installers_canonicalize_or_reject_aliases_before_locking(self):
        self.assertLess(
            self.posix.index('INSTALL_ROOT=$(canonical_install_root "$INSTALL_ROOT")'),
            self.posix.index('INSTALL_LOCK_DIR="${INSTALL_ROOT}.lock"'),
        )
        self.assertLess(
            self.powershell.index(
                '$InstallRoot = Resolve-CanonicalInstallRoot -Path $InstallRoot'
            ),
            self.powershell.index("Acquire-InstallMutex", self.powershell.index("if ($Uninstall)")),
        )
        self.assertIn("ReparsePoint", self.powershell)

    def test_posix_sentinel_publication_is_the_commit_point(self):
        marker = self.posix.index(
            'mv -- "$INSTALL_ROOT/.comic-sol-install.new" '
            '"$INSTALL_ROOT/$INSTALL_MARKER_NAME"'
        )
        committed = self.posix.index("COMMITTED=1", marker)
        cleanup = self.posix.index('for backup in "$STABLE_BACKUP" "$TARGET_BACKUP"', marker)
        self.assertLess(marker, committed)
        self.assertLess(committed, cleanup)
        self.assertIn("Could not remove rollback backup", self.posix)

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

    @unittest.skipUnless(os.name == "nt", "PowerShell installer test")
    def test_powershell_relative_root_uses_the_current_powershell_location(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(shell)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            process_directory = root / "process"
            powershell_location = root / "location"
            wrong_root = process_directory / "runtime"
            requested_root = powershell_location / "runtime"
            wrong_root.mkdir(parents=True)
            requested_root.mkdir(parents=True)
            self.write_marker(wrong_root)
            self.write_marker(requested_root)
            (wrong_root / "foreign.txt").write_text("preserve", encoding="utf-8")
            runner = root / "run-relative-uninstall.ps1"
            runner.write_text(
                "param([string]$Installer, [string]$Location)\n"
                "Set-Location -LiteralPath $Location\n"
                "& $Installer -InstallRoot 'runtime' -Uninstall\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    shell, "-NoProfile", "-File", str(runner),
                    "-Installer", str(self.root / "installers/install.ps1"),
                    "-Location", str(powershell_location),
                ],
                cwd=process_directory,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertFalse(requested_root.exists())
            self.assertTrue((wrong_root / "foreign.txt").is_file())

    @unittest.skipUnless(os.name != "nt", "POSIX installer test")
    def test_posix_installer_refuses_active_install_root_lock(self):
        with tempfile.TemporaryDirectory() as raw:
            install_root = Path(raw) / "locked-install"
            install_root.mkdir()
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

    @unittest.skipUnless(os.name != "nt", "POSIX installer test")
    def test_posix_alias_uses_the_canonical_install_lock(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            install_root = root / "runtime"
            install_root.mkdir()
            self.write_marker(install_root)
            alias = root / "runtime-alias"
            alias.symlink_to(install_root, target_is_directory=True)
            lock = Path(f"{install_root}.lock")
            lock.mkdir()
            (lock / "pid").write_text(f"{os.getpid()}\n", encoding="ascii")

            result = self.run_uninstall(alias)

            self.assertNotEqual(0, result.returncode)
            self.assertIn(
                "another Comic Sol installer is using this install root", result.stderr
            )
            self.assertTrue((install_root / ".comic-sol-install").is_file())

    @unittest.skipUnless(os.name != "nt", "POSIX installer test")
    def test_posix_cleanup_failure_does_not_roll_back_published_sentinel(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            install_root = root / "runtime"
            (install_root / "bin").mkdir(parents=True)
            (install_root / "bin/old.txt").write_text("old", encoding="utf-8")
            (install_root / "versions/2.0.0rc4").mkdir(parents=True)
            (install_root / "versions/2.0.0rc5").mkdir(parents=True)
            self.write_marker(install_root)

            archive = root / "comic-sol.zip"
            executable = (
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  --version) echo 'comic-sol 2.0.0rc5' ;;\n"
                "  doctor) exit 0 ;;\n"
                "esac\n"
            ).encode("utf-8")
            member = zipfile.ZipInfo("comic-sol/comic-sol")
            member.create_system = 3
            member.external_attr = 0o100755 << 16
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr(member, executable)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()

            shim = root / "shim"
            shim.mkdir()
            real_rm = shutil.which("rm")
            self.assertIsNotNone(real_rm)
            rm_shim = shim / "rm"
            rm_shim.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *rollback*)\n"
                "    if grep -q '2.0.0rc5' \"$TEST_INSTALL_ROOT/.comic-sol-install\" 2>/dev/null; then\n"
                "      exit 1\n"
                "    fi\n"
                "    ;;\n"
                "esac\n"
                "exec \"$REAL_RM\" \"$@\"\n",
                encoding="utf-8",
            )
            rm_shim.chmod(0o755)
            environment = os.environ.copy()
            environment.update({
                "PATH": f"{shim}{os.pathsep}{environment['PATH']}",
                "REAL_RM": real_rm,
                "TEST_INSTALL_ROOT": str(install_root),
            })

            result = subprocess.run(
                [
                    "sh", str(self.root / "installers/install.sh"),
                    "--archive", str(archive), "--sha256", digest,
                    "--install-root", str(install_root),
                ],
                capture_output=True,
                text=True,
                check=False,
                env=environment,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(
                "2.0.0rc5", (install_root / "active-version").read_text("utf-8").strip()
            )
            marker_lines = (install_root / ".comic-sol-install").read_text("utf-8").splitlines()
            self.assertEqual("2.0.0rc5", marker_lines[1])
            self.assertIn("Could not remove rollback backup", result.stderr)


if __name__ == "__main__":
    unittest.main()
