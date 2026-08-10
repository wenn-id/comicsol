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
    $Pointer = Join-Path $InstallRoot "active-version"
    if ($HadPointer) { Set-Content -NoNewline -LiteralPath $Pointer -Value "$PreviousVersion`n" -Encoding utf8 }
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
    $VersionLine = ($VersionOutput | Select-Object -First 1).ToString().Trim()
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
    New-Item -ItemType Directory -Force -Path $Versions, $InstallRoot | Out-Null
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
    foreach ($Path in @($StableBackup, $TargetBackup)) { if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force } }
    $Committed = $true
    Write-Output "Installed unsigned Comic Sol $Version at $InstallRoot"
    Write-Output "User projects are outside this directory."
} catch {
    throw
} finally {
    Restore-Install
    if (Test-Path -LiteralPath $Temp) { Remove-Item -LiteralPath $Temp -Recurse -Force }
}
