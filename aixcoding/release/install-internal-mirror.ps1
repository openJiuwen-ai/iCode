# Install or upgrade AIxCoding from the internal release mirror (Windows x64).
# Windows on Arm uses the x64 package through emulation; native Arm is not published.
& {
    param(
        [ValidateSet('prod', 'test')]
        [string]$Channel = $(if ($env:AIXCODING_CHANNEL) { $env:AIXCODING_CHANNEL } elseif ($env:ICODE_CHANNEL) { $env:ICODE_CHANNEL } else { 'prod' }),
        [string]$Version = '',
        [string]$BaseUrl = $(if ($env:AIXCODING_BASE_URL) { $env:AIXCODING_BASE_URL } elseif ($env:ICODE_BASE_URL) { $env:ICODE_BASE_URL } else { 'http://82.187.34.98/aixcoding/file/aixcoding' })
    )

    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    if ($PSBoundParameters.ContainsKey('Version') -and [string]::IsNullOrWhiteSpace($Version)) {
        throw '-Version needs a non-empty value.'
    }
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'This installer runs only on Windows.'
    }
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw 'Only 64-bit Windows is supported by this installer.'
    }
    if (-not $env:LOCALAPPDATA) {
        throw 'LOCALAPPDATA is required to locate the AIxCoding installation directory.'
    }

    $root = $BaseUrl.TrimEnd('/')
    $binDir = Join-Path $env:LOCALAPPDATA 'chrys\bin'
    $ownBinary = Join-Path $binDir 'chrys.exe'
    $others = @(Get-Command aixcoding-cli -CommandType Application -All -ErrorAction SilentlyContinue |
        Where-Object { [IO.Path]::GetDirectoryName($_.Source) -ne $binDir })
    $externalCommand = if ($others) { $others[0].Source } else { '' }
    if ($others -and (Test-Path -LiteralPath $ownBinary -PathType Leaf)) {
        Write-Host "Warning: $externalCommand does not start the AIxCoding offline install; continue to start that install with chrys."
    } elseif ($others) {
        throw ("$externalCommand is not an AIxCoding offline install, so this script would not replace it. " +
            'If it is AIxCoding installed with uv or pipx, upgrade it with that tool (for example, ' +
            'uv tool upgrade iCode-TUI), or uninstall it first and run this script again.')
    }

    if (-not $Version) {
        $channelUrl = "$root/channels/cli/${Channel}.txt"
        Write-Host "Reading $Channel channel from $channelUrl"
        try {
            $Version = [string](Invoke-RestMethod -Uri $channelUrl -UseBasicParsing)
        } catch {
            throw ("Could not read $channelUrl. Check the network; if the mirror moved, " +
                'rerun with -BaseUrl <url> or set AIXCODING_BASE_URL=<url>. ' + $_.Exception.Message)
        }
    }
    $Version = $Version.Trim() -replace '^[vV]', ''
    if ($Version -notmatch '^\d+\.\d+\.\d+([._-][A-Za-z0-9._-]+)?$') {
        throw "Invalid release version: $Version"
    }

    $archive = "aixcoding-cli-windows-x86_64-v$Version-offline.zip"
    $workDir = Join-Path ([IO.Path]::GetTempPath()) ("aixcoding-install-" + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($workDir) | Out-Null
    try {
        $archivePath = Join-Path $workDir $archive
        $extractDir = Join-Path $workDir 'extract'
        $archiveUrl = "$root/releases/cli/$Version/$archive"
        Write-Host "Downloading $archiveUrl"
        try {
            Invoke-WebRequest -Uri $archiveUrl -OutFile $archivePath -UseBasicParsing
        } catch {
            throw ("Could not download $archiveUrl. Check the network; if the mirror moved, " +
                'rerun with -BaseUrl <url> or set AIXCODING_BASE_URL=<url>. ' + $_.Exception.Message)
        }
        function Invoke-PackageCommand([string]$Exe, [string]$Command) {
            $process = Start-Process -FilePath $Exe -ArgumentList $Command -NoNewWindow -PassThru
            $null = $process.Handle
            $process.WaitForExit()
            return $process.ExitCode
        }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($archivePath, $extractDir)
        $binary = Join-Path $extractDir 'aixcoding-cli.exe'
        if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) {
            throw "Archive does not contain aixcoding-cli.exe at its root: $archive"
        }
        $status = Invoke-PackageCommand $binary '--version'
        if ($status -ne 0) { throw "Binary self-check failed (exit $status)." }

        $status = Invoke-PackageCommand $binary 'install'
        if ($status -ne 0) { throw "aixcoding-cli install failed (exit code $status)." }

        $commandPath = if ($externalCommand) { $ownBinary } else { Join-Path $binDir 'aixcoding-cli.exe' }
        if (-not (Test-Path -LiteralPath $commandPath -PathType Leaf)) {
            throw "Installation finished without creating $commandPath"
        }
        $status = Invoke-PackageCommand $commandPath '--version'
        if ($status -ne 0) { throw "Installed command validation failed (exit ${status}): $commandPath" }
        if ($externalCommand) {
            Write-Host 'Installation complete. The external aixcoding-cli was preserved; run chrys.'
        } else {
            Write-Host 'Installation complete. Open a new PowerShell window and run aixcoding-cli.'
        }
    }
    finally {
        Remove-Item -LiteralPath $workDir -Recurse -Force -ErrorAction SilentlyContinue
    }
} @args
