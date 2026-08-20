# Install:   .\install.ps1 -Archive .\comic-sol.zip -SHA256 <digest> -Checksums .\SHA256SUMS -Signature .\SHA256SUMS.sigstore.json
# Uninstall: .\install.ps1 -Uninstall
param(
    [string]$Archive,
    [string]$Url,
    [Parameter(Mandatory=$false)][string]$SHA256,
    [Parameter(Mandatory=$false)][string]$Checksums,
    [Parameter(Mandatory=$false)][string]$Signature,
    [string]$InstallRoot = "$HOME\AppData\Local\ComicSol",
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$InstallMutex = $null
$InstallMarkerName = ".comic-sol-install"
$InstallMarkerMagic = "comic-sol-install-v1"
$CosignCommand = if ($env:COMIC_SOL_COSIGN) { $env:COMIC_SOL_COSIGN } else { "cosign" }
$CosignOidcIssuer = "https://token.actions.githubusercontent.com"
$CosignIdentityRegexp = '^https://github\.com/wenn-id/comicsol/\.github/workflows/release\.yml@refs/tags/v[0-9]+\.[0-9]+\.[0-9]+(rc[0-9]+)?$'
function Encode-MarkerRoot {
    param([string]$Path)
    $normalized = $Path.Replace('\', '/')
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($normalized)
    return (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
}
function Acquire-InstallMutex {
    $name = "ComicSol-Install-" + (($InstallRoot.ToLowerInvariant()) -replace '[^A-Za-z0-9]', '_')
    $script:InstallMutex = New-Object System.Threading.Mutex($false, $name)
    try {
        if (-not $script:InstallMutex.WaitOne(0)) {
            throw "another Comic Sol installer is using this install root"
        }
    } catch [System.Threading.AbandonedMutexException] {
        # Previous installer died; ownership transferred to this process.
    }
}
function Release-InstallMutex {
    if ($script:InstallMutex) {
        try { $script:InstallMutex.ReleaseMutex() } catch [System.Threading.ApplicationException] { }
        $script:InstallMutex.Dispose()
        $script:InstallMutex = $null
    }
}
function Resolve-CanonicalInstallRoot {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "install root is not a directory"
    }
    $providerPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    $absolute = [System.IO.Path]::GetFullPath($providerPath)
    $item = Get-Item -Force -LiteralPath $absolute
    while ($item) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "install root must not contain symlinks or reparse points"
        }
        $item = $item.Parent
    }
    return (Resolve-Path -LiteralPath $absolute).Path
}
function Test-SensitiveInstallRoot {
    param([string]$Path)
    $comparison = [System.StringComparison]::OrdinalIgnoreCase
    $trimmed = $Path.TrimEnd([char[]]'\/')
    $volumeRoot = [System.IO.Path]::GetPathRoot($Path).TrimEnd([char[]]'\/')
    if ($trimmed.Equals($volumeRoot, $comparison)) { return $true }
    if ($HOME) {
        $homePath = [System.IO.Path]::GetFullPath($HOME).TrimEnd([char[]]'\/')
        if ($trimmed.Equals($homePath, $comparison)) { return $true }
    }
    $currentPath = (Get-Location).ProviderPath.TrimEnd([char[]]'\/')
    if ($trimmed.Equals($currentPath, $comparison)) { return $true }
    return (Test-Path -LiteralPath (Join-Path $Path ".git")) -or
        (Test-Path -LiteralPath (Join-Path $Path "project.json"))
}

