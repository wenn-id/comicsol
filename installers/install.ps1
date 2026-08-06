# Install:   .\install.ps1 -Archive .\comic-sol.zip -SHA256 <digest>
# Uninstall: .\install.ps1 -Uninstall
param(
    [string]$Archive,
    [string]$Url,
    [Parameter(Mandatory=$false)][string]$SHA256,
    [string]$InstallRoot = "$HOME\AppData\Local\ComicSol",
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$Version = "2.0.0rc4"

if ($Uninstall) {
    if (Test-Path -LiteralPath $InstallRoot) { Remove-Item -LiteralPath $InstallRoot -Recurse -Force }
    Write-Output "Comic Sol runtime removed. User projects were preserved."
    exit 0
}
if (-not $SHA256) { throw "-SHA256 is required for this unsigned prerelease" }

$Temp = Join-Path ([System.IO.Path]::GetTempPath()) ("comic-sol-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $Temp | Out-Null
try {
    if ($Url) {
        $Archive = Join-Path $Temp "comic-sol.zip"
        Invoke-WebRequest -Uri $Url -OutFile $Archive -UseBasicParsing
    }
    if (-not $Archive -or -not (Test-Path -LiteralPath $Archive)) {
        throw "Provide -Archive PATH or -Url HTTPS_URL"
    }
    $Actual = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Actual -ne $SHA256.ToLowerInvariant()) { throw "SHA256 mismatch" }

    $Stage = Join-Path $Temp "stage"
    if (-not $Archive.EndsWith(".zip")) { throw "Unsupported archive; the PowerShell installer requires .zip" }

    function Test-UnsafeArchive {
        param([string]$ArchivePath)
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $reader = [System.IO.Compression.ZipFile]::OpenRead($ArchivePath)
        try {
            foreach ($entry in $reader.Entries) {
                $name = $entry.FullName
                if ($name -eq "" -or $name.EndsWith("/")) { continue }
                $normalized = $name.Replace('\', '/')
                if (-not ($normalized -eq "comic-sol" -or $normalized.StartsWith("comic-sol/"))) {
                    throw "unsafe archive member: $name"
                }
                if ($normalized -match '(^|/)\.\.(/|$)' -or $normalized -match '(^|/)\.(/|$)' -or $normalized.StartsWith("/") -or $normalized -match '^[A-Za-z]:/') {
                    throw "unsafe archive member: $name"
                }
                if (($entry.ExternalAttributes -band 0xF0000000) -eq 0xA0000000) {
                    throw "unsafe archive member: symbolic links are not allowed: $name"
                }
            }
        } finally {
            $reader.Dispose()
        }
    }
    Test-UnsafeArchive -ArchivePath $Archive
    Expand-Archive -LiteralPath $Archive -DestinationPath $Stage
    $Runtime = Join-Path $Stage "comic-sol"
    if (-not (Test-Path -LiteralPath $Runtime)) { throw "archive must contain top-level comic-sol runtime" }
    $Exe = Join-Path $Runtime "comic-sol.exe"
    & $Exe doctor --output-root $(if ($env:COMIC_SOL_OUTPUT_ROOT) { $env:COMIC_SOL_OUTPUT_ROOT } else { "$HOME\Documents\Comic Sol" })
    if ($LASTEXITCODE -ne 0) { throw "doctor verification failed" }

    $Versions = Join-Path $InstallRoot "versions"
    $Target = Join-Path $Versions $Version
    $Pending = "$Target.new"
    New-Item -ItemType Directory -Force -Path $Versions, $InstallRoot | Out-Null
    if (Test-Path $Pending) { Remove-Item $Pending -Recurse -Force }
    Move-Item $Runtime $Pending
    if (Test-Path $Target) { Remove-Item $Target -Recurse -Force }
    Move-Item $Pending $Target
    $StableRuntime = Join-Path $InstallRoot "bin"
    $PendingRuntime = Join-Path $InstallRoot "bin.new"
    $RollbackRuntime = Join-Path $InstallRoot "bin.rollback"
    if (Test-Path $PendingRuntime) { Remove-Item $PendingRuntime -Recurse -Force }
    if (Test-Path $RollbackRuntime) { Remove-Item $RollbackRuntime -Recurse -Force }
    Copy-Item $Target $PendingRuntime -Recurse
    if (Test-Path $StableRuntime) { Move-Item $StableRuntime $RollbackRuntime }
    Move-Item $PendingRuntime $StableRuntime
    if (Test-Path $RollbackRuntime) { Remove-Item $RollbackRuntime -Recurse -Force }
    Set-Content -NoNewline -Path (Join-Path $InstallRoot "active-version.new") -Value "$Version`n"
    Move-Item -Force (Join-Path $InstallRoot "active-version.new") (Join-Path $InstallRoot "active-version")

    Write-Output "Installed unsigned Comic Sol $Version at $InstallRoot"
    Write-Output "User projects are outside this directory."
} finally {
    if (Test-Path $Temp) { Remove-Item $Temp -Recurse -Force }
}
