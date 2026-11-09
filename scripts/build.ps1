# Build a self-contained chrys binary for Windows using PyApp.
#
# Prerequisites: Rust toolchain (cargo), uv, Python 3.14+
#
# Usage:
#   .\scripts\build.ps1              # default: uses pip (no GitHub needed at runtime)
#   .\scripts\build.ps1 -UseUv       # uses uv (faster, but downloads uv from GitHub on first run)
#   .\scripts\build.ps1 -Offline     # bundles every dependency; first run needs no network
#
# Environment variables:
#   PYAPP_VERSION  - PyApp release to use (default: 0.29.0)
#   PYAPP_SOURCE   - path to local PyApp source.tar.gz (skips GitHub download)
#   PYTHON_DIST    - path to local python-build-standalone tarball (skips GitHub download)
#   OFFLINE_DIST   - path to a prebuilt offline distribution (skips building one)
#
# By default the binary embeds Python plus the iCode wheel, and the chosen
# installer fetches the dependencies from PyPI on first run.  With -Offline the
# embedded distribution already contains chrys and every dependency, so the
# first run only unpacks it - no installer, no PyPI, no network.
#
# On Windows on Arm, PyPI has no Arm64 wheels for cryptography and grpcio, so
# they are built from source: use -Offline (the other flavors would build them
# on the user's machine), with Visual Studio's ARM64 C++ tools installed and,
# as CD does, OPENSSL_DIR naming a static OpenSSL (vcpkg's
# openssl:arm64-windows-static-md), OPENSSL_STATIC=1 and UV_CACHE_DIR naming a
# short folder such as C:\uv: uv builds inside its cache, and grpcio's deepest
# object file then exceeds the 260 characters cl.exe can write.
#
# Output: dist\aixcoding-cli.exe