if ($Uninstall) {
    if (-not (Test-Path -LiteralPath $InstallRoot)) {
        Write-Output "Comic Sol runtime is already removed. User projects were preserved."
        exit 0
    }
    $InstallRoot = Resolve-CanonicalInstallRoot -Path $InstallRoot
    Acquire-InstallMutex
    try {
        if (Test-SensitiveInstallRoot -Path $InstallRoot) {
            throw "refusing to uninstall from a filesystem root, home, current directory, repository, or Comic Sol project root"
        }

        $Marker = Join-Path $InstallRoot $InstallMarkerName
        $Pointer = Join-Path $InstallRoot "active-version"
        if (-not (Test-Path -LiteralPath $Marker -PathType Leaf) -or
            -not (Test-Path -LiteralPath $Pointer -PathType Leaf)) {
            throw "refusing to uninstall: install root is not a registered Comic Sol runtime; reinstall or upgrade this root first"
        }
        $MarkerLines = @(Get-Content -LiteralPath $Marker)
        $ActiveVersion = (Get-Content -Raw -LiteralPath $Pointer).Trim()
        $comparison = [System.StringComparison]::OrdinalIgnoreCase
        $ExpectedMarkerRoot = Encode-MarkerRoot -Path $InstallRoot
        if ($MarkerLines.Count -ne 3 -or
            $MarkerLines[0] -ne $InstallMarkerMagic -or
            -not $MarkerLines[1] -or
            $MarkerLines[1] -ne $ActiveVersion -or
            (-not $MarkerLines[2].Equals($ExpectedMarkerRoot, $comparison) -and
             -not $MarkerLines[2].Equals($InstallRoot, $comparison))) {
            throw "refusing to uninstall: install registration is invalid; reinstall or upgrade this root first"
        }

        foreach ($Child in @("bin", "versions", ".bin.rollback", "bin.new")) {
            $ManagedPath = Join-Path $InstallRoot $Child
            if (Test-Path -LiteralPath $ManagedPath) { Remove-Item -LiteralPath $ManagedPath -Recurse -Force }
        }
        foreach ($Child in @("active-version.new", ".comic-sol-install.new", "active-version", $InstallMarkerName)) {
            $ManagedPath = Join-Path $InstallRoot $Child
            if (Test-Path -LiteralPath $ManagedPath) { Remove-Item -LiteralPath $ManagedPath -Force }
        }
        if (-not (Get-ChildItem -Force -LiteralPath $InstallRoot | Select-Object -First 1)) {
            Remove-Item -LiteralPath $InstallRoot -Force
        }
        Write-Output "Comic Sol runtime removed. User projects were preserved."
    } finally {
        Release-InstallMutex
    }
    exit 0
}
if (-not $SHA256 -or -not $Checksums -or -not $Signature) { throw "-SHA256, -Checksums, and -Signature are required for a signed release" }
if (-not (Get-Command $CosignCommand -ErrorAction SilentlyContinue)) { throw "cosign is required for signature verification" }
if (-not (Test-Path -LiteralPath $Checksums -PathType Leaf) -or -not (Test-Path -LiteralPath $Signature -PathType Leaf)) {
    throw "checksum manifest and Sigstore bundle are required for signature verification"
}
& $CosignCommand verify-blob --bundle $Signature --certificate-identity-regexp $CosignIdentityRegexp --certificate-oidc-issuer $CosignOidcIssuer $Checksums | Out-Null
if ($LASTEXITCODE -ne 0) { throw "signature verification failed" }
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
$InstallRoot = Resolve-CanonicalInstallRoot -Path $InstallRoot
Acquire-InstallMutex

$Temp = Join-Path ([System.IO.Path]::GetTempPath()) ("comic-sol-" + [guid]::NewGuid())
$Committed = $false
$InstallStarted = $false
$PreviousVersion = $null
$HadPointer = $false
$Target = $null
$TargetBackup = $null
$StableRuntime = $null
$StableBackup = $null
$StablePublished = $false
$TargetPublished = $false
function Restore-Install {
    if (-not $InstallStarted -or $Committed) { return }
    if ($StableBackup -and (Test-Path -LiteralPath $StableBackup)) {
        if (Test-Path -LiteralPath $StableRuntime) { Remove-Item -LiteralPath $StableRuntime -Recurse -Force }
        Move-Item -LiteralPath $StableBackup -Destination $StableRuntime
    } elseif ($StablePublished -and (Test-Path -LiteralPath $StableRuntime)) {
        Remove-Item -LiteralPath $StableRuntime -Recurse -Force
    }
    if ($TargetBackup -and (Test-Path -LiteralPath $TargetBackup)) {
        if (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target -Recurse -Force }
        Move-Item -LiteralPath $TargetBackup -Destination $Target
    } elseif ($TargetPublished -and (Test-Path -LiteralPath $Target)) {
        Remove-Item -LiteralPath $Target -Recurse -Force
    }
    if ($Target) { Remove-Item -LiteralPath "$Target.new" -Recurse -Force -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath (Join-Path $InstallRoot "bin.new") -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $InstallRoot "active-version.new") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $InstallRoot ".comic-sol-install.new") -Force -ErrorAction SilentlyContinue
    $Pointer = Join-Path $InstallRoot "active-version"
    if ($HadPointer) { Set-Content -NoNewline -LiteralPath $Pointer -Value "$PreviousVersion`n" -Encoding utf8 }
    elseif (Test-Path -LiteralPath $Pointer) { Remove-Item -LiteralPath $Pointer -Force }
}

