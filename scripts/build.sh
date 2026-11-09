#!/usr/bin/env bash
# Build a self-contained chrys binary for the current platform using PyApp.
#
# Prerequisites: Rust toolchain (cargo), uv, Python 3.14+
#                On Linux: musl-tools (apt-get install musl-tools) for static binaries
#
# Usage:
#   ./scripts/build.sh              # default: uses pip (no GitHub needed at runtime)
#   ./scripts/build.sh --uv         # uses uv (faster, but downloads uv from GitHub on first run)
#   ./scripts/build.sh --offline    # bundles every dependency; first run needs no network
#
# Environment variables:
#   PYAPP_VERSION  — PyApp release to use (default: 0.29.0)
#   PYAPP_SOURCE   — path to local PyApp source.tar.gz (skips GitHub download)
#   PYTHON_DIST    — path to local python-build-standalone tarball (skips GitHub download)
#   OFFLINE_DIST   — path to a prebuilt offline distribution (skips building one)
#
# By default the binary embeds Python plus the iCode wheel, and the chosen
# installer fetches the dependencies from PyPI on first run.  With --offline
# the embedded distribution already contains chrys and every dependency, so
# the first run only unpacks it — no installer, no PyPI, no network.
#
# Output: dist/aixcoding-cli (or dist/aixcoding-cli.exe on Windows)

set -euo pipefail

USE_UV=false
OFFLINE=false
for arg in "$@"; do
    case "$arg" in
        --uv) USE_UV=true ;;
        --offline) OFFLINE=true ;;
        *) echo "Unknown argument: $arg"; echo "Usage: $0 [--uv | --offline]"; exit 1 ;;
    esac
done

if [ "$USE_UV" = "true" ] && [ "$OFFLINE" = "true" ]; then
    echo "Error: --uv and --offline are mutually exclusive; an offline build runs no installer" >&2
    exit 1
fi

