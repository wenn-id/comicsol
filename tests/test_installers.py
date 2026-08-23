import functools
import hashlib
import http.server
import os
import re
import shlex
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from typing import cast


_TEST_SIGSTORE_BUNDLE = (
    '{"mediaType":"application/vnd.dev.sigstore.bundle+json","verification":"test-fixture"}\n'
)

# Declared host tools the POSIX installer suite executes. The suite skips with
# an explicit message instead of failing opaquely when one is unavailable.
POSIX_INSTALLER_TOOLS = ("sh", "perl", "sha256sum", "unzip", "curl", "mktemp", "mv")


def posix_installer_test(test_method):
    """Run a POSIX installer test only on POSIX hosts with all required tools."""

    @functools.wraps(test_method)
    def wrapper(self):
        if os.name == "nt":
            self.skipTest("POSIX installer test")
        missing = sorted(name for name in POSIX_INSTALLER_TOOLS if shutil.which(name) is None)
        if missing:
            self.skipTest(f"missing required host tools: {', '.join(missing)}")
        return test_method(self)

    return wrapper


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
        encoded_root = (marker_root or install_root.resolve()).as_posix().encode().hex()
        (install_root / "active-version").write_text(f"{version}\n", encoding="utf-8")
        (install_root / ".comic-sol-install").write_text(
            f"comic-sol-install-v1\n{version}\n{encoded_root}\n",
            encoding="utf-8",
        )

    @staticmethod
    def write_runtime_archive(root, version="2.0.0rc6", filename="comic-sol.zip"):
        archive = root / filename
        executable = (
            "#!/bin/sh\n"
            'case "$1" in\n'
            f"  --version) printf 'comic-sol {version}\\n' ;;\n"
            "  doctor)\n"
            '    if [ -n "${COMIC_SOL_TEST_DOCTOR_MARKER:-}" ]; then\n'
            '      printf staged > "$COMIC_SOL_TEST_DOCTOR_MARKER"\n'
            "    fi\n"
            "    exit 0\n"
            "    ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n"
        ).encode("utf-8")
        member = zipfile.ZipInfo("comic-sol/comic-sol")
        member.create_system = 3
        member.external_attr = 0o100755 << 16
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr(member, executable)
        return archive

    @staticmethod
    def find_windows_csc():
        candidates = [shutil.which("csc.exe")]
        candidates += [
            str(Path(root) / version / "csc.exe")
            for root in (
                Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64",
                Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework",
            )
            if root.is_dir()
            for version in sorted(
                (entry.name for entry in root.iterdir() if entry.is_dir()), reverse=True
            )
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return candidate
        return None

    @classmethod
    def write_windows_runtime_archive(cls, root, version="2.0.0rc6", filename="comic-sol.zip"):
        """Zip a real runnable comic-sol.exe stub built with the .NET csc tool.

        The PowerShell installer executes the archive payload, so Windows
        lifecycle tests need a native executable instead of a shell script.
        """
        csc = cls.find_windows_csc()
        if csc is None:
            raise unittest.SkipTest(
                "the .NET Framework csc.exe compiler is required to build the "
                "Windows runtime stub executable"
            )
        build_directory = root / f"stub-{version}"
        build_directory.mkdir(parents=True, exist_ok=True)
        source = build_directory / "stub.cs"
        executable = build_directory / "comic-sol.exe"
        source.write_text(
            "using System;\n"
            "using System.IO;\n"
            "class Program {\n"
            "  static int Main(string[] args) {\n"
            '    if (args.Length > 0 && args[0] == "--version") {\n'
            f'      Console.WriteLine("comic-sol {version}");\n'
            "      return 0;\n"
            "    }\n"
            '    if (args.Length > 0 && args[0] == "doctor") {\n'
            '      string marker = Environment.GetEnvironmentVariable("COMIC_SOL_TEST_DOCTOR_MARKER");\n'
            '      if (!String.IsNullOrEmpty(marker)) File.WriteAllText(marker, "staged");\n'
            "      return 0;\n"
            "    }\n"
            "    return 0;\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
        compiled = subprocess.run(
            [csc, "-nologo", "-optimize+", f"-out:{executable}", str(source)],
            capture_output=True,
            text=True,
            check=False,
        )
        if compiled.returncode != 0 or not executable.is_file():
            raise unittest.SkipTest(
                f"csc could not compile the Windows runtime stub: {compiled.stderr}"
            )
        archive = root / filename
        member = zipfile.ZipInfo("comic-sol/comic-sol.exe")
        member.create_system = 0
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr(member, executable.read_bytes())
        return archive

    @staticmethod
    def write_powershell_signature_fixture(
        root,
        archive_name,
        digest,
        *,
        bundle_payload=_TEST_SIGSTORE_BUNDLE,
        expected_identity=(
            r"^https://github\.com/wenn-id/comicsol/\.github/workflows/release\.yml@"
            r"refs/tags/v[0-9]+\.[0-9]+\.[0-9]+(rc[0-9]+)?$"
        ),
        expected_issuer="https://token.actions.githubusercontent.com",
    ):
        """Write SHA256SUMS plus a cosign.ps1 stub using only PowerShell builtins.

        The stub is a PowerShell script, not a batch file, so cmd.exe never
        re-parses the certificate identity regexp (its pipe and caret
        characters are just data). It compares every supplied argument and
        both payload digests, needing no external host tools at all.
        """
        checksums = root / "SHA256SUMS"
        signature = root / "SHA256SUMS.sigstore.json"
        checksums.write_text(f"{digest.lower()}  {archive_name}\n", encoding="utf-8")
        signature.write_text(bundle_payload, encoding="utf-8")
        fixture_directory = root / "cosign-fixture"
        fixture_directory.mkdir(parents=True, exist_ok=True)
        bundle_sha256 = hashlib.sha256(signature.read_bytes()).hexdigest().upper()
        checksums_sha256 = hashlib.sha256(checksums.read_bytes()).hexdigest().upper()
        cosign = fixture_directory / "cosign.ps1"
        cosign.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "if ($args.Count -ne 8) { exit 90 }\n"
            "if ($args[0] -ne 'verify-blob') { exit 90 }\n"
            "if ($args[1] -ne '--bundle') { exit 90 }\n"
            "if ($args[3] -ne '--certificate-identity-regexp') { exit 90 }\n"
            "if ($args[5] -ne '--certificate-oidc-issuer') { exit 90 }\n"
            f"if ($args[4] -cne '{expected_identity}') {{ exit 90 }}\n"
            f"if ($args[6] -cne '{expected_issuer}') {{ exit 90 }}\n"
            "if ((Get-FileHash -LiteralPath $args[2] -Algorithm SHA256).Hash "
            f"-ne '{bundle_sha256}') {{ exit 90 }}\n"
            "if ((Get-FileHash -LiteralPath $args[7] -Algorithm SHA256).Hash "
            f"-ne '{checksums_sha256}') {{ exit 90 }}\n"
            "exit 0\n",
            encoding="utf-8",
        )
        return checksums, signature, cosign

    def run_powershell_archive_install(
        self,
        archive,
        install_root,
        *,
        env=None,
    ):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(shell, "PowerShell is required for the Windows installer test")
        environment = os.environ.copy()
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksums, signature, cosign = self.write_powershell_signature_fixture(
            archive.parent, archive.name, digest
        )
        environment["COMIC_SOL_COSIGN"] = str(cosign)
        if env:
            environment.update(env)
        return subprocess.run(
            [
                shell,
                "-NoProfile",
                "-File",
                str(self.root / "installers/install.ps1"),
                "-Archive",
                str(archive),
                "-SHA256",
                digest,
                "-Checksums",
                str(checksums),
                "-Signature",
                str(signature),
                "-InstallRoot",
                str(install_root),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

    def run_powershell_release_install(self, archive, install_root, release, *, env=None):
        """Run pinned Windows mode with deterministic release-download fixtures."""
        shell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(shell, "PowerShell is required for the Windows installer test")
        version = release.removeprefix("v")
        archive_name = f"comic-sol-{version}-windows-x86_64.zip"
        fixture_root = archive.parent / "powershell-release-fixture"
        fixture_root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive, fixture_root / archive_name)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        identity = (
            r"^https://github\.com/wenn-id/comicsol/\.github/workflows/release\.yml@"
            f"refs/tags/{re.escape(release)}$"
        )
        _checksums, _signature, cosign = self.write_powershell_signature_fixture(
            fixture_root,
            archive_name,
            digest,
            expected_identity=identity,
        )
        # Keep production URLs immutable. This test-only script copy replaces
        # only the network transport so Windows CI can execute the remaining
        # release-mode path against local deterministic payloads.
        installer = (self.root / "installers/install.ps1").read_text(encoding="utf-8")
        download_start = installer.index("function Invoke-BoundedHttpsDownload {")
        download_end = installer.index("function Get-StrictManifestDigest {")
        fake_download = (
            "function Invoke-BoundedHttpsDownload {\n"
            "    param([string]$Uri, [string]$Destination)\n"
            "    $Parsed = [System.Uri]$Uri\n"
            "    if (-not $Parsed.IsAbsoluteUri -or $Parsed.Scheme -ne 'https') {\n"
            "        throw 'download URL must be absolute HTTPS'\n"
            "    }\n"
            "    Add-Content -LiteralPath $env:COMIC_SOL_TEST_URL_LOG -Value $Uri\n"
            "    $Source = Join-Path $env:COMIC_SOL_TEST_RELEASE_FIXTURES "
            "$Parsed.Segments[-1]\n"
            "    Copy-Item -LiteralPath $Source -Destination $Destination\n"
            "}\n"
        )
        test_installer = fixture_root / "install-release-test.ps1"
        test_installer.write_text(
            installer[:download_start] + fake_download + installer[download_end:],
            encoding="utf-8",
        )
        url_log = fixture_root / "urls.txt"
        environment = os.environ.copy()
        environment.update(
            {
                "COMIC_SOL_COSIGN": str(cosign),
                "COMIC_SOL_TEST_RELEASE_FIXTURES": str(fixture_root),
                "COMIC_SOL_TEST_URL_LOG": str(url_log),
            }
        )
        if env:
            environment.update(env)
        result = subprocess.run(
            [
                shell,
                "-NoProfile",
                "-File",
                str(test_installer),
                "-Release",
                release,
                "-InstallRoot",
                str(install_root),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        urls = url_log.read_text(encoding="utf-8").splitlines() if url_log.exists() else []
        return result, urls

    @staticmethod
    def snapshot_tree(root):
        fingerprint = {}
        for path in sorted(Path(root).rglob("*")):
            if path.is_file() and not path.is_symlink():
                fingerprint[path.relative_to(root).as_posix()] = path.read_bytes()
        return fingerprint

    @staticmethod
    def write_signature_fixture(
        root,
        archive_name,
        digest,
        *,
        bundle_payload=_TEST_SIGSTORE_BUNDLE,
        expected_identity=(
            r"^https://github\.com/wenn-id/comicsol/\.github/workflows/release\.yml@"
            r"refs/tags/v[0-9]+\.[0-9]+\.[0-9]+(rc[0-9]+)?$"
        ),
        expected_issuer="https://token.actions.githubusercontent.com",
        expected_bundle_payload=None,
    ):
        checksums = root / "SHA256SUMS"
        signature = root / "SHA256SUMS.sigstore.json"
        checksums.write_text(f"{digest.lower()}  {archive_name}\n", encoding="utf-8")
        signature.write_text(bundle_payload, encoding="utf-8")
        if expected_bundle_payload is None:
            expected_bundle_payload = bundle_payload
        cosign = root / "cosign"
        # The fixture shell is hermetic: it depends only on perl (already a
        # declared install.sh prerequisite) and never on diffutils cmp/diff.
        cosign.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "same_path() {\n"
            "  perl -MCwd=abs_path -e '\n"
            "    my ($left, $right) = @ARGV;\n"
            "    my $left_path = Cwd::abs_path($left);\n"
            "    my $right_path = Cwd::abs_path($right);\n"
            "    exit 1 unless defined $left_path && defined $right_path && $left_path eq $right_path;\n"
            '  \' "$1" "$2"\n'
            "}\n"
            'if [ "${1-}" != verify-blob ] ||\n'
            '   [ "${2-}" != --bundle ] ||\n'
            f'   ! same_path "${{3-}}" {shlex.quote(str(signature))} ||\n'
            '   [ "${4-}" != --certificate-identity-regexp ] ||\n'
            f'   [ "${{5-}}" != {shlex.quote(expected_identity)} ] ||\n'
            '   [ "${6-}" != --certificate-oidc-issuer ] ||\n'
            f'   [ "${{7-}}" != {shlex.quote(expected_issuer)} ] ||\n'
            f'   ! same_path "${{8-}}" {shlex.quote(str(checksums))}; then\n'
            "  exit 90\n"
            "fi\n"
            f"printf %s {shlex.quote(expected_bundle_payload)} | "
            "perl -e '\n"
            "  binmode STDIN;\n"
            "  local $/;\n"
            "  my $expected = <STDIN>;\n"
            '  open(my $handle, "<", $ARGV[0]) or exit 1;\n'
            "  binmode $handle;\n"
            "  my $actual = do { local $/; <$handle> };\n"
            "  exit(defined $expected && defined $actual && $expected eq $actual ? 0 : 1);\n"
            '\' "$3"\n',
            encoding="utf-8",
        )
        cosign.chmod(0o755)
        return checksums, signature, cosign

    def run_posix_archive_install(
        self,
        archive,
        install_root,
        *,
        env=None,
        bundle_payload=None,
        expected_identity=None,
        expected_issuer=None,
        expected_bundle_payload=None,
    ):
        environment = os.environ.copy()
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        fixture_options = {}
        if bundle_payload is not None:
            fixture_options["bundle_payload"] = bundle_payload
        if expected_identity is not None:
            fixture_options["expected_identity"] = expected_identity
        if expected_issuer is not None:
            fixture_options["expected_issuer"] = expected_issuer
        if expected_bundle_payload is not None:
            fixture_options["expected_bundle_payload"] = expected_bundle_payload
        checksums, signature, cosign = self.write_signature_fixture(
            archive.parent, archive.name, digest, **fixture_options
        )
        environment["COMIC_SOL_COSIGN"] = str(cosign)
        if env:
            environment.update(env)
        return subprocess.run(
            [
                "sh",
                str(self.root / "installers/install.sh"),
                "--archive",
                str(archive),
                "--sha256",
                digest,
                "--checksums",
                str(checksums),
                "--signature",
                str(signature),
                "--install-root",
                str(install_root),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

    def run_posix_release_install(
        self,
        archive,
        install_root,
        release,
        *,
        host_os="Linux",
        host_arch="x86_64",
        manifest_lines=None,
        force_shasum=False,
        env=None,
    ):
        """Run pinned release mode against deterministic curl/cosign host shims."""
        version = release.removeprefix("v")
        asset_arch = host_arch
        if host_os == "Linux" and host_arch in {"x86_64", "amd64"}:
            platform, asset_arch = "linux", "x86_64"
        elif host_os == "Darwin" and host_arch in {"arm64", "aarch64"}:
            platform, asset_arch = "macos", "arm64"
            if release in {f"v2.0.0rc{number}" for number in range(1, 5)}:
                asset_arch = "x86_64"
        else:
            platform = "unsupported"
        archive_name = f"comic-sol-{version}-{platform}-{asset_arch}.zip"
        fixture_root = archive.parent / f"release-fixture-{host_os}-{host_arch}"
        fixture_root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive, fixture_root / archive_name)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksums = fixture_root / "SHA256SUMS"
        if manifest_lines is None:
            manifest_lines = [f"{digest}  {archive_name}"]
        checksums.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
        (fixture_root / "SHA256SUMS.sigstore.json").write_text(
            _TEST_SIGSTORE_BUNDLE, encoding="utf-8"
        )
        shim = fixture_root / "shim"
        shim.mkdir()
        url_log = fixture_root / "urls.txt"
        curl = shim / "curl"
        curl.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "url=''\ndestination=''\n"
            'while [ "$#" -gt 0 ]; do\n'
            '  case "$1" in\n'
            "    https://*) url=$1; shift ;;\n"
            "    -o) destination=$2; shift 2 ;;\n"
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            '[ -n "$url" ] && [ -n "$destination" ] || exit 92\n'
            'printf \'%s\\n\' "$url" >> "$COMIC_SOL_TEST_URL_LOG"\n'
            "name=${url##*/}\n"
            'cp "$COMIC_SOL_TEST_RELEASE_FIXTURES/$name" "$destination"\n',
            encoding="utf-8",
        )
        curl.chmod(0o755)
        uname = shim / "uname"
        uname.write_text(
            "#!/bin/sh\n"
            'case "${1-}" in\n'
            '  -s) printf "%s\\n" "$COMIC_SOL_TEST_UNAME_S" ;;\n'
            '  -m) printf "%s\\n" "$COMIC_SOL_TEST_UNAME_M" ;;\n'
            "  *) exit 93 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        uname.chmod(0o755)
        identity = (
            r"^https://github\.com/wenn-id/comicsol/\.github/workflows/release\.yml@"
            f"refs/tags/{re.escape(release)}$"
        )
        cosign = shim / "cosign"
        cosign.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            'if [ "${1-}" != verify-blob ] || [ "${2-}" != --bundle ] ||\n'
            '   [ "$(basename -- "${3-}")" != SHA256SUMS.sigstore.json ] ||\n'
            '   [ "${4-}" != --certificate-identity-regexp ] ||\n'
            f'   [ "${{5-}}" != {shlex.quote(identity)} ] ||\n'
            '   [ "${6-}" != --certificate-oidc-issuer ] ||\n'
            '   [ "${7-}" != https://token.actions.githubusercontent.com ] ||\n'
            '   [ "$(basename -- "${8-}")" != SHA256SUMS ]; then\n'
            "  exit 90\n"
            "fi\n",
            encoding="utf-8",
        )
        cosign.chmod(0o755)
        environment = os.environ.copy()
        if force_shasum:
            real_shasum = shutil.which("shasum")
            if real_shasum is None:
                self.skipTest("shasum is required for the macOS checksum fallback test")
            sha256sum = shim / "sha256sum"
            sha256sum.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            sha256sum.chmod(0o755)
            shasum = shim / "shasum"
            shasum.write_text(
                "#!/bin/sh\n"
                'printf used > "$COMIC_SOL_TEST_SHASUM_MARKER"\n'
                'exec "$COMIC_SOL_TEST_REAL_SHASUM" "$@"\n',
                encoding="utf-8",
            )
            shasum.chmod(0o755)
            environment["COMIC_SOL_TEST_REAL_SHASUM"] = real_shasum
            environment["COMIC_SOL_TEST_SHASUM_MARKER"] = str(fixture_root / "shasum-used")
        environment.update(
            {
                "PATH": f"{shim}{os.pathsep}{environment['PATH']}",
                "COMIC_SOL_COSIGN": str(cosign),
                "COMIC_SOL_TEST_RELEASE_FIXTURES": str(fixture_root),
                "COMIC_SOL_TEST_URL_LOG": str(url_log),
                "COMIC_SOL_TEST_UNAME_S": host_os,
                "COMIC_SOL_TEST_UNAME_M": host_arch,
            }
        )
        if env:
            environment.update(env)
        result = subprocess.run(
            [
                "sh",
                str(self.root / "installers/install.sh"),
                "--release",
                release,
                "--install-root",
                str(install_root),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        requested_urls = (
            url_log.read_text(encoding="utf-8").splitlines() if url_log.exists() else []
        )
        return result, requested_urls, shim

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
        archive_name = Path(url.split("?", 1)[0].split("#", 1)[0]).name
        checksums, signature, cosign = self.write_signature_fixture(
            install_root.parent, archive_name, sha256
        )
        environment["COMIC_SOL_COSIGN"] = str(cosign)
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
                "--checksums",
                str(checksums),
                "--signature",
                str(signature),
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
            self.assertIn("signature verification", script.lower())
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

    def test_posix_fixture_is_hermetic_without_diffutils(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = self.write_runtime_archive(root)
            _checksums, _signature, cosign = self.write_signature_fixture(
                root, archive.name, "0" * 64
            )
            fixture = cosign.read_text(encoding="utf-8")
        # The POSIX suite must not depend on external diffutils cmp/diff; perl
        # is already a declared install.sh prerequisite.
        self.assertNotIn("cmp ", fixture)
        self.assertNotIn(" diff", fixture)
        self.assertIn("perl", fixture)
        self.assertIn("command -v perl", self.posix)

    def test_installers_enforce_https_redirects_and_normalize_digests(self):
        self.assertIn(
            "curl -fL --max-redirs 5 --proto '=https' --proto-redir '=https' --tlsv1.2",
            self.posix,
        )
        self.assertIn("tr '[:upper:]' '[:lower:]'", self.posix)
        self.assertIn("AllowAutoRedirect = $false", self.powershell)
        self.assertIn("$redirects -ge 5", self.powershell)
        self.assertIn('Scheme -ne "https"', self.powershell)
        self.assertIn("download redirect must remain HTTPS", self.powershell)

    def test_installers_accept_only_release_tag_sigstore_identities_in_manual_mode(self):
        expected = "refs/tags/v[0-9]+\\.[0-9]+\\.[0-9]+(rc[0-9]+)?"
        self.assertIn(expected, self.posix)
        self.assertIn(expected, self.powershell)
        self.assertNotIn("heads/main", self.posix)
        self.assertNotIn("heads/main", self.powershell)

    def test_release_mode_has_strict_targets_urls_redirects_and_tag_identity(self):
        self.assertIn("--release", self.posix)
        self.assertIn("-Release", self.powershell)
        self.assertIn("^v[0-9]+\\.[0-9]+\\.[0-9]+", self.posix)
        self.assertIn("\\Av[0-9]+\\.[0-9]+\\.[0-9]+", self.powershell)
        self.assertIn("(?:rc[0-9]+)?\\z", self.powershell)
        self.assertIn("Linux:x86_64", self.posix)
        self.assertIn("Darwin:arm64", self.posix)
        self.assertIn("windows-x86_64.zip", self.powershell)
        self.assertIn("https://github.com/wenn-id/comicsol/releases/download/", self.posix)
        self.assertIn("https://github.com/wenn-id/comicsol/releases/download/", self.powershell)
        self.assertIn("--max-redirs 5", self.posix)
        self.assertIn("AllowAutoRedirect = $false", self.powershell)
        self.assertIn("download redirect must remain HTTPS", self.powershell)
        self.assertIn("refs/tags/$RELEASE_IDENTITY", self.posix)
        self.assertIn("refs/tags/$EscapedRelease", self.powershell)

    @posix_installer_test
    def test_posix_release_rejects_invalid_tags_before_download(self):
        for release in (
            "latest",
            "2.0.0",
            "v2.0",
            "v2.0.0/other",
            "v2.0.0-rc1",
            "v2.0.0\njunk",
            "v2.0.0\rjunk",
            "v2.0.0\tjunk",
            "v2.0.0\n",
        ):
            with self.subTest(release=release), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                shim = root / "shim"
                shim.mkdir()
                request_marker = root / "curl-called"
                curl = shim / "curl"
                curl.write_text(
                    f"#!/bin/sh\nprintf called > {shlex.quote(str(request_marker))}\nexit 99\n",
                    encoding="utf-8",
                )
                curl.chmod(0o755)
                result = subprocess.run(
                    [
                        "sh",
                        str(self.root / "installers/install.sh"),
                        "--release",
                        release,
                        "--install-root",
                        str(root / "runtime"),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    env={
                        **os.environ,
                        "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}",
                        "COMIC_SOL_COSIGN": "true",
                    },
                )
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertIn("exact vX.Y.Z", result.stderr)
                self.assertFalse(request_marker.exists())

    @unittest.skipUnless(os.name == "nt", "PowerShell installer tag-validation test")
    def test_powershell_release_rejects_control_characters_before_download(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(shell)
        for release in ("v2.0.0\njunk", "v2.0.0\rjunk", "v2.0.0\tjunk", "v2.0.0\n"):
            with self.subTest(release=release), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                result = subprocess.run(
                    [
                        shell,
                        "-NoProfile",
                        "-File",
                        str(self.root / "installers/install.ps1"),
                        "-Release",
                        release,
                        "-InstallRoot",
                        str(root / "runtime"),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("exact vX.Y.Z", result.stderr)
                self.assertFalse((root / "runtime" / ".comic-sol-install").exists())

    @posix_installer_test
    def test_posix_release_selects_assets_verifies_staged_runtime_and_prints_doctor(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            release = "v9.8.7rc1"
            archive = self.write_runtime_archive(root, version=release[1:])
            install_root = root / "install root with spaces"
            doctor_marker = root / "staged-doctor"
            result, urls, _shim = self.run_posix_release_install(
                archive,
                install_root,
                release,
                env={"COMIC_SOL_TEST_DOCTOR_MARKER": str(doctor_marker)},
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            base = f"https://github.com/wenn-id/comicsol/releases/download/{release}"
            self.assertEqual(
                [
                    f"{base}/SHA256SUMS",
                    f"{base}/SHA256SUMS.sigstore.json",
                    f"{base}/comic-sol-{release[1:]}-linux-x86_64.zip",
                ],
                urls,
            )
            self.assertEqual("staged", doctor_marker.read_text(encoding="utf-8"))
            self.assertEqual(release[1:], (install_root / "active-version").read_text().strip())
            self.assertEqual(
                f"'{install_root.resolve()}/bin/comic-sol' doctor",
                result.stdout.splitlines()[-1],
            )

    @posix_installer_test
    def test_posix_release_failure_preserves_active_install(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            install_root = root / "runtime"
            old_archive = self.write_runtime_archive(root, version="2.0.0rc4", filename="old.zip")
            installed = self.run_posix_archive_install(old_archive, install_root)
            self.assertEqual(0, installed.returncode, installed.stdout + installed.stderr)
            before = self.snapshot_tree(install_root)

            release = "v2.0.0rc6"
            new_archive = self.write_runtime_archive(root, version=release[1:], filename="new.zip")
            archive_name = f"comic-sol-{release[1:]}-linux-x86_64.zip"
            failed, _urls, _shim = self.run_posix_release_install(
                new_archive,
                install_root,
                release,
                manifest_lines=[f"{'0' * 64}  {archive_name}"],
            )

            self.assertNotEqual(0, failed.returncode, failed.stdout + failed.stderr)
            self.assertIn("SHA256 does not match", failed.stderr)
            self.assertEqual(before, self.snapshot_tree(install_root))

    @posix_installer_test
    def test_posix_release_requires_one_strict_manifest_record(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            release = "v2.0.0rc6"
            archive = self.write_runtime_archive(root, version=release[1:])
            archive_name = f"comic-sol-{release[1:]}-linux-x86_64.zip"
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            failed, _urls, _shim = self.run_posix_release_install(
                archive,
                root / "runtime",
                release,
                manifest_lines=[
                    f"{digest}  {archive_name}",
                    f"{digest}  {archive_name}",
                ],
            )
            self.assertNotEqual(0, failed.returncode, failed.stdout + failed.stderr)
            self.assertIn("exactly one strict entry", failed.stderr)
            self.assertFalse((root / "runtime" / ".comic-sol-install").exists())

    @posix_installer_test
    def test_posix_macos_release_uses_arm64_asset_and_shasum_fallback(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            release = "v2.0.0rc6"
            archive = self.write_runtime_archive(root, version=release[1:])
            result, urls, shim = self.run_posix_release_install(
                archive,
                root / "runtime",
                release,
                host_os="Darwin",
                host_arch="arm64",
                force_shasum=True,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertTrue((shim.parent / "shasum-used").is_file())
            self.assertTrue(urls[-1].endswith(f"-{release[1:]}-macos-arm64.zip"))

    @posix_installer_test
    def test_posix_release_rejects_unsupported_target_before_download(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            release = "v2.0.0rc6"
            archive = self.write_runtime_archive(root, version=release[1:])
            result, urls, _shim = self.run_posix_release_install(
                archive,
                root / "runtime",
                release,
                host_os="Linux",
                host_arch="arm64",
            )
            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("no Comic Sol native release", result.stderr)
            self.assertEqual([], urls)

    @posix_installer_test
    def test_posix_sigstore_verifier_checks_bundle_and_claim_arguments(self):
        scenarios = (
            {"bundle_payload": "{}\n", "expected_bundle_payload": _TEST_SIGSTORE_BUNDLE},
            {"expected_identity": "wrong-identity"},
            {"expected_issuer": "https://issuer.invalid"},
        )
        for fixture_options in scenarios:
            with (
                self.subTest(fixture_options=fixture_options),
                tempfile.TemporaryDirectory() as raw,
            ):
                root = Path(raw)
                archive = self.write_runtime_archive(root)
                install_root = root / "runtime"
                result = self.run_posix_archive_install(archive, install_root, **fixture_options)

                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("signature verification failed", result.stderr)
                self.assertFalse((install_root / ".comic-sol-install").exists())

    @posix_installer_test
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

    @posix_installer_test
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

    @posix_installer_test
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
                self.assertEqual("2.0.0rc6", (install_root / "active-version").read_text().strip())
                self.assertTrue((install_root / ".comic-sol-install").is_file())
            finally:
                self.stop_installer_server(server, thread)

    @posix_installer_test
    def test_posix_url_preserves_release_asset_name_for_signed_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = self.write_runtime_archive(
                root, filename="comic-sol-2.0.0rc6-linux-x86_64.zip"
            )
            server, thread = self.start_installer_server(tls=True, payload=archive.read_bytes())
            try:
                install_root = root / "runtime"
                url = f"https://127.0.0.1:{server.server_address[1]}/{archive.name}"
                result = self.run_posix_url_install(
                    install_root,
                    url,
                    hashlib.sha256(archive.read_bytes()).hexdigest(),
                    {"CURL_CA_BUNDLE": str(cast(Path, server.certificate_path))},
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual("2.0.0rc6", (install_root / "active-version").read_text().strip())
            finally:
                self.stop_installer_server(server, thread)

    @posix_installer_test
    def test_posix_rejects_archive_when_signed_manifest_digest_differs(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = self.write_runtime_archive(root)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            checksums, signature, cosign = self.write_signature_fixture(root, archive.name, digest)
            checksums.write_text(f"{'0' * 64}  {archive.name}\n", encoding="utf-8")
            result = subprocess.run(
                [
                    "sh",
                    str(self.root / "installers/install.sh"),
                    "--archive",
                    str(archive),
                    "--sha256",
                    digest,
                    "--checksums",
                    str(checksums),
                    "--signature",
                    str(signature),
                    "--install-root",
                    str(root / "runtime"),
                ],
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "COMIC_SOL_COSIGN": str(cosign)},
            )
            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("manifest", result.stderr.lower())

    @posix_installer_test
    def test_posix_rejects_archive_when_member_listing_fails(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = self.write_runtime_archive(root)
            install_root = root / "runtime"
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            checksums, signature, cosign = self.write_signature_fixture(root, archive.name, digest)
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
                    "COMIC_SOL_COSIGN": str(cosign),
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
                    "--checksums",
                    str(checksums),
                    "--signature",
                    str(signature),
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

    @posix_installer_test
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

    @posix_installer_test
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
            self.posix.index(
                'INSTALL_LOCK_DIR="$(dirname -- "$INSTALL_ROOT_DISPLAY")/.comic-sol-install.lock"'
            ),
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

    @posix_installer_test
    def test_posix_relative_checksum_paths_survive_secure_handoff(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = self.write_runtime_archive(root)
            install_root = root / "runtime"
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            checksums, signature, cosign = self.write_signature_fixture(root, archive.name, digest)

            result = subprocess.run(
                [
                    "sh",
                    str(self.root / "installers/install.sh"),
                    "--archive",
                    archive.name,
                    "--sha256",
                    digest,
                    "--checksums",
                    checksums.name,
                    "--signature",
                    signature.name,
                    "--install-root",
                    install_root.name,
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "COMIC_SOL_COSIGN": str(cosign)},
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            marker_lines = (install_root / ".comic-sol-install").read_text("utf-8").splitlines()
            self.assertEqual(str(install_root.resolve()).encode().hex(), marker_lines[2])

            uninstall = self.run_uninstall(install_root)
            self.assertEqual(0, uninstall.returncode, uninstall.stdout + uninstall.stderr)
            self.assertFalse(install_root.exists())

    @posix_installer_test
    def test_posix_relative_archive_persists_display_root_and_uninstalls_root(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = self.write_runtime_archive(root)
            install_root = root / "runtime"
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            checksums, signature, cosign = self.write_signature_fixture(root, archive.name, digest)

            result = subprocess.run(
                [
                    "sh",
                    str(self.root / "installers/install.sh"),
                    "--archive",
                    archive.name,
                    "--sha256",
                    digest,
                    "--checksums",
                    str(checksums),
                    "--signature",
                    str(signature),
                    "--install-root",
                    install_root.name,
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "COMIC_SOL_COSIGN": str(cosign)},
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            marker_lines = (install_root / ".comic-sol-install").read_text("utf-8").splitlines()
            self.assertEqual(str(install_root.resolve()).encode().hex(), marker_lines[2])

            uninstall = self.run_uninstall(install_root)
            self.assertEqual(0, uninstall.returncode, uninstall.stdout + uninstall.stderr)
            self.assertFalse(install_root.exists())

    @posix_installer_test
    def test_posix_lifecycle_is_idempotent_and_preserves_external_state(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            install_root = root / "runtime"
            project = root / "user-project"
            project.mkdir()
            project_sentinel = project / "do-not-delete.txt"
            project_sentinel.write_text("user project", encoding="utf-8")
            config = root / "home" / ".codex" / "config.toml"
            config.parent.mkdir(parents=True)
            config.write_text("[mcp_servers.other]\ncommand = 'keep'\n", encoding="utf-8")
            archive = self.write_runtime_archive(root, version="2.0.0rc4", filename="old.zip")
            environment = {
                "COMIC_SOL_OUTPUT_ROOT": str(project),
                "HOME": str(root / "home"),
            }

            first = self.run_posix_archive_install(archive, install_root, env=environment)
            self.assertEqual(0, first.returncode, first.stdout + first.stderr)
            runtime_snapshot = {
                "marker": (install_root / ".comic-sol-install").read_bytes(),
                "active_version": (install_root / "active-version").read_bytes(),
                "bin": (install_root / "bin" / "comic-sol").read_bytes(),
                "versioned": (install_root / "versions" / "2.0.0rc4" / "comic-sol").read_bytes(),
            }
            repeated = self.run_posix_archive_install(archive, install_root, env=environment)
            self.assertEqual(0, repeated.returncode, repeated.stdout + repeated.stderr)
            self.assertEqual(
                runtime_snapshot,
                {
                    "marker": (install_root / ".comic-sol-install").read_bytes(),
                    "active_version": (install_root / "active-version").read_bytes(),
                    "bin": (install_root / "bin" / "comic-sol").read_bytes(),
                    "versioned": (
                        install_root / "versions" / "2.0.0rc4" / "comic-sol"
                    ).read_bytes(),
                },
            )

            uninstall = self.run_uninstall(install_root)
            self.assertEqual(0, uninstall.returncode, uninstall.stdout + uninstall.stderr)
            self.assertFalse(install_root.exists())
            repeated_uninstall = self.run_uninstall(install_root)
            self.assertEqual(
                0,
                repeated_uninstall.returncode,
                repeated_uninstall.stdout + repeated_uninstall.stderr,
            )
            reinstall = self.run_posix_archive_install(archive, install_root)
            self.assertEqual(0, reinstall.returncode, reinstall.stdout + reinstall.stderr)
            self.assertEqual("2.0.0rc4", (install_root / "active-version").read_text().strip())
            self.assertEqual("user project", project_sentinel.read_text(encoding="utf-8"))
            self.assertEqual("[mcp_servers.other]\ncommand = 'keep'\n", config.read_text())

    @posix_installer_test
    def test_posix_upgrade_publishes_new_version_without_touching_external_state(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            install_root = root / "runtime"
            project = root / "user-project"
            project.mkdir()
            project_sentinel = project / "do-not-delete.txt"
            project_sentinel.write_text("user project", encoding="utf-8")
            config = root / "home" / ".codex" / "config.toml"
            config.parent.mkdir(parents=True)
            config.write_text("[mcp_servers.other]\ncommand = 'keep'\n", encoding="utf-8")
            old_archive = self.write_runtime_archive(root, version="2.0.0rc4", filename="old.zip")
            new_archive = self.write_runtime_archive(root, version="2.0.0rc6", filename="new.zip")
            environment = {
                "COMIC_SOL_OUTPUT_ROOT": str(project),
                "HOME": str(root / "home"),
            }

            first = self.run_posix_archive_install(old_archive, install_root, env=environment)
            self.assertEqual(0, first.returncode, first.stdout + first.stderr)
            old_bin = (install_root / "bin" / "comic-sol").read_bytes()
            old_versioned = (install_root / "versions" / "2.0.0rc4" / "comic-sol").read_bytes()
            upgraded = self.run_posix_archive_install(new_archive, install_root, env=environment)
            self.assertEqual(0, upgraded.returncode, upgraded.stdout + upgraded.stderr)
            self.assertEqual("2.0.0rc6", (install_root / "active-version").read_text().strip())
            self.assertNotEqual(old_bin, (install_root / "bin" / "comic-sol").read_bytes())
            self.assertEqual(
                old_bin, (install_root / "versions" / "2.0.0rc4" / "comic-sol").read_bytes()
            )
            self.assertEqual(
                old_versioned, (install_root / "versions" / "2.0.0rc4" / "comic-sol").read_bytes()
            )
            self.assertFalse((install_root / "versions" / ".2.0.0rc6.rollback").exists())
            self.assertFalse((install_root / ".bin.rollback").exists())
            self.assertTrue((install_root / "versions" / "2.0.0rc4").exists())
            self.assertEqual("user project", project_sentinel.read_text(encoding="utf-8"))
            self.assertEqual("[mcp_servers.other]\ncommand = 'keep'\n", config.read_text())

    @posix_installer_test
    def test_posix_upgrade_failure_restores_runtime_and_preserves_external_state(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            install_root = root / "runtime"
            project = root / "user-project"
            project.mkdir()
            project_sentinel = project / "do-not-delete.txt"
            project_sentinel.write_text("user project", encoding="utf-8")
            config = root / "home" / ".codex" / "config.toml"
            config.parent.mkdir(parents=True)
            config.write_text("[mcp_servers.other]\ncommand = 'keep'\n", encoding="utf-8")
            old_archive = self.write_runtime_archive(root, version="2.0.0rc4", filename="old.zip")
            new_archive = self.write_runtime_archive(root, version="2.0.0rc6", filename="new.zip")
            environment = {
                "COMIC_SOL_OUTPUT_ROOT": str(project),
                "HOME": str(root / "home"),
            }
            installed = self.run_posix_archive_install(old_archive, install_root, env=environment)
            self.assertEqual(0, installed.returncode, installed.stdout + installed.stderr)
            old_bin = (install_root / "bin" / "comic-sol").read_bytes()
            old_marker = (install_root / ".comic-sol-install").read_bytes()

            shim = root / "shim"
            shim.mkdir()
            real_mv = shutil.which("mv")
            self.assertIsNotNone(real_mv)
            mv_shim = shim / "mv"
            mv_shim.write_text(
                "#!/bin/sh\n"
                "previous=''\nlast=''\n"
                'for argument in "$@"; do previous="$last"; last="$argument"; done\n'
                'if [ "$(basename "$previous")" = bin.new ] && [ "$(basename "$last")" = bin ]; then exit 91; fi\n'
                'exec "$REAL_MV" "$@"\n',
                encoding="utf-8",
            )
            mv_shim.chmod(0o755)
            failed = self.run_posix_archive_install(
                new_archive,
                install_root,
                env=environment
                | {
                    "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}",
                    "REAL_MV": cast(str, real_mv),
                },
            )
            self.assertNotEqual(0, failed.returncode, failed.stdout + failed.stderr)
            self.assertEqual("2.0.0rc4", (install_root / "active-version").read_text().strip())
            self.assertEqual(old_marker, (install_root / ".comic-sol-install").read_bytes())
            self.assertEqual(old_bin, (install_root / "bin" / "comic-sol").read_bytes())
            self.assertFalse((install_root / "bin.new").exists())
            self.assertTrue(project_sentinel.is_file())
            self.assertEqual("user project", project_sentinel.read_text(encoding="utf-8"))
            self.assertEqual("[mcp_servers.other]\ncommand = 'keep'\n", config.read_text())

    @unittest.skipUnless(os.name == "nt", "PowerShell installer release-mode test")
    def test_powershell_release_selects_asset_verifies_stage_and_prints_doctor(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            release = "v9.8.7rc1"
            archive = self.write_windows_runtime_archive(
                root,
                version=release.removeprefix("v"),
                filename="source.zip",
            )
            install_root = root / "runtime with spaces"
            doctor_marker = root / "doctor-ran"

            result, urls = self.run_powershell_release_install(
                archive,
                install_root,
                release,
                env={"COMIC_SOL_TEST_DOCTOR_MARKER": str(doctor_marker)},
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            release_base = f"https://github.com/wenn-id/comicsol/releases/download/{release}"
            self.assertEqual(
                [
                    f"{release_base}/SHA256SUMS",
                    f"{release_base}/SHA256SUMS.sigstore.json",
                    f"{release_base}/comic-sol-9.8.7rc1-windows-x86_64.zip",
                ],
                urls,
            )
            self.assertEqual("staged", doctor_marker.read_text(encoding="utf-8"))
            self.assertEqual(
                "9.8.7rc1",
                (install_root / "active-version").read_text(encoding="utf-8-sig").strip(),
            )
            self.assertTrue((install_root / "bin" / "comic-sol.exe").is_file())
            expected_doctor = f'& "{install_root.resolve()}\\bin\\comic-sol.exe" doctor'
            self.assertEqual(expected_doctor, result.stdout.strip().splitlines()[-1])

    @unittest.skipUnless(os.name == "nt", "PowerShell installer lifecycle test")
    def test_powershell_lifecycle_is_idempotent_and_preserves_external_state(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            install_root = root / "runtime"
            project = root / "user-project"
            project.mkdir()
            project_sentinel = project / "do-not-delete.txt"
            project_sentinel.write_text("user project", encoding="utf-8")
            config = root / "home" / "client-config.json"
            config.parent.mkdir(parents=True)
            config.write_text('{"client":"preserved"}\n', encoding="utf-8")
            archive = self.write_windows_runtime_archive(root, version="2.0.0rc4")
            environment = {
                "COMIC_SOL_OUTPUT_ROOT": str(project),
                "HOME": str(root / "home"),
                "USERPROFILE": str(root / "home"),
            }

            first = self.run_powershell_archive_install(archive, install_root, env=environment)
            self.assertEqual(0, first.returncode, first.stdout + first.stderr)
            runtime_snapshot = self.snapshot_tree(install_root)
            self.assertEqual(
                "2.0.0rc4",
                (install_root / "active-version").read_text(encoding="utf-8").strip(),
            )
            repeated = self.run_powershell_archive_install(archive, install_root, env=environment)
            self.assertEqual(0, repeated.returncode, repeated.stdout + repeated.stderr)
            self.assertEqual(runtime_snapshot, self.snapshot_tree(install_root))

            uninstall = self.run_uninstall(install_root)
            self.assertEqual(0, uninstall.returncode, uninstall.stdout + uninstall.stderr)
            self.assertFalse(install_root.exists())
            repeated_uninstall = self.run_uninstall(install_root)
            self.assertEqual(
                0,
                repeated_uninstall.returncode,
                repeated_uninstall.stdout + repeated_uninstall.stderr,
            )
            reinstall = self.run_powershell_archive_install(archive, install_root, env=environment)
            self.assertEqual(0, reinstall.returncode, reinstall.stdout + reinstall.stderr)
            self.assertEqual(
                "2.0.0rc4",
                (install_root / "active-version").read_text(encoding="utf-8").strip(),
            )
            self.assertEqual("user project", project_sentinel.read_text(encoding="utf-8"))
            self.assertEqual('{"client":"preserved"}\n', config.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "PowerShell installer lifecycle test")
    def test_powershell_upgrade_publishes_new_version_without_touching_external_state(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            install_root = root / "runtime"
            project = root / "user-project"
            project.mkdir()
            project_sentinel = project / "do-not-delete.txt"
            project_sentinel.write_text("user project", encoding="utf-8")
            config = root / "home" / "client-config.json"
            config.parent.mkdir(parents=True)
            config.write_text('{"client":"preserved"}\n', encoding="utf-8")
            old_archive = self.write_windows_runtime_archive(
                root, version="2.0.0rc4", filename="old.zip"
            )
            new_archive = self.write_windows_runtime_archive(
                root, version="2.0.0rc6", filename="new.zip"
            )
            environment = {
                "COMIC_SOL_OUTPUT_ROOT": str(project),
                "HOME": str(root / "home"),
                "USERPROFILE": str(root / "home"),
            }

            first = self.run_powershell_archive_install(old_archive, install_root, env=environment)
            self.assertEqual(0, first.returncode, first.stdout + first.stderr)
            old_bin = (install_root / "bin" / "comic-sol.exe").read_bytes()
            upgraded = self.run_powershell_archive_install(
                new_archive, install_root, env=environment
            )
            self.assertEqual(0, upgraded.returncode, upgraded.stdout + upgraded.stderr)
            self.assertEqual(
                "2.0.0rc6",
                (install_root / "active-version").read_text(encoding="utf-8").strip(),
            )
            self.assertNotEqual(old_bin, (install_root / "bin" / "comic-sol.exe").read_bytes())
            self.assertEqual(
                old_bin,
                (install_root / "versions" / "2.0.0rc4" / "comic-sol.exe").read_bytes(),
            )
            self.assertFalse((install_root / "versions" / ".2.0.0rc6.rollback").exists())
            self.assertFalse((install_root / ".bin.rollback").exists())
            self.assertTrue((install_root / "versions" / "2.0.0rc4").is_dir())
            self.assertEqual("user project", project_sentinel.read_text(encoding="utf-8"))
            self.assertEqual('{"client":"preserved"}\n', config.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "PowerShell installer lifecycle test")
    def test_powershell_upgrade_failure_restores_runtime_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            install_root = root / "runtime"
            project = root / "user-project"
            project.mkdir()
            project_sentinel = project / "do-not-delete.txt"
            project_sentinel.write_text("user project", encoding="utf-8")
            config = root / "home" / "client-config.json"
            config.parent.mkdir(parents=True)
            config.write_text('{"client":"preserved"}\n', encoding="utf-8")
            old_archive = self.write_windows_runtime_archive(
                root, version="2.0.0rc4", filename="old.zip"
            )
            new_archive = self.write_windows_runtime_archive(
                root, version="2.0.0rc6", filename="new.zip"
            )
            environment = {
                "COMIC_SOL_OUTPUT_ROOT": str(project),
                "HOME": str(root / "home"),
                "USERPROFILE": str(root / "home"),
            }
            installed = self.run_powershell_archive_install(
                old_archive, install_root, env=environment
            )
            self.assertEqual(0, installed.returncode, installed.stdout + installed.stderr)
            runtime_snapshot = self.snapshot_tree(install_root)

            # Hold an open handle on the installed executable so the stable
            # runtime swap fails mid-mutation and forces Restore-Install.
            locked = open(install_root / "bin" / "comic-sol.exe", "rb")
            try:
                failed = self.run_powershell_archive_install(
                    new_archive, install_root, env=environment
                )
            finally:
                locked.close()

            self.assertNotEqual(0, failed.returncode, failed.stdout + failed.stderr)
            self.assertEqual(runtime_snapshot, self.snapshot_tree(install_root))
            self.assertFalse((install_root / "bin.new").exists())
            self.assertFalse((install_root / ".bin.rollback").exists())
            self.assertFalse((install_root / "versions" / ".2.0.0rc6.rollback").exists())
            self.assertFalse((install_root / "versions" / "2.0.0rc6").exists())
            self.assertFalse((install_root / "active-version.new").exists())
            self.assertFalse((install_root / ".comic-sol-install.new").exists())
            self.assertEqual("user project", project_sentinel.read_text(encoding="utf-8"))
            self.assertEqual('{"client":"preserved"}\n', config.read_text(encoding="utf-8"))

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

    @posix_installer_test
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

    @posix_installer_test
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

    @posix_installer_test
    def test_posix_cleanup_failure_does_not_roll_back_published_sentinel(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            install_root = root / "runtime"
            (install_root / "bin").mkdir(parents=True)
            (install_root / "bin/old.txt").write_text("old", encoding="utf-8")
            (install_root / "versions/2.0.0rc4").mkdir(parents=True)
            (install_root / "versions/2.0.0rc6").mkdir(parents=True)
            self.write_marker(install_root)

            archive = root / "comic-sol.zip"
            executable = (
                "#!/bin/sh\n"
                'case "$1" in\n'
                "  --version) echo 'comic-sol 2.0.0rc6' ;;\n"
                "  doctor) exit 0 ;;\n"
                "esac\n"
            ).encode("utf-8")
            member = zipfile.ZipInfo("comic-sol/comic-sol")
            member.create_system = 3
            member.external_attr = 0o100755 << 16
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr(member, executable)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            checksums, signature, cosign = self.write_signature_fixture(root, archive.name, digest)

            shim = root / "shim"
            shim.mkdir()
            real_rm = shutil.which("rm")
            self.assertIsNotNone(real_rm)
            rm_shim = shim / "rm"
            rm_shim.write_text(
                "#!/bin/sh\n"
                'case "$*" in\n'
                "  *rollback*)\n"
                "    if grep -q '2.0.0rc6' \"$TEST_INSTALL_ROOT/.comic-sol-install\" 2>/dev/null; then\n"
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
                    "COMIC_SOL_COSIGN": str(cosign),
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
                    "--checksums",
                    str(checksums),
                    "--signature",
                    str(signature),
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
                "2.0.0rc6", (install_root / "active-version").read_text("utf-8").strip()
            )
            marker_lines = (install_root / ".comic-sol-install").read_text("utf-8").splitlines()
            self.assertEqual("2.0.0rc6", marker_lines[1])
            self.assertIn("Could not remove rollback backup", result.stderr)

    @posix_installer_test
    def test_posix_newline_install_root_round_trips_marker_and_uninstall(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = self.write_runtime_archive(root)
            install_root = root / "runtime\nname"
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            checksums, signature, cosign = self.write_signature_fixture(root, archive.name, digest)
            result = subprocess.run(
                [
                    "sh",
                    str(self.root / "installers/install.sh"),
                    "--archive",
                    archive.name,
                    "--sha256",
                    digest,
                    "--checksums",
                    str(checksums),
                    "--signature",
                    str(signature),
                    "--install-root",
                    install_root.name,
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "COMIC_SOL_COSIGN": str(cosign)},
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            uninstall = self.run_uninstall(install_root)
            self.assertEqual(0, uninstall.returncode, uninstall.stdout + uninstall.stderr)
            self.assertFalse(install_root.exists())


if __name__ == "__main__":
    unittest.main()
