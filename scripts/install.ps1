# Install or upgrade AIxCoding from its offline package (Windows).
#
#   irm https://raw.githubusercontent.com/openJiuwen-ai/iCode/main/scripts/install.ps1 | iex
#   irm https://raw.gitcode.com/openJiuwen/iCode/raw/main/scripts/install.ps1 | iex
#
# To choose what it installs, set these first (or pass -Version / -Source when running the file):
#   $env:ICODE_VERSION = "0.29.1"   install that version instead of the latest
#   $env:ICODE_SOURCE = "gitcode"   download only from github or gitcode
#
# It downloads the package for this machine from GitHub Releases, or from GitCode when GitHub
# fails, checks it against the release's SHA256SUMS.txt and runs `aixcoding-cli install`, which puts
# `chrys.exe` and `aixcoding-cli.exe` in %LOCALAPPDATA%\chrys\bin and adds that folder to the user PATH.
# Running it again upgrades.
#
# Everything runs in one script block, so `irm | iex` leaves nothing behind in the session, and
# a failure throws instead of exiting, which would close the window. It uses .NET for hashing and
# unzipping: Windows PowerShell's Get-FileHash and Expand-Archive come from script modules, which
# the default execution policy does not load.

& {
    param(
        [string]$Version = $env:ICODE_VERSION,
        [string]$Source = $env:ICODE_SOURCE
    )

    # An explicitly empty option is a command-line mistake, not "install the latest".
    if ($PSBoundParameters.ContainsKey('Version') -and [string]::IsNullOrWhiteSpace($Version)) {
        throw '-Version needs a value.'
    }
    if ($PSBoundParameters.ContainsKey('Source') -and [string]::IsNullOrWhiteSpace($Source)) {
        throw '-Source needs a value.'
    }

    $ErrorActionPreference = 'Stop'
    # Windows PowerShell draws its progress bar so slowly that it throttles the download.
    $ProgressPreference = 'SilentlyContinue'
    # Windows PowerShell 5.1 may not offer TLS 1.2, which GitHub and GitCode require.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $githubRepo = 'openJiuwen-ai/iCode'
    $gitcodeRepo = 'openJiuwen/iCode'
    $hostNames = @{ github = 'GitHub'; gitcode = 'GitCode' }

    function Test-Version([string]$Text) {
        $Text -match '^\d+\.\d+\.\d+$'
    }

    function Get-ReleaseUrl([string]$HostId, [string]$Tag, [string]$File) {
        if ($HostId -eq 'github') {
            "https://github.com/$githubRepo/releases/download/$Tag/$File"
        } else {
            "https://gitcode.com/$gitcodeRepo/releases/download/$Tag/$File"
        }
    }

    # GitCode has no releases/latest link, so ask its API, whose anonymous quota is shared by
    # everyone and often runs out; the version on main is the next best guess, and the checksum
    # download that follows proves whether that release exists.
    function Get-GitCodeLatestTag {
        try {
            $release = Invoke-RestMethod -Uri "https://api.gitcode.com/api/v5/repos/$gitcodeRepo/releases/latest" -TimeoutSec 30 -UseBasicParsing
            if ($release.tag_name) { return [string]$release.tag_name }
        } catch { }
        try {
            $pyproject = Invoke-RestMethod -Uri "https://raw.gitcode.com/$gitcodeRepo/raw/main/pyproject.toml" -TimeoutSec 30 -UseBasicParsing
            if ("$pyproject" -match '(?m)^version\s*=\s*"([^"]+)"') { return "v$($Matches[1])" }
        } catch { }
        return $null
    }

    # Runs on this console, so the binary can ask the user to quit a running AIxCoding first.
    function Invoke-Install([string]$Exe) {
        $process = Start-Process -FilePath $Exe -ArgumentList 'install' -NoNewWindow -PassThru
        $null = $process.Handle  # Without the handle, ExitCode reads empty after the wait.
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) { throw "aixcoding-cli install failed (exit code $($process.ExitCode))." }
    }

    # Returns whether AIxCoding is now installed; throws when another host would not help.
    function Install-From([string]$HostId) {
        $name = $hostNames[$HostId]
        if ($Version) {
            $sumsUrl = Get-ReleaseUrl $HostId "v$Version" 'SHA256SUMS.txt'
        } elseif ($HostId -eq 'github') {
            # Redirects to the newest release without the API and its hourly quota.
            $sumsUrl = "https://github.com/$githubRepo/releases/latest/download/SHA256SUMS.txt"
        } else {
            $tag = Get-GitCodeLatestTag
            if (-not $tag) {
                Write-Host "Could not find the latest AIxCoding version on $name."
                return $false
            }
            $sumsUrl = Get-ReleaseUrl $HostId $tag 'SHA256SUMS.txt'
        }
        $sumsFile = Join-Path $tmp 'SHA256SUMS.txt'
        try {
            Invoke-WebRequest -Uri $sumsUrl -OutFile $sumsFile -TimeoutSec 30 -UseBasicParsing
        } catch {
            Write-Host "Could not download $sumsUrl`: $($_.Exception.Message)"
            return $false
        }

        # The name of this machine's package, which also carries its version.
        # AIxCoding branding: Windows install command contract
        $prefix = "aixcoding-cli-windows-$arch-v"
        $suffix = '-offline.zip'
        $package = $null
        $expected = $null
        foreach ($line in Get-Content -LiteralPath $sumsFile) {
            $fields = $line.Trim() -split '\s+', 2
            if ($fields.Count -eq 2 -and $fields[1].TrimStart('*') -like "$prefix*$suffix") {
                $package = $fields[1].TrimStart('*')
                $expected = $fields[0]
                break
            }
        }
        $target = if ($package) { $package.Substring($prefix.Length, $package.Length - $prefix.Length - $suffix.Length) } else { '' }
        if (-not (Test-Version $target)) {
            Write-Host "That release on $name has no package for Windows ($arch)."
            return $false
        }

        $installedExe = Join-Path $binDir 'chrys.exe'
        $installed = ''
        if (Test-Path -LiteralPath $installedExe) {
            # Windows PowerShell 5.1 turns a native command's stderr into errors, which 'Stop' would throw.
            $installed = try {
                & { $ErrorActionPreference = 'Continue'; "$(& $installedExe --version 2>$null | Select-Object -First 1)".Trim() }
            } catch { '' }
        }
        # Without its `aixcoding-cli` alias the install is incomplete, and installing again restores it.
        if ($installed -eq $target -and (Test-Path -LiteralPath (Join-Path $binDir 'aixcoding-cli.exe'))) {
            Write-Host "AIxCoding $target is already installed."
            return $true
        }
        # A host that lags behind, such as GitCode right after a release, must not downgrade.
        if (-not $Version -and (Test-Version $installed) -and ([version]$installed -gt [version]$target)) {
            Write-Host "AIxCoding $installed is already installed, which is newer than the latest on $name ($target)."
            # The installed binary puts back an `aixcoding-cli` alias that went missing.
            if (-not (Test-Path -LiteralPath (Join-Path $binDir 'aixcoding-cli.exe'))) { Invoke-Install $installedExe }
            return $true
        }

        Write-Host "Downloading AIxCoding $target for Windows ($arch) from $name..."
        $zip = Join-Path $tmp $package
        # pwsh 7.4 and later can give up on a download that stops arriving, so the next host gets
        # a turn; Windows PowerShell gives up after 5 minutes without data.
        $stall = @{}
        if ((Get-Command Invoke-WebRequest).Parameters.ContainsKey('OperationTimeoutSeconds')) {
            $stall = @{ ConnectionTimeoutSeconds = 30; OperationTimeoutSeconds = 60 }
        }
        try {
            Invoke-WebRequest -Uri (Get-ReleaseUrl $HostId "v$target" $package) -OutFile $zip -UseBasicParsing @stall
        } catch {
            Write-Host "Could not download $package`: $($_.Exception.Message)"
            return $false
        }
        $sha256 = [Security.Cryptography.SHA256]::Create()
        $stream = [IO.File]::OpenRead($zip)
        try {
            $actual = -join ($sha256.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') })
        } finally {
            $stream.Dispose()
            $sha256.Dispose()
        }
        if ($actual -ne $expected) {
            Write-Host "The download of $package is damaged (its SHA-256 does not match SHA256SUMS.txt)."
            return $false
        }

        # From here on a failure is not the download's, so another host would not help.
        $packageDir = Join-Path $tmp 'package'
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($zip, $packageDir)
        # The binary unpacks its runtime on first run, then copies itself into place.
        Invoke-Install (Join-Path $packageDir 'aixcoding-cli.exe')
        return $true
    }

    if (-not $env:LOCALAPPDATA) {
        throw 'This script installs AIxCoding on Windows; on macOS and Linux, use install.sh.'
    }
    $binDir = Join-Path $env:LOCALAPPDATA 'chrys\bin'

    $Version = "$Version".TrimStart('v')
    if ($Version -and -not (Test-Version $Version)) {
        throw "-Version takes a version number such as 0.29.1, not '$Version'."
    }
    switch ("$Source") {
        '' { $sources = @('github', 'gitcode') }
        'github' { $sources = @('github') }
        'gitcode' { $sources = @('gitcode') }
        default { throw "-Source must be github or gitcode, not '$Source'." }
    }

    $osArch = try { [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() } catch { $env:PROCESSOR_ARCHITECTURE }
    switch ($osArch) {
        { $_ -in 'X64', 'AMD64' } { $arch = 'x86_64' }
        { $_ -in 'Arm64', 'ARM64' } { $arch = 'aarch64' }
        default { throw "There is no AIxCoding package for $osArch processors." }
    }

    # AIxCoding branding: Windows install command contract
    # The installer's folder goes first on the user PATH, so another `aixcoding-cli` would keep starting
    # whatever it starts now only in terminals that put it earlier, and confuse later upgrades.
    $others = @(Get-Command aixcoding-cli -CommandType Application -All -ErrorAction SilentlyContinue |
        Where-Object { [IO.Path]::GetDirectoryName($_.Source) -ne $binDir })
    if ($others -and (Test-Path -LiteralPath (Join-Path $binDir 'chrys.exe') -PathType Leaf)) {
        # An offline install that already lives with this `aixcoding-cli` is started with `chrys`;
        # upgrading it changes nothing about that.
        Write-Host "Warning: $($others[0].Source) does not start the AIxCoding offline install; start that with chrys."
    } elseif ($others) {
        throw ("$($others[0].Source) is not an AIxCoding offline install, so this script would not replace it. " +
            'If it is AIxCoding installed with uv or pipx, upgrade it with that tool (for example, ' +
            'uv tool upgrade iCode-TUI), or uninstall it first and run this script again.')
    }

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("icode-install-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        foreach ($hostId in $sources) {
            if (Install-From $hostId) { return }
            Write-Host "Could not install from $($hostNames[$hostId])."
        }
        if ($Version) {
            throw ("Could not install AIxCoding $Version. Check that this version is listed at " +
                "https://github.com/$githubRepo/releases and that you are online.")
        }
        throw 'The download failed. Check your network connection, or try again later.'
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
} @args