PYAPP_VERSION="${PYAPP_VERSION:-0.29.0}"
# The binary ships the CPython version .python-version pins (the one CI tests
# on), built by this python-build-standalone release; bump it together with
# PYTHON_BUILD_STANDALONE_RELEASE in .github/workflows/cd.yml.
PYTHON_BUILD_STANDALONE_RELEASE=20261026
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
CPYTHON_VERSION="$(tr -d '[:space:]' < "$PROJECT_ROOT/.python-version")"
if ! [[ "$CPYTHON_VERSION" =~ ^3\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: .python-version must pin a full CPython version, got '$CPYTHON_VERSION'" >&2
    exit 1
fi
PYTHON_VERSION="${CPYTHON_VERSION%.*}"

# Env-var paths may be relative to the caller's directory; resolve them before
# any cd so they survive the working-directory changes below.  "?:*" spares
# Git Bash drive-letter paths.
for var in PYAPP_SOURCE PYTHON_DIST OFFLINE_DIST; do
    val="${!var:-}"
    case "$val" in
        ""|/*|?:*) ;;
        *) printf -v "$var" '%s/%s' "$PWD" "$val" ;;
    esac
done

cd "$PROJECT_ROOT"

# Extract version from pyproject.toml. --no-project: syncing the project here
# would build its source-only dependencies without the offline build's pins.
VERSION=$(uv run --no-project python -c "
import tomllib, pathlib
data = tomllib.loads(pathlib.Path('pyproject.toml').read_text())
print(data['project']['version'])
")

echo "==> Building chrys v${VERSION} (PyApp v${PYAPP_VERSION})"

# ── Fetch ripgrep binary ──────────────────────────────────────────────
echo "==> Fetching rg binary for bundling..."
"$SCRIPT_DIR/fetch_rg.sh"

# ── Build wheel ───────────────────────────────────────────────────────
echo "==> Building wheel..."
uv build --wheel --quiet

# ── Prepare PyApp source ─────────────────────────────────────────────
BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$BUILD_DIR"' EXIT

if [ -n "${PYAPP_SOURCE:-}" ]; then
    echo "==> Using local PyApp source: $PYAPP_SOURCE"
    tar xzf "$PYAPP_SOURCE" -C "$BUILD_DIR"
else
    echo "==> Downloading PyApp source..."
    PYAPP_URL="https://github.com/ofek/pyapp/releases/download/v${PYAPP_VERSION}/source.tar.gz"
    if command -v gh &>/dev/null && gh release download "v${PYAPP_VERSION}" --repo ofek/pyapp --pattern source.tar.gz --dir "$BUILD_DIR" 2>/dev/null; then
        tar xzf "$BUILD_DIR/source.tar.gz" -C "$BUILD_DIR"
    else
        curl -fsSL "$PYAPP_URL" | tar xz -C "$BUILD_DIR"
    fi
fi

PYAPP_DIR="$BUILD_DIR/pyapp-v${PYAPP_VERSION}"
WHEEL_SOURCE="dist/icode_tui-${VERSION}-py3-none-any.whl"
if [ ! -f "$WHEEL_SOURCE" ]; then
    WHEEL_SOURCE="$(ls -t dist/icode_tui-*.whl | head -1)"
fi
cp "$WHEEL_SOURCE" "$PYAPP_DIR/"
cd "$PYAPP_DIR"

WHEEL="$(basename "$WHEEL_SOURCE")"
BUILD_OS="$(uname -s)"
BUILD_ARCH="$(uname -m)"
case "$BUILD_OS" in
    Linux|Darwin|MINGW*|MSYS*|CYGWIN*) BUILD_USES_RUNTIME_ALIAS=true ;;
    *) BUILD_USES_RUNTIME_ALIAS=false ;;
esac

BUILD_ON_WINDOWS=false
if [[ "$BUILD_OS" == MINGW* ]] || [[ "$BUILD_OS" == MSYS* ]] || [[ "$BUILD_OS" == CYGWIN* ]]; then
    BUILD_ON_WINDOWS=true
fi

# ── Resolve the Python distribution ───────────────────────────────────
# The pinned CPython from the pinned python-build-standalone release, rather
# than the one PyApp's build.rs names.
#
# Linux always resolves to the glibc build.  The musl Rust target below makes
# the launcher itself dependency-free, but python-build-standalone's musl
# distribution is dynamically linked against musl (PT_INTERP
# /lib/ld-musl-x86_64.so.1), so it runs on Alpine and nowhere else; its
# statically linked +static variant cannot dlopen extension modules at all,
# which rules out cryptography, lxml, pydantic-core and friends.  The glibc
# distribution only needs GLIBC_2.17 — CentOS 7 and Ubuntu 14.04 era.
DIST_TRIPLE=""
case "$BUILD_OS-$BUILD_ARCH" in
    Linux-x86_64)  DIST_TRIPLE="x86_64-unknown-linux-gnu" ;;
    Linux-aarch64) DIST_TRIPLE="aarch64-unknown-linux-gnu" ;;
    Darwin-arm64)  DIST_TRIPLE="aarch64-apple-darwin" ;;
    Darwin-x86_64) DIST_TRIPLE="x86_64-apple-darwin" ;;
    MINGW*|MSYS*|CYGWIN*) DIST_TRIPLE="x86_64-pc-windows-msvc" ;;
esac

DIST_URL=""
if [ -n "$DIST_TRIPLE" ]; then
    DIST_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_BUILD_STANDALONE_RELEASE}/cpython-${CPYTHON_VERSION}+${PYTHON_BUILD_STANDALONE_RELEASE}-${DIST_TRIPLE}-install_only_stripped.tar.gz"
fi

# ── Configure PyApp ───────────────────────────────────────────────────
export PYAPP_PROJECT_NAME=chrys
export PYAPP_PROJECT_VERSION="$VERSION"
export PYAPP_PYTHON_VERSION="$PYTHON_VERSION"
export PYAPP_EXEC_SPEC=chrys.app.cli.app:pyapp_main
# iCode has its own upgrade and uninstall paths, so PyApp's `self` commands
# (update, remove, restore, ...) stay hidden: `none` names them with a random
# string nobody types.
export PYAPP_SELF_COMMAND=none
export PYAPP_PASS_LOCATION=true

if [ "$OFFLINE" = "true" ]; then
    echo "==> Installer: none (chrys and all dependencies are bundled)"

    OFFLINE_ARCHIVE="chrys-offline-dist.tar.gz"
    if [ -n "${OFFLINE_DIST:-}" ]; then
        echo "==> Using prebuilt offline distribution: $OFFLINE_DIST"
        cp "$OFFLINE_DIST" "$OFFLINE_ARCHIVE"
    else
        if [ -n "${PYTHON_DIST:-}" ]; then
            echo "==> Using local Python distribution: $PYTHON_DIST"
            DIST_ARGS=(--dist-archive "$PYTHON_DIST")
        else
            if [ -z "$DIST_URL" ]; then
                echo "Error: no python-build-standalone build for ${BUILD_OS}-${BUILD_ARCH}" >&2
                exit 1
            fi
            DIST_ARGS=(--dist-url "$DIST_URL")
        fi
        "$SCRIPT_DIR/build_offline_dist.sh" \
            "${DIST_ARGS[@]}" \
            --wheel "$PYAPP_DIR/$WHEEL" \
            --output "$PYAPP_DIR/$OFFLINE_ARCHIVE"
    fi

    # PYAPP_DISTRIBUTION_PATH embeds the archive as-is (and must not be
    # combined with PYAPP_DISTRIBUTION_SOURCE — build.rs panics).  With
    # PYAPP_SKIP_INSTALL, first run reduces to unpacking it.
    export PYAPP_DISTRIBUTION_PATH="$OFFLINE_ARCHIVE"
    export PYAPP_DISTRIBUTION_PATH_PREFIX=python
    export PYAPP_SKIP_INSTALL=true
    # FULL_ISOLATION makes PyApp run the unpacked interpreter directly instead
    # of building a virtualenv around it — the only mode where the bundled
    # site-packages is the one actually used.
    export PYAPP_FULL_ISOLATION=true
    # These are relative to PATH_PREFIX, which build.rs prepends for us.
    if [ "$BUILD_ON_WINDOWS" = "true" ]; then
        export PYAPP_DISTRIBUTION_PYTHON_PATH="python.exe"
        export PYAPP_DISTRIBUTION_SITE_PACKAGES_PATH="Lib/site-packages"
    else
        export PYAPP_DISTRIBUTION_PYTHON_PATH="bin/python3"
        export PYAPP_DISTRIBUTION_SITE_PACKAGES_PATH="lib/python${PYTHON_VERSION}/site-packages"
    fi
else
    export PYAPP_PROJECT_PATH="$WHEEL"
    export PYAPP_PIP_ALLOW_CONFIG=true
    export PYAPP_DISTRIBUTION_EMBED=true

    if [ "$USE_UV" = "true" ]; then
        echo "==> Installer: uv (fast, downloads uv from GitHub on first run)"
        export PYAPP_UV_ENABLED=true
    else
        echo "==> Installer: pip (bundled, no extra downloads)"
        export PYAPP_FULL_ISOLATION=true
        export PYAPP_DISTRIBUTION_PIP_AVAILABLE=true
    fi

    # Use local Python distribution if provided (avoids GitHub download during build)
    if [ -n "${PYTHON_DIST:-}" ]; then
        echo "==> Using local Python distribution: $PYTHON_DIST"
        cp "$PYTHON_DIST" .
        export PYAPP_DISTRIBUTION_PATH="$(basename "$PYTHON_DIST")"
        if [ "$BUILD_ON_WINDOWS" = "true" ]; then
            export PYAPP_DISTRIBUTION_PYTHON_PATH="python/python.exe"
        else
            export PYAPP_DISTRIBUTION_PYTHON_PATH="python/bin/python3"
        fi
    fi
fi

# ── Patch PyApp ───────────────────────────────────────────────────────
# 1. Tolerate non-UTF-8 subprocess output (fixes non-English Windows)
echo "==> Patching PyApp for UTF-8 compatibility..."
perl -i -pe '
  s/let mut output = String::new\(\);/let mut raw = Vec::new();/;
  s/reader\.read_to_string\(\&mut output\)\?;/reader.read_to_end(\&mut raw)?;/;
  s/Ok\(\(result\?, output\)\)/let output = String::from_utf8_lossy(\&raw).into_owned();\n    Ok((result?, output))/;
' src/process.rs

# 2. Increase download timeout (default 30s is too short for large Python distributions)
echo "==> Patching PyApp download timeout (300s)..."
perl -i -pe '
  s/reqwest::blocking::get\(([^)]+)\)/reqwest::blocking::Client::builder().timeout(std::time::Duration::from_secs(300)).build().unwrap().get($1).send()/g;
' build.rs

if [ "$BUILD_USES_RUNTIME_ALIAS" = "true" ]; then
    # PyApp launches Chrys by spawning the unpacked Python executable. On
    # Windows, macOS, and Linux, process-name cleanup tools can still match
    # that interpreter name, so run Chrys through a sibling alias.
    echo "==> Patching PyApp to run Chrys through renamed Python..."
    perl -0pi -e '
      s/pub fn pythonw_path\(\) -> PathBuf \{\n    install_dir\(\)\.join\(installation_pythonw_path\(\)\)\n\}/pub fn pythonw_path() -> PathBuf {\n    install_dir().join(installation_pythonw_path())\n}\n\n#[cfg(windows)]\npub const CHRYS_RUNTIME_EXE: \&str = "chrys-runtime.exe";\n\n#[cfg(any(target_os = "macos", target_os = "linux"))]\npub const CHRYS_RUNTIME_EXE: \&str = "chrys-runtime";\n\n#[cfg(windows)]\npub const CHRYS_RUNTIMEW_EXE: \&str = "chrys-runtimew.exe";\n\n#[cfg(any(windows, target_os = "macos", target_os = "linux"))]\nfn renamed_python_path(original: PathBuf, alias: \&str) -> PathBuf {\n    if let Some(path) = original.parent().map(|parent| parent.join(alias)) {\n        if path.is_file() {\n            return path;\n        }\n        if cfg!(debug_assertions) {\n            eprintln!(\n                "Chrys runtime alias {} is missing; falling back to {}",\n                path.display(),\n                original.display()\n            );\n        }\n    }\n    original\n}\n\n#[cfg(any(windows, target_os = "macos", target_os = "linux"))]\npub fn runtime_python_path() -> PathBuf {\n    renamed_python_path(python_path(), CHRYS_RUNTIME_EXE)\n}\n\n#[cfg(not(any(windows, target_os = "macos", target_os = "linux")))]\npub fn runtime_python_path() -> PathBuf {\n    python_path()\n}\n\n#[cfg(windows)]\npub fn runtime_pythonw_path() -> PathBuf {\n    renamed_python_path(pythonw_path(), CHRYS_RUNTIMEW_EXE)\n}/;
    ' src/app.rs
    perl -0pi -e '
      s/\npub fn run_project\(\) -> Result<\(\)> \{\n    let mut command = python_command\(\&app::python_path\(\)\);\n\n    #\[cfg\(windows\)\]\n    \{\n        if app::is_gui\(\) \{\n            command = python_command\(\&app::pythonw_path\(\)\);\n        \}\n    \}/\n#[cfg(any(windows, target_os = "macos", target_os = "linux"))]\nfn link_or_copy_file(source: \&Path, target: \&Path) -> Result<()> {\n    if target.is_file() {\n        return Ok(());\n    }\n    fs::hard_link(source, target)\n        .or_else(|_| fs::copy(source, target).map(|_| ()))\n        .with_context(|| {\n            format!(\n                "unable to create Chrys runtime alias {} from {}",\n                \&target.display(),\n                \&source.display()\n            )\n        })\n}\n\n#[cfg(windows)]\nfn source_pth_path(source: \&Path) -> Option<std::path::PathBuf> {\n    let executable_pth = source.with_extension("_pth");\n    if executable_pth.is_file() {\n        return Some(executable_pth);\n    }\n\n    let python_pth = source.parent()?.join("python._pth");\n    python_pth.is_file().then_some(python_pth)\n}\n\n#[cfg(any(windows, target_os = "macos", target_os = "linux"))]\nfn ensure_runtime_alias(source: \&Path, alias_name: \&str) -> Result<()> {\n    if source.file_name() == Some(OsStr::new(alias_name)) {\n        return Ok(());\n    }\n    let alias = source\n        .parent()\n        .with_context(|| format!("Chrys runtime alias source has no parent: {}", source.display()))?\n        .join(alias_name);\n    link_or_copy_file(source, \&alias)?;\n\n    #[cfg(windows)]\n    {\n        if let Some(source_pth) = source_pth_path(source) {\n            link_or_copy_file(\&source_pth, \&alias.with_extension("_pth"))?;\n        }\n    }\n\n    Ok(())\n}\n\n#[cfg(any(windows, target_os = "macos", target_os = "linux"))]\nfn ensure_runtime_aliases() -> Result<()> {\n    ensure_runtime_alias(\&app::python_path(), app::CHRYS_RUNTIME_EXE)?;\n    #[cfg(windows)]\n    {\n        let pythonw_path = app::pythonw_path();\n        if pythonw_path.is_file() {\n            ensure_runtime_alias(\&pythonw_path, app::CHRYS_RUNTIMEW_EXE)?;\n        }\n    }\n    Ok(())\n}\n\npub fn run_project() -> Result<()> {\n    let mut command = python_command(\&app::runtime_python_path());\n\n    #[cfg(windows)]\n    {\n        if app::is_gui() {\n            command = python_command(\&app::runtime_pythonw_path());\n        }\n    }/;
      s/\n    FileExt::unlock\(\&lock_file\)/\n    #[cfg(any(windows, target_os = "macos", target_os = "linux"))]\n    ensure_runtime_aliases()?;\n\n    FileExt::unlock(\&lock_file)/;
    ' src/distribution.rs
    if ! grep -E 'CHRYS_RUNTIME_EXE|CHRYS_RUNTIMEW_EXE' src/app.rs >/dev/null \
        || ! grep -E 'target_os = "macos"' src/app.rs >/dev/null \
        || ! grep -E 'target_os = "linux"' src/app.rs >/dev/null \
        || ! grep -E 'ensure_runtime_aliases' src/distribution.rs >/dev/null \
        || ! grep -E 'runtime_python_path' src/distribution.rs >/dev/null \
        || ! grep -E 'source_pth_path' src/distribution.rs >/dev/null \
        || ! grep -E 'fs::hard_link' src/distribution.rs >/dev/null \
        || ! perl -0ne 'exit(/install_project\(\)\?;.*ensure_runtime_aliases\(\)\?;/s ? 0 : 1)' src/distribution.rs \
        || ! perl -0ne 'exit(/ensure_runtime_aliases\(\)\?;.*FileExt::unlock\(\&lock_file\)/s ? 0 : 1)' src/distribution.rs; then
        echo "Error: failed to patch PyApp runtime aliases" >&2
        exit 1
    fi
fi

# ── Determine build target ────────────────────────────────────────────
# On Linux, use musl target for static binaries (no glibc dependency).
TARGET=""
case "$BUILD_OS-$BUILD_ARCH" in
    Linux-x86_64)  TARGET="x86_64-unknown-linux-musl" ;;
    Linux-aarch64) TARGET="aarch64-unknown-linux-musl" ;;
esac

# ── Pin the Python distribution ──────────────────────────────────────
# Installer builds embed the pinned distribution as well.  On Linux it is
# also the glibc build: left to itself, the musl Rust target makes PyApp
# embed a musl Python that most Linux users cannot run.  Offline builds and a
# local PYTHON_DIST already embed a distribution via PYAPP_DISTRIBUTION_PATH,
# and setting a source alongside a path makes build.rs panic.
if [ "$OFFLINE" != "true" ] && [ -z "${PYTHON_DIST:-}" ] && [ -n "$DIST_URL" ]; then
    export PYAPP_DISTRIBUTION_SOURCE="$DIST_URL"
    echo "==> Python distribution: $DIST_URL"
fi

# ── Build ─────────────────────────────────────────────────────────────
echo "==> Compiling binary..."
if [ -n "$TARGET" ]; then
    echo "    Target: $TARGET (static, no glibc dependency)"
    rustup target add "$TARGET" 2>/dev/null || true
    cargo build --release --target "$TARGET"
    RELEASE_DIR="target/$TARGET/release"
else
    cargo build --release
    RELEASE_DIR="target/release"
fi

# ── Copy output ───────────────────────────────────────────────────────
# AIxCoding branding: PyApp output name
if [ "$BUILD_ON_WINDOWS" = "true" ]; then
    OUTPUT="$PROJECT_ROOT/dist/aixcoding-cli.exe"
    cp "$RELEASE_DIR/pyapp.exe" "$OUTPUT"
else
    OUTPUT="$PROJECT_ROOT/dist/aixcoding-cli"
    cp "$RELEASE_DIR/pyapp" "$OUTPUT"
    chmod +x "$OUTPUT"
fi

SIZE=$(du -h "$OUTPUT" | cut -f1)
echo "==> Done: $OUTPUT ($SIZE)"
if [ "$OFFLINE" = "true" ]; then
    echo "    First run: unpacks the bundled distribution (~10s), no network needed"
elif [ "$USE_UV" = "true" ]; then
    echo "    First run: uv installs deps from PyPI (~10s, cached after)"
    echo "    Corp network: set UV_INDEX_URL=https://your-mirror/simple"
else
    echo "    First run: pip installs deps from PyPI (~30s, cached after)"
    echo "    Corp network: set PIP_INDEX_URL=https://your-mirror/simple"
fi
