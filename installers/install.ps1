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

if ($Uninstall) {
    if (Test-Path -LiteralPath $InstallRoot) { Remove-Item -LiteralPath $InstallRoot -Recurse -Force }
    Write-Output "Comic Sol runtime removed. User projects were preserved."
    exit 0
}
if (-not $SHA256) { throw "-SHA256 is required for this unsigned prerelease" }

$Temp = Join-Path ([System.IO.Path]::GetTempPath()) ("comic-sol-" + [guid]::NewGuid())
$Committed = $false
$InstallStarted = $false
$PreviousVersion = $null
$HadPointer = $false
$Target = $null
$TargetBackup = $null
$StableRuntime = $null
$StableBackup = $null
function Restore-Install {
    if (-not $InstallStarted -or $Committed) { return }
    if ($StableRuntime -and (Test-Path -LiteralPath $StableRuntime)) { Remove-Item -LiteralPath $StableRuntime -Recurse -Force }
    if ($StableBackup -and (Test-Path -LiteralPath $StableBackup)) { Move-Item -LiteralPath $StableBackup -Destination $StableRuntime }
    if ($Target -and (Test-Path -LiteralPath $Target)) { Remove-Item -LiteralPath $Target -Recurse -Force }
    if ($TargetBackup -and (Test-Path -LiteralPath $TargetBackup)) { Move-Item -LiteralPath $TargetBackup -Destination $Target }
    $Pointer = Join-Path $InstallRoot "active-version"
    if ($HadPointer) { Set-Content -NoNewline -LiteralPath $Pointer -Value "$PreviousVersion`n" }
    elseif (Test-Path -LiteralPath $Pointer) { Remove-Item -LiteralPath $Pointer -Force }
}

New-Item -ItemType Directory -Path $Temp | Out-Null
try {
    if ($Url) {
        $Archive = Join-Path $Temp "comic-sol.zip"
        Invoke-WebRequest -Uri $Url -OutFile $Archive -UseBasicParsing
    }
    if (-not $Archive -or -not (Test-Path -LiteralPath $Archive)) { throw "Provide -Archive PATH or -Url HTTPS_URL" }
    $Actual = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Actual -ne $SHA256.ToLowerInvariant()) { throw "SHA256 mismatch" }
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
    if ($LASTEXITCODE -ne 0 -or $VersionOutput -notmatch '^comic-sol ([0-9]+\.[0-9]+\.[0-9]+(?:rc[0-9]+)?)$') { throw "unable to determine a valid runtime version" }
    $Version = $Matches[1]
    & $Exe doctor --output-root $(if ($env:COMIC_SOL_OUTPUT_ROOT) { $env:COMIC_SOL_OUTPUT_ROOT } else { "$HOME\Documents\Comic Sol" })
    if ($LASTEXITCODE -ne 0) { throw "doctor verification failed" }

    $InstallStarted = $true
    $Versions = Join-Path $InstallRoot "versions"
    $Target = Join-Path $Versions $Version
    $TargetBackup = Join-Path $Versions ("." + $Version + ".rollback")
    $StableRuntime = Join-Path $InstallRoot "bin"
    $StableBackup = Join-Path $InstallRoot ".bin.rollback"
    New-Item -ItemType Directory -Force -Path $Versions, $InstallRoot | Out-Null
    $Pointer = Join-Path $InstallRoot "active-version"
    if (Test-Path -LiteralPath $Pointer) { $HadPointer = $true; $PreviousVersion = (Get-Content -Raw -LiteralPath $Pointer).Trim() }
    foreach ($Path in @($TargetBackup, $StableBackup, "$Target.new", (Join-Path $InstallRoot "bin.new"))) { if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force } }
    Move-Item -LiteralPath $Runtime -Destination "$Target.new"
    if (Test-Path -LiteralPath $Target) { Move-Item -LiteralPath $Target -Destination $TargetBackup }
    Move-Item -LiteralPath "$Target.new" -Destination $Target
    Copy-Item -LiteralPath $Target -Destination (Join-Path $InstallRoot "bin.new") -Recurse
    if (Test-Path -LiteralPath $StableRuntime) { Move-Item -LiteralPath $StableRuntime -Destination $StableBackup }
    Move-Item -LiteralPath (Join-Path $InstallRoot "bin.new") -Destination $StableRuntime
    Set-Content -NoNewline -LiteralPath (Join-Path $InstallRoot "active-version.new") -Value "$Version`n"
    Move-Item -Force -LiteralPath (Join-Path $InstallRoot "active-version.new") -Destination $Pointer
    foreach ($Path in @($StableBackup, $TargetBackup)) { if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force } }
    $Committed = $true
    Write-Output "Installed unsigned Comic Sol $Version at $InstallRoot"
    Write-Output "User projects are outside this directory."
} catch {
    Restore-Install
    throw
} finally {
    if (Test-Path -LiteralPath $Temp) { Remove-Item -LiteralPath $Temp -Recurse -Force }
}