try {
    New-Item -ItemType Directory -Path $Temp | Out-Null
    if ($Url) {
        $parsedUrl = [System.Uri]$Url
        if (-not $parsedUrl.IsAbsoluteUri -or $parsedUrl.Scheme -ne "https") {
            throw "-Url must be an absolute HTTPS URL"
        }
        $archivePath = $parsedUrl.AbsolutePath
        $archiveName = [System.IO.Path]::GetFileName($archivePath)
        if (-not $archiveName -or $archiveName -in @('.', '..') -or $archiveName -notmatch '^[A-Za-z0-9._-]+$') {
            throw "-Url must end with a safe release asset name"
        }
        $Archive = Join-Path $Temp $archiveName
        Invoke-WebRequest -Uri $parsedUrl -OutFile $Archive -UseBasicParsing -MaximumRedirection 0
    }
    if (-not $Archive -or -not (Test-Path -LiteralPath $Archive)) { throw "Provide -Archive PATH or -Url HTTPS_URL" }
    $Actual = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
    $ArchiveName = [System.IO.Path]::GetFileName($Archive)
    $ManifestDigest = $null
    foreach ($Line in Get-Content -LiteralPath $Checksums) {
        $Parts = $Line -split "  ", 2
        if ($Parts.Count -eq 2 -and [System.IO.Path]::GetFileName($Parts[1]) -eq $ArchiveName) {
            if ($ManifestDigest -and $ManifestDigest -ne $Parts[0].ToLowerInvariant()) { throw "signed checksum manifest has duplicate archive entries" }
            $ManifestDigest = $Parts[0].ToLowerInvariant()
        }
    }
    if (-not $ManifestDigest) { throw "signed checksum manifest has no entry for archive" }
    if ($Actual -ne $ManifestDigest -or $Actual -ne $SHA256.ToLowerInvariant()) { throw "SHA256 does not match signed checksum manifest" }
    if (-not $Archive.EndsWith(".zip")) { throw "Unsupported archive; the PowerShell installer requires .zip" }

    function Test-UnsafeArchive {
        param([string]$ArchivePath)
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $reader = [System.IO.Compression.ZipFile]::OpenRead($ArchivePath)
        try {
            foreach ($entry in $reader.Entries) {
                $name = $entry.FullName
                $normalized = $name.Replace('\', '/')
                if ($normalized -eq "") { continue }
                if (-not ($normalized -eq "comic-sol" -or $normalized.StartsWith("comic-sol/"))) { throw "unsafe archive member: $name" }
                if ($normalized -match '(^|/)\.\.(/|$)' -or $normalized -match '(^|/)\.(/|$)' -or $normalized.StartsWith("/") -or $normalized -match '^[A-Za-z]:/') { throw "unsafe archive member: $name" }
                if (($entry.ExternalAttributes -band 0xF0000000) -eq 0xA0000000) { throw "unsafe archive member: symbolic links are not allowed: $name" }
            }
        } finally { $reader.Dispose() }
    }
    Test-UnsafeArchive -ArchivePath $Archive
    $Stage = Join-Path $Temp "stage"
    Expand-Archive -LiteralPath $Archive -DestinationPath $Stage
    $Runtime = Join-Path $Stage "comic-sol"
    $Exe = Join-Path $Runtime "comic-sol.exe"
    if (-not (Test-Path -LiteralPath $Exe)) { throw "archive executable is missing" }
    $VersionOutput = & $Exe --version
    $VersionLine = [string]($VersionOutput | Select-Object -First 1)
    $VersionLine = $VersionLine.Trim()
    if ($LASTEXITCODE -ne 0 -or $VersionLine -notmatch '^comic-sol ([0-9]+\.[0-9]+\.[0-9]+(?:rc[0-9]+)?)$') { throw "unable to determine a valid runtime version" }
    $Version = $Matches[1]
    & $Exe doctor --output-root $(if ($env:COMIC_SOL_OUTPUT_ROOT) { $env:COMIC_SOL_OUTPUT_ROOT } else { "$HOME\Documents\Comic Sol" })
    if ($LASTEXITCODE -ne 0) { throw "doctor verification failed" }

    $InstallStarted = $true
    $Versions = Join-Path $InstallRoot "versions"
    $Target = Join-Path $Versions $Version
    $TargetBackup = Join-Path $Versions ("." + $Version + ".rollback")
    $StableRuntime = Join-Path $InstallRoot "bin"
    $StableBackup = Join-Path $InstallRoot ".bin.rollback"
    New-Item -ItemType Directory -Force -Path $Versions | Out-Null
    $Pointer = Join-Path $InstallRoot "active-version"
    if (Test-Path -LiteralPath $Pointer) { $HadPointer = $true; $PreviousVersion = (Get-Content -Raw -LiteralPath $Pointer).Trim() }
    foreach ($Path in @($TargetBackup, $StableBackup, "$Target.new", (Join-Path $InstallRoot "bin.new"))) { if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force } }
    Move-Item -LiteralPath $Runtime -Destination "$Target.new"
    if (Test-Path -LiteralPath $Target) { Move-Item -LiteralPath $Target -Destination $TargetBackup }
    Move-Item -LiteralPath "$Target.new" -Destination $Target
    $TargetPublished = $true
    Copy-Item -LiteralPath $Target -Destination (Join-Path $InstallRoot "bin.new") -Recurse
    if (Test-Path -LiteralPath $StableRuntime) { Move-Item -LiteralPath $StableRuntime -Destination $StableBackup }
    Move-Item -LiteralPath (Join-Path $InstallRoot "bin.new") -Destination $StableRuntime
    $StablePublished = $true
    Set-Content -NoNewline -LiteralPath (Join-Path $InstallRoot "active-version.new") -Value "$Version`n" -Encoding utf8
    Move-Item -Force -LiteralPath (Join-Path $InstallRoot "active-version.new") -Destination $Pointer
    $MarkerNew = Join-Path $InstallRoot ".comic-sol-install.new"
    Set-Content -LiteralPath $MarkerNew -Value @($InstallMarkerMagic, $Version, (Encode-MarkerRoot -Path $InstallRoot)) -Encoding utf8
    Move-Item -Force -LiteralPath $MarkerNew -Destination (Join-Path $InstallRoot $InstallMarkerName)
    $Committed = $true
    foreach ($Path in @($StableBackup, $TargetBackup)) {
        if (Test-Path -LiteralPath $Path) {
            try { Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop }
            catch { Write-Warning "Could not remove rollback backup '$Path': $($_.Exception.Message)" }
        }
    }
    Write-Output "Installed signed Comic Sol $Version at $InstallRoot"
    Write-Output "User projects are outside this directory."
} catch {
    $originalError = $_
    try { Restore-Install }
    catch { Write-Error "Rollback failed: $($_.Exception.Message)" }
    throw $originalError
} finally {
    try {
        if (Test-Path -LiteralPath $Temp) { Remove-Item -LiteralPath $Temp -Recurse -Force -ErrorAction Stop }
    } catch { Write-Warning "Temporary cleanup failed: $($_.Exception.Message)" }
    Release-InstallMutex
}