param(
    [switch]$UseUv,
    [switch]$Offline
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if ($UseUv -and $Offline) {
    throw "-UseUv and -Offline are mutually exclusive; an offline build runs no installer"
}

$PyAppVersion = if ($env:PYAPP_VERSION) { $env:PYAPP_VERSION } else { "0.29.0" }
# The binary ships the CPython version .python-version pins (the one CI tests
# on), built by this python-build-standalone release; bump it together with
# PYTHON_BUILD_STANDALONE_RELEASE in .github/workflows/cd.yml.
$PythonBuildStandaloneRelease = "20261026"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$CPythonVersion = (Get-Content (Join-Path $ProjectRoot ".python-version") -Raw).Trim()
if ($CPythonVersion -notmatch '^3\.\d+\.\d+$') {
    throw ".python-version must pin a full CPython version, got '$CPythonVersion'"
}
$PythonVersion = $CPythonVersion -replace '\.\d+$', ''
# The same machine detection as fetch_rg.ps1, so Python and the bundled ripgrep match.
switch ($env:PROCESSOR_ARCHITECTURE.ToLower()) {
    "amd64" { $Triple = "x86_64-pc-windows-msvc" }
    "arm64" { $Triple = "aarch64-pc-windows-msvc" }
    default { throw "Unsupported architecture: $env:PROCESSOR_ARCHITECTURE" }
}
$DistUrl = "https://github.com/astral-sh/python-build-standalone/releases/download/$PythonBuildStandaloneRelease/cpython-$CPythonVersion+$PythonBuildStandaloneRelease-$Triple-install_only_stripped.tar.gz"

# Env-var paths may be relative to the caller's directory; resolve them before
# any Set-Location so they survive the working-directory changes below.
foreach ($name in "PYAPP_SOURCE", "PYTHON_DIST", "OFFLINE_DIST") {
    $value = [Environment]::GetEnvironmentVariable($name)
    if ($value -and -not [System.IO.Path]::IsPathRooted($value)) {
        Set-Item "env:$name" (Join-Path (Get-Location).Path $value)
    }
}

Set-Location $ProjectRoot

# Extract version from pyproject.toml. --no-project: syncing the project here
# would build its source-only dependencies without the offline build's pins.
$Version = uv run --no-project python -c "import tomllib, pathlib; print(tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8'))['project']['version'])"
if ($LASTEXITCODE -ne 0) { throw "Failed to extract version" }

Write-Host "==> Building chrys v$Version (PyApp v$PyAppVersion)"

# ── Fetch ripgrep binary ──────────────────────────────────────────────
Write-Host "==> Fetching rg binary for bundling..."
& "$ScriptDir\fetch_rg.ps1"

# ── Build wheel ───────────────────────────────────────────────────────
Write-Host "==> Building wheel..."
uv build --wheel --quiet
if ($LASTEXITCODE -ne 0) { throw "uv build failed" }

# ── Prepare PyApp source ─────────────────────────────────────────────
$BuildDir = Join-Path ([System.IO.Path]::GetTempPath()) "pyapp-build-$(Get-Random)"
New-Item -ItemType Directory -Path $BuildDir -Force | Out-Null

try {
    if ($env:PYAPP_SOURCE) {
        Write-Host "==> Using local PyApp source: $env:PYAPP_SOURCE"
        tar xzf $env:PYAPP_SOURCE -C $BuildDir
    } else {
        Write-Host "==> Downloading PyApp source..."
        $SourceArchive = Join-Path $BuildDir "source.tar.gz"
        $PyAppUrl = "https://github.com/ofek/pyapp/releases/download/v$PyAppVersion/source.tar.gz"
        $downloaded = $false
        if (Get-Command gh -ErrorAction SilentlyContinue) {
            try {
                gh release download "v$PyAppVersion" --repo ofek/pyapp --pattern source.tar.gz --dir $BuildDir 2>$null
                $downloaded = $true
            } catch { }
        }
        if (-not $downloaded) {
            Invoke-WebRequest -Uri $PyAppUrl -OutFile $SourceArchive
        }
        tar xzf (Join-Path $BuildDir "source.tar.gz") -C $BuildDir
    }

    $PyAppDir = Join-Path $BuildDir "pyapp-v$PyAppVersion"
    $WheelSource = Join-Path "dist" "icode_tui-$Version-py3-none-any.whl"
    $WheelFile = Get-Item $WheelSource -ErrorAction SilentlyContinue
    if (-not $WheelFile) {
        $WheelFile = Get-ChildItem "dist\icode_tui-*.whl" |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
    }
    if (-not $WheelFile) {
        throw "No iCode wheel found in dist"
    }
    Copy-Item $WheelFile.FullName $PyAppDir
    Set-Location $PyAppDir

    $Wheel = $WheelFile.Name

    # ── Configure PyApp ───────────────────────────────────────────────
    # Every PYAPP_* variable is set explicitly (to $null when unused) because
    # they persist for the whole PowerShell session: leftovers from an earlier
    # run with different flags would otherwise leak into this build.
    $env:PYAPP_PROJECT_NAME = "chrys"
    $env:PYAPP_PROJECT_VERSION = $Version
    $env:PYAPP_PYTHON_VERSION = $PythonVersion
    $env:PYAPP_EXEC_SPEC = "chrys.app.cli.app:pyapp_main"
    # iCode has its own upgrade and uninstall paths, so PyApp's `self` commands
    # (update, remove, restore, ...) stay hidden: `none` names them with a random
    # string nobody types.
    $env:PYAPP_SELF_COMMAND = "none"
    $env:PYAPP_PASS_LOCATION = "true"
    # Set below for installer builds only; a stale session value alongside
    # PYAPP_DISTRIBUTION_PATH would make PyApp's build.rs panic.
    $env:PYAPP_DISTRIBUTION_SOURCE = $null

    if ($Offline) {
        Write-Host "==> Installer: none (chrys and all dependencies are bundled)"

        $OfflineArchive = "chrys-offline-dist.tar.gz"
        if ($env:OFFLINE_DIST) {
            Write-Host "==> Using prebuilt offline distribution: $env:OFFLINE_DIST"
            Copy-Item $env:OFFLINE_DIST (Join-Path $PyAppDir $OfflineArchive)
        } elseif ($env:PYTHON_DIST) {
            Write-Host "==> Using local Python distribution: $env:PYTHON_DIST"
            & "$ScriptDir\build_offline_dist.ps1" `
                -DistArchive $env:PYTHON_DIST `
                -Wheel (Join-Path $PyAppDir $Wheel) `
                -Output (Join-Path $PyAppDir $OfflineArchive)
        } else {
            & "$ScriptDir\build_offline_dist.ps1" `
                -DistUrl $DistUrl `
                -Wheel (Join-Path $PyAppDir $Wheel) `
                -Output (Join-Path $PyAppDir $OfflineArchive)
        }

        # PYAPP_DISTRIBUTION_PATH embeds the archive as-is (and must not be
        # combined with PYAPP_DISTRIBUTION_SOURCE - build.rs panics).  With
        # PYAPP_SKIP_INSTALL, first run reduces to unpacking it.
        $env:PYAPP_DISTRIBUTION_PATH = $OfflineArchive
        $env:PYAPP_DISTRIBUTION_PATH_PREFIX = "python"
        $env:PYAPP_SKIP_INSTALL = "true"
        # FULL_ISOLATION runs the unpacked interpreter directly instead of
        # building a virtualenv around it - the only mode where the bundled
        # site-packages is the one actually used.
        $env:PYAPP_FULL_ISOLATION = "true"
        # Relative to PATH_PREFIX, which build.rs prepends for us.
        $env:PYAPP_DISTRIBUTION_PYTHON_PATH = "python.exe"
        $env:PYAPP_DISTRIBUTION_SITE_PACKAGES_PATH = "Lib\site-packages"

        $env:PYAPP_PROJECT_PATH = $null
        $env:PYAPP_PIP_ALLOW_CONFIG = $null
        $env:PYAPP_DISTRIBUTION_EMBED = $null
        $env:PYAPP_UV_ENABLED = $null
        $env:PYAPP_DISTRIBUTION_PIP_AVAILABLE = $null
    } else {
        $env:PYAPP_PROJECT_PATH = $Wheel
        $env:PYAPP_PIP_ALLOW_CONFIG = "true"
        $env:PYAPP_DISTRIBUTION_EMBED = "true"
        $env:PYAPP_SKIP_INSTALL = $null
        $env:PYAPP_DISTRIBUTION_PATH_PREFIX = $null
        $env:PYAPP_DISTRIBUTION_SITE_PACKAGES_PATH = $null

        if ($UseUv) {
            Write-Host "==> Installer: uv (fast, downloads uv from GitHub on first run)"
            $env:PYAPP_UV_ENABLED = "true"
            $env:PYAPP_FULL_ISOLATION = $null
            $env:PYAPP_DISTRIBUTION_PIP_AVAILABLE = $null
        } else {
            Write-Host "==> Installer: pip (bundled, no extra downloads)"
            $env:PYAPP_FULL_ISOLATION = "true"
            $env:PYAPP_DISTRIBUTION_PIP_AVAILABLE = "true"
            $env:PYAPP_UV_ENABLED = $null
        }

        if ($env:PYTHON_DIST) {
            Write-Host "==> Using local Python distribution: $env:PYTHON_DIST"
            Copy-Item $env:PYTHON_DIST .
            $env:PYAPP_DISTRIBUTION_PATH = Split-Path -Leaf $env:PYTHON_DIST
            $env:PYAPP_DISTRIBUTION_PYTHON_PATH = "python/python.exe"
        } else {
            # The pinned distribution, rather than the one PyApp's build.rs names.
            $env:PYAPP_DISTRIBUTION_SOURCE = $DistUrl
            $env:PYAPP_DISTRIBUTION_PATH = $null
            $env:PYAPP_DISTRIBUTION_PYTHON_PATH = $null
        }
    }

    # ── Patch PyApp ───────────────────────────────────────────────────
    # 1. Tolerate non-UTF-8 subprocess output (fixes non-English Windows)
    Write-Host "==> Patching PyApp for UTF-8 compatibility..."
    $ProcessRs = Join-Path $PyAppDir "src\process.rs"
    (Get-Content $ProcessRs -Raw) `
        -replace 'let mut output = String::new\(\);', 'let mut raw = Vec::new();' `
        -replace 'reader\.read_to_string\(&mut output\)\?;', 'reader.read_to_end(&mut raw)?;' `
        -replace 'Ok\(\(result\?, output\)\)', "let output = String::from_utf8_lossy(&raw).into_owned();`n    Ok((result?, output))" |
        Set-Content $ProcessRs -NoNewline

    # 2. Increase download timeout (default 30s is too short for large Python distributions)
    Write-Host "==> Patching PyApp download timeout (300s)..."
    $BuildRs = Join-Path $PyAppDir "build.rs"
    (Get-Content $BuildRs -Raw) `
        -replace 'reqwest::blocking::get\(([^)]+)\)', 'reqwest::blocking::Client::builder().timeout(std::time::Duration::from_secs(300)).build().unwrap().get($1).send()' |
        Set-Content $BuildRs -NoNewline

    # 3. On Windows, PyApp launches Chrys by spawning the unpacked Python
    # executable.  Run through a sibling alias so broad `taskkill /IM python.exe`
    # cleanup scripts do not kill the live Chrys process.
    Write-Host "==> Patching PyApp to run Chrys through renamed Python..."
    $AppRs = Join-Path $PyAppDir "src\app.rs"
    $AppSource = Get-Content $AppRs -Raw
    $OldPythonPath = @'
pub fn python_path() -> PathBuf {
    install_dir().join(installation_python_path())
}

#[cfg(windows)]
pub fn pythonw_path() -> PathBuf {
    install_dir().join(installation_pythonw_path())
}
'@
    $NewPythonPath = @'
pub fn python_path() -> PathBuf {
    install_dir().join(installation_python_path())
}

#[cfg(windows)]
pub fn pythonw_path() -> PathBuf {
    install_dir().join(installation_pythonw_path())
}

#[cfg(windows)]
pub const CHRYS_RUNTIME_EXE: &str = "chrys-runtime.exe";

#[cfg(any(target_os = "macos", target_os = "linux"))]
pub const CHRYS_RUNTIME_EXE: &str = "chrys-runtime";

#[cfg(windows)]
pub const CHRYS_RUNTIMEW_EXE: &str = "chrys-runtimew.exe";

#[cfg(any(windows, target_os = "macos", target_os = "linux"))]
fn renamed_python_path(original: PathBuf, alias: &str) -> PathBuf {
    if let Some(path) = original.parent().map(|parent| parent.join(alias)) {
        if path.is_file() {
            return path;
        }
        if cfg!(debug_assertions) {
            eprintln!(
                "Chrys runtime alias {} is missing; falling back to {}",
                path.display(),
                original.display()
            );
        }
    }
    original
}

#[cfg(any(windows, target_os = "macos", target_os = "linux"))]
pub fn runtime_python_path() -> PathBuf {
    renamed_python_path(python_path(), CHRYS_RUNTIME_EXE)
}

#[cfg(not(any(windows, target_os = "macos", target_os = "linux")))]
pub fn runtime_python_path() -> PathBuf {
    python_path()
}

#[cfg(windows)]
pub fn runtime_pythonw_path() -> PathBuf {
    renamed_python_path(pythonw_path(), CHRYS_RUNTIMEW_EXE)
}
'@
    $AppSource = $AppSource.Replace($OldPythonPath, $NewPythonPath)
    Set-Content $AppRs $AppSource -NoNewline

    $DistributionRs = Join-Path $PyAppDir "src\distribution.rs"
    $DistributionSource = Get-Content $DistributionRs -Raw
    $OldRunProject = @'
pub fn run_project() -> Result<()> {
    let mut command = python_command(&app::python_path());

    #[cfg(windows)]
    {
        if app::is_gui() {
            command = python_command(&app::pythonw_path());
        }
    }
'@
    $NewRunProject = @'
#[cfg(any(windows, target_os = "macos", target_os = "linux"))]
fn link_or_copy_file(source: &Path, target: &Path) -> Result<()> {
    if target.is_file() {
        return Ok(());
    }
    fs::hard_link(source, target)
        .or_else(|_| fs::copy(source, target).map(|_| ()))
        .with_context(|| {
            format!(
                "unable to create Chrys runtime alias {} from {}",
                &target.display(),
                &source.display()
            )
        })
}

#[cfg(windows)]
fn source_pth_path(source: &Path) -> Option<std::path::PathBuf> {
    let executable_pth = source.with_extension("_pth");
    if executable_pth.is_file() {
        return Some(executable_pth);
    }

    let python_pth = source.parent()?.join("python._pth");
    python_pth.is_file().then_some(python_pth)
}

#[cfg(any(windows, target_os = "macos", target_os = "linux"))]
fn ensure_runtime_alias(source: &Path, alias_name: &str) -> Result<()> {
    if source.file_name() == Some(OsStr::new(alias_name)) {
        return Ok(());
    }
    let alias = source
        .parent()
        .with_context(|| format!("Chrys runtime alias source has no parent: {}", source.display()))?
        .join(alias_name);
    link_or_copy_file(source, &alias)?;

    #[cfg(windows)]
    {
        if let Some(source_pth) = source_pth_path(source) {
            link_or_copy_file(&source_pth, &alias.with_extension("_pth"))?;
        }
    }

    Ok(())
}

#[cfg(any(windows, target_os = "macos", target_os = "linux"))]
fn ensure_runtime_aliases() -> Result<()> {
    ensure_runtime_alias(&app::python_path(), app::CHRYS_RUNTIME_EXE)?;
    #[cfg(windows)]
    {
        let pythonw_path = app::pythonw_path();
        if pythonw_path.is_file() {
            ensure_runtime_alias(&pythonw_path, app::CHRYS_RUNTIMEW_EXE)?;
        }
    }
    Ok(())
}

pub fn run_project() -> Result<()> {
    let mut command = python_command(&app::runtime_python_path());

    #[cfg(windows)]
    {
        if app::is_gui() {
            command = python_command(&app::runtime_pythonw_path());
        }
    }
'@
    $DistributionSource = $DistributionSource.Replace($OldRunProject, $NewRunProject)
    $OldEnsureReady = @'
    if !app::install_dir().is_dir() {
        materialize()?;

        if !app::skip_install() {
            install_project()?;
        }
    }

    FileExt::unlock(&lock_file)
'@
    $NewEnsureReady = @'
    if !app::install_dir().is_dir() {
        materialize()?;

        if !app::skip_install() {
            install_project()?;
        }
    }

    #[cfg(any(windows, target_os = "macos", target_os = "linux"))]
    ensure_runtime_aliases()?;

    FileExt::unlock(&lock_file)
'@
    $DistributionSource = $DistributionSource.Replace($OldEnsureReady, $NewEnsureReady)
    Set-Content $DistributionRs $DistributionSource -NoNewline
    $AppRuntimeAliasPatch = Select-String -Path $AppRs -Pattern 'CHRYS_RUNTIME_EXE|CHRYS_RUNTIMEW_EXE'
    $AppMacRuntimeAliasPatch = Select-String -Path $AppRs -Pattern 'target_os = "macos"'
    $AppLinuxRuntimeAliasPatch = Select-String -Path $AppRs -Pattern 'target_os = "linux"'
    $DistributionRuntimeAliasPatch = Select-String -Path $DistributionRs -Pattern 'ensure_runtime_aliases'
    $DistributionPatchedSource = Get-Content $DistributionRs -Raw
    $RuntimeRunPathPatch = $DistributionPatchedSource -match 'python_command\(&app::runtime_python_path\(\)\)'
    $RuntimePthPatch = $DistributionPatchedSource -match 'source_pth_path'
    $RuntimeHardLinkPatch = $DistributionPatchedSource -match 'fs::hard_link'
    $RuntimeAliasAfterInstall = $DistributionPatchedSource -match 'install_project\(\)\?;[\s\S]*ensure_runtime_aliases\(\)\?;'
    $RuntimeAliasBeforeUnlock = $DistributionPatchedSource -match 'ensure_runtime_aliases\(\)\?;[\s\S]*FileExt::unlock\(&lock_file\)'
    if (
        -not $AppRuntimeAliasPatch `
        -or -not $AppMacRuntimeAliasPatch `
        -or -not $AppLinuxRuntimeAliasPatch `
        -or -not $DistributionRuntimeAliasPatch `
        -or -not $RuntimeRunPathPatch `
        -or -not $RuntimePthPatch `
        -or -not $RuntimeHardLinkPatch `
        -or -not $RuntimeAliasAfterInstall `
        -or -not $RuntimeAliasBeforeUnlock
    ) {
        throw "failed to patch PyApp runtime aliases"
    }

    # ── Build ─────────────────────────────────────────────────────────
    Write-Host "==> Compiling binary..."
    cargo build --release
    if ($LASTEXITCODE -ne 0) { throw "cargo build failed" }

    # ── Copy output ───────────────────────────────────────────────────
    $DistDir = Join-Path $ProjectRoot "dist"
    if (-not (Test-Path $DistDir)) { New-Item -ItemType Directory -Path $DistDir | Out-Null }
    # AIxCoding branding: PyApp output name
    $Output = Join-Path $DistDir "aixcoding-cli.exe"
    Copy-Item "target\release\pyapp.exe" $Output

    $SizeMB = [math]::Round((Get-Item $Output).Length / 1MB, 1)
    Write-Host "==> Done: $Output ($SizeMB MB)"
    if ($Offline) {
        Write-Host "    First run: unpacks the bundled distribution (~10s), no network needed"
    } elseif ($UseUv) {
        Write-Host "    First run: uv installs deps from PyPI (~10s, cached after)"
        Write-Host "    Corp network: set UV_INDEX_URL=https://your-mirror/simple"
    } else {
        Write-Host "    First run: pip installs deps from PyPI (~30s, cached after)"
        Write-Host "    Corp network: set PIP_INDEX_URL=https://your-mirror/simple"
    }
} finally {
    Set-Location $ProjectRoot
    Remove-Item -Recurse -Force $BuildDir -ErrorAction SilentlyContinue
}
