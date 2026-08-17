import hashlib
import http.server
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from typing import cast


class _InstallerHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, server_address, handler):
        super().__init__(server_address, handler)
        self.payload: bytes = b""
        self.redirect_to: str | None = None
        self.request_count: int = 0
        self.certificate_path: Path | None = None
        self.certificate_directory: tempfile.TemporaryDirectory[str] | None = None
        self._certificate_context: ssl.SSLContext | None = None


class _InstallerRequestHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        server = cast(_InstallerHTTPServer, self.server)
        server.request_count += 1
        redirect_to = server.redirect_to
        if redirect_to is not None:
            self.send_response(302)
            self.send_header("Location", redirect_to)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(server.payload)))
        self.end_headers()
        self.wfile.write(server.payload)

    def log_message(self, format, *args):
        return


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

    @staticmethod
    def write_runtime_archive(root, version="2.0.0rc5"):
        archive = root / "comic-sol.zip"
        executable = (
            "#!/bin/sh\n"
            'case "$1" in\n'
            f"  --version) printf 'comic-sol {version}\\n' ;;\n"
            "  doctor) exit 0 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n"
        ).encode("utf-8")
        member = zipfile.ZipInfo("comic-sol/comic-sol")
        member.create_system = 3
        member.external_attr = 0o100755 << 16
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr(member, executable)
        return archive

    def start_installer_server(self, *, tls=False, payload=b""):
        server = _InstallerHTTPServer(("127.0.0.1", 0), _InstallerRequestHandler)
        server.payload = payload
        if tls:
            openssl = shutil.which("openssl")
            if openssl is None:
                server.server_close()
                self.skipTest("openssl is required for HTTPS installer behavior tests")
            certificate_directory = tempfile.TemporaryDirectory()
            certificate_root = Path(certificate_directory.name)
            certificate = certificate_root / "server.crt"
            key = certificate_root / "server.key"
            result = subprocess.run(
                [
                    openssl,
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-keyout",
                    str(key),
                    "-out",
                    str(certificate),
                    "-sha256",
                    "-days",
                    "1",
                    "-nodes",
                    "-subj",
                    "/CN=127.0.0.1",
                    "-addext",
                    "subjectAltName=IP:127.0.0.1",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                certificate_directory.cleanup()
                server.server_close()
                self.skipTest(f"openssl cannot create test certificate: {result.stderr}")
            tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
            tls_context.load_cert_chain(certificate, key)
            server.socket = tls_context.wrap_socket(server.socket, server_side=True)
            server.certificate_path = certificate
            server.certificate_directory = certificate_directory
            server._certificate_context = tls_context
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def run_posix_url_install(self, install_root, url, sha256, env=None):
        environment = os.environ.copy()
        if env:
            environment.update(env)
        return subprocess.run(
            [
                "sh",
                str(self.root / "installers/install.sh"),
                "--url",
                url,
                "--sha256",
                sha256,
                "--install-root",
                str(install_root),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

    @staticmethod
    def stop_installer_server(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if server.certificate_directory is not None:
            server.certificate_directory.cleanup()

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
        self.assertIn("command -v perl", self.posix)
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

    def test_installers_enforce_https_redirects_and_normalize_digests(self):
        self.assertIn("curl -fL --proto '=https' --proto-redir '=https' --tlsv1.2", self.posix)
        self.assertIn("tr '[:upper:]' '[:lower:]'", self.posix)
        self.assertIn("MaximumRedirection 0", self.powershell)
        self.assertNotIn("MaximumRedirection 1", self.powershell)
        self.assertIn("Scheme", self.powershell)
        self.assertIn("https", self.powershell.lower())

    @unittest.skipUnless(os.name != "nt", "POSIX installer test")
    def test_posix_rejects_http_url_before_request(self):
        server, thread = self.start_installer_server()
        try:
            with tempfile.TemporaryDirectory() as raw:
                install_root = Path(raw) / "runtime"
                url = f"http://127.0.0.1:{server.server_address[1]}/comic-sol.zip"
                result = self.run_posix_url_install(install_root, url, "0" * 64)

                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual(0, server.request_count)
                self.assertFalse((install_root / ".comic-sol-install").exists())
        finally:
            self.stop_installer_server(server, thread)

    @unittest.skipUnless(os.name != "nt", "POSIX installer test")
    def test_posix_rejects_https_to_http_redirect(self):
        http_server, http_thread = self.start_installer_server()
        https_server = https_thread = None
        try:
            https_server, https_thread = self.start_installer_server(tls=True)
            https_server.redirect_to = (
                f"http://127.0.0.1:{http_server.server_address[1]}/comic-sol.zip"
            )
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                install_root = root / "runtime"
                url = f"https://127.0.0.1:{https_server.server_address[1]}/comic-sol.zip"
                result = self.run_posix_url_install(
                    install_root,
                    url,
                    "0" * 64,
                    {"CURL_CA_BUNDLE": str(cast(Path, https_server.certificate_path))},
                )

                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual(1, https_server.request_count)
                self.assertEqual(0, http_server.request_count)
                self.assertFalse((install_root / ".comic-sol-install").exists())
        finally:
            if https_server is not None:
                self.stop_installer_server(https_server, https_thread)
            self.stop_installer_server(http_server, http_thread)

    @unittest.skipUnless(os.name != "nt", "POSIX installer test")
    def test_posix_accepts_uppercase_digest_for_real_archive_download(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = self.write_runtime_archive(root)
            server, thread = self.start_installer_server(tls=True, payload=archive.read_bytes())
            try:
                install_root = root / "runtime"
                url = f"https://127.0.0.1:{server.server_address[1]}/comic-sol.zip"
                result = self.run_posix_url_install(
                    install_root,
                    url,
                    hashlib.sha256(archive.read_bytes()).hexdigest().upper(),
                    {"CURL_CA_BUNDLE": str(cast(Path, server.certificate_path))},
                )

                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual(1, server.request_count)
                self.assertEqual("2.0.0rc5", (install_root / "active-version").read_text().strip())
                self.assertTrue((install_root / ".comic-sol-install").is_file())
            finally:
                self.stop_installer_server(server, thread)

    @unittest.skipUnless(os.name != "nt", "POSIX installer test")
    def test_posix_rejects_archive_when_member_listing_fails(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = self.write_runtime_archive(root)
            install_root = root / "runtime"
            shim = root / "shim"
            shim.mkdir()
            real_unzip = shutil.which("unzip")
            self.assertIsNotNone(real_unzip)
            unzip_shim = shim / "unzip"
            unzip_shim.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "-Z1" ]; then\n'
                '  printf "%s\\n" "simulated listing failure" >&2\n'
                "  exit 1\n"
                "fi\n"
                'exec "$REAL_UNZIP" "$@"\n',
                encoding="utf-8",
            )
            unzip_shim.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{shim}{os.pathsep}{environment['PATH']}",
                    "REAL_UNZIP": cast(str, real_unzip),
                }
            )
            result = subprocess.run(
                [
                    "sh",
                    str(self.root / "installers/install.sh"),
                    "--archive",
                    str(archive),
                    "--sha256",
                    hashlib.sha256(archive.read_bytes()).hexdigest(),
                    "--install-root",
                    str(install_root),
                ],
                capture_output=True,
                text=True,
                check=False,
                env=environment,
            )

            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("archive member validation failed", result.stderr)
            self.assertFalse((install_root / ".comic-sol-install").exists())
            self.assertFalse((install_root / "bin").exists())

    @unittest.skipUnless(os.name != "nt", "POSIX installer test")
    def test_posix_rejects_symlinked_install_root(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = self.write_runtime_archive(root)
            target = root / "target"
            target.mkdir()
            install_root = root / "install-root"
            install_root.symlink_to(target, target_is_directory=True)
            result = subprocess.run(
                [
                    "sh",
                    str(self.root / "installers/install.sh"),
                    "--archive",
                    str(archive),
                    "--sha256",
                    hashlib.sha256(archive.read_bytes()).hexdigest(),
                    "--install-root",
                    str(install_root),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("symlink", result.stderr.lower())
            self.assertFalse((target / ".comic-sol-install").exists())
            self.assertFalse((target / "bin").exists())

    @unittest.skipUnless(os.name != "nt", "POSIX installer test")
    def test_posix_rejects_symlinked_install_ancestor(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = self.write_runtime_archive(root)
            target_parent = root / "target-parent"
            target_parent.mkdir()
            symlink_parent = root / "symlink-parent"
            symlink_parent.symlink_to(target_parent, target_is_directory=True)
            install_root = symlink_parent / "runtime"
            result = subprocess.run(
                [
                    "sh",
                    str(self.root / "installers/install.sh"),
                    "--archive",
                    str(archive),
                    "--sha256",
                    hashlib.sha256(archive.read_bytes()).hexdigest(),
                    "--install-root",
                    str(install_root),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("symlink", result.stderr.lower())
            self.assertFalse((target_parent / "runtime" / ".comic-sol-install").exists())
            self.assertFalse((target_parent / "runtime" / "bin").exists())

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
            self.posix.index('INSTALL_LOCK_DIR="$INSTALL_ROOT/.comic-sol-install.lock"'),
        )
        self.assertLess(
            self.posix.index('rmdir -- "$install_root_name"'),
            self.posix.index('release_install_lock\n  echo "Comic Sol runtime removed'),
        )
        self.assertLess(
            self.powershell.index("$InstallRoot = Resolve-CanonicalInstallRoot -Path $InstallRoot"),
            self.powershell.index("Acquire-InstallMutex", self.powershell.index("if ($Uninstall)")),
        )
        self.assertIn("ReparsePoint", self.powershell)

    def test_posix_sentinel_publication_is_the_commit_point(self):
        marker = self.posix.index(
            'mv -- "$INSTALL_ROOT/.comic-sol-install.new" "$INSTALL_ROOT/$INSTALL_MARKER_NAME"'
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

    @unittest.skipUnless(os.name != "nt", "POSIX installer test")
    def test_posix_relative_archive_persists_display_root_and_uninstalls_root(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = self.write_runtime_archive(root)
            install_root = root / "runtime"
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()

            result = subprocess.run(
                [
                    "sh",
                    str(self.root / "installers/install.sh"),
                    "--archive",
                    archive.name,
                    "--sha256",
                    digest,
                    "--install-root",
                    install_root.name,
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            marker_lines = (install_root / ".comic-sol-install").read_text("utf-8").splitlines()
            self.assertEqual(str(install_root.resolve()), marker_lines[2])

            uninstall = self.run_uninstall(install_root)
            self.assertEqual(0, uninstall.returncode, uninstall.stdout + uninstall.stderr)
            self.assertFalse(install_root.exists())

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
                    shell,
                    "-NoProfile",
                    "-File",
                    str(runner),
                    "-Installer",
                    str(self.root / "installers/install.ps1"),
                    "-Location",
                    str(powershell_location),
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
            self.write_marker(install_root)
            lock = install_root.parent / ".comic-sol-install.lock"
            lock.mkdir(parents=True)
            (lock / "pid").write_text(f"{os.getpid()}\n", encoding="ascii")
            result = subprocess.run(
                [
                    "sh",
                    str(self.root / "installers/install.sh"),
                    "--install-root",
                    str(install_root),
                    "--uninstall",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("another Comic Sol installer is using this install root", result.stderr)
            self.assertTrue(lock.is_dir())

    @unittest.skipUnless(os.name != "nt", "POSIX installer test")
    def test_posix_alias_rejects_symlinked_install_root_before_locking(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            install_root = root / "runtime"
            install_root.mkdir()
            self.write_marker(install_root)
            alias = root / "runtime-alias"
            alias.symlink_to(install_root, target_is_directory=True)
            lock = install_root / ".comic-sol-install.lock"
            lock.mkdir()
            (lock / "pid").write_text(f"{os.getpid()}\n", encoding="ascii")

            result = self.run_uninstall(alias)

            self.assertNotEqual(0, result.returncode)
            self.assertIn("symlink", result.stderr.lower())
            self.assertTrue((install_root / ".comic-sol-install").is_file())
            self.assertTrue(lock.is_dir())

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
                'case "$1" in\n'
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
                'case "$*" in\n'
                "  *rollback*)\n"
                "    if grep -q '2.0.0rc5' \"$TEST_INSTALL_ROOT/.comic-sol-install\" 2>/dev/null; then\n"
                "      exit 1\n"
                "    fi\n"
                "    ;;\n"
                "esac\n"
                'exec "$REAL_RM" "$@"\n',
                encoding="utf-8",
            )
            rm_shim.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{shim}{os.pathsep}{environment['PATH']}",
                    "REAL_RM": real_rm,
                    "TEST_INSTALL_ROOT": str(install_root),
                }
            )

            result = subprocess.run(
                [
                    "sh",
                    str(self.root / "installers/install.sh"),
                    "--archive",
                    str(archive),
                    "--sha256",
                    digest,
                    "--install-root",
                    str(install_root),
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
