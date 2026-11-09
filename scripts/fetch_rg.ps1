# Download the ripgrep (rg) binary for the current platform and place it in
# src\chrys\foundation\vendor\ripgrep\ so it gets bundled into the wheel.
#
# Usage:
#   .\scripts\fetch_rg.ps1                                       # download from GitHub
#   .\scripts\fetch_rg.ps1 -Version 14.1.1                       # specific version
#   .\scripts\fetch_rg.ps1 -Archive C:\path\to\ripgrep-*.zip     # use local archive
#
# Environment variables (override parameters):
#   RG_VERSION   - ripgrep version (default: 15.2.0)
#   RG_BASE_URL  - base URL for downloads (default: GitHub releases URL)
#   RG_ARCHIVE   - path to pre-downloaded archive (skips download entirely)

param(
    [string]$Version,
    [string]$Archive
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$RgVersion = if ($Version) { $Version } elseif ($env:RG_VERSION) { $env:RG_VERSION } else { "15.2.0" }
$RgBaseUrl = if ($env:RG_BASE_URL) { $env:RG_BASE_URL } else { "https://github.com/BurntSushi/ripgrep/releases/download" }
$RgArchive = if ($Archive) { $Archive } elseif ($env:RG_ARCHIVE) { $env:RG_ARCHIVE } else { "" }

# ── Detect platform ──────────────────────────────────────────────────
$Arch = $env:PROCESSOR_ARCHITECTURE.ToLower()

switch ($Arch) {
    "amd64" { $Triple = "x86_64-pc-windows-msvc" }
    "arm64" { $Triple = "aarch64-pc-windows-msvc" }
    default { throw "Unsupported architecture: $Arch" }
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DestDir = Join-Path (Split-Path -Parent $ScriptDir) "src\chrys\foundation\vendor\ripgrep"
if (-not (Test-Path $DestDir)) { New-Item -ItemType Directory -Path $DestDir -Force | Out-Null }

# ── Obtain archive ───────────────────────────────────────────────────
if ($RgArchive) {
    Write-Host "==> Using local archive: $RgArchive"
    $ArchivePath = $RgArchive
} else {
    $ArchiveName = "ripgrep-$RgVersion-$Triple.zip"
    $Url = "$RgBaseUrl/$RgVersion/$ArchiveName"
    Write-Host "==> Downloading rg $RgVersion for $Triple..."
    Write-Host "    $Url"
    $TmpDir = Join-Path ([System.IO.Path]::GetTempPath()) "fetch-rg-$(Get-Random)"
    New-Item -ItemType Directory -Path $TmpDir -Force | Out-Null
    $ArchivePath = Join-Path $TmpDir $ArchiveName
    # The release CDN answers 5xx now and then, and an outage can hold for minutes: keep asking with
    # a capped exponential backoff instead of giving up after seconds. Spelled as a loop: Windows
    # PowerShell 5.1 has no -MaximumRetryCount.
    $Attempts = 10
    for ($Attempt = 1; ; $Attempt++) {
        try {
            Invoke-WebRequest -Uri $Url -OutFile $ArchivePath
            break
        } catch {
            if ($Attempt -ge $Attempts) { throw }
            Write-Host "    attempt $Attempt failed: $($_.Exception.Message)"
            Start-Sleep -Seconds ([math]::Min([math]::Pow(2, $Attempt - 1), 30))
        }
    }
}

# ── Extract rg.exe ───────────────────────────────────────────────────
Write-Host "==> Extracting rg.exe to $DestDir..."
$ExtractDir = Join-Path ([System.IO.Path]::GetTempPath()) "fetch-rg-extract-$(Get-Random)"
Expand-Archive -Path $ArchivePath -DestinationPath $ExtractDir -Force
$RgExe = Get-ChildItem -Path $ExtractDir -Recurse -Filter "rg.exe" | Select-Object -First 1
if (-not $RgExe) { throw "rg.exe not found in archive" }
Copy-Item $RgExe.FullName (Join-Path $DestDir "rg.exe") -Force

# Cleanup
if ($TmpDir -and (Test-Path $TmpDir)) { Remove-Item -Recurse -Force $TmpDir }
if (Test-Path $ExtractDir) { Remove-Item -Recurse -Force $ExtractDir }

$SizeKB = [math]::Round((Get-Item (Join-Path $DestDir "rg.exe")).Length / 1KB)
Write-Host "==> Done: $DestDir\rg.exe (${SizeKB}KB)"
