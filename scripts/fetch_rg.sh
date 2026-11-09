#!/usr/bin/env bash
# Download the ripgrep (rg) binary for the current platform and place it in
# src/chrys/foundation/vendor/ripgrep/ so it gets bundled into the wheel.
#
# Usage:
#   ./scripts/fetch_rg.sh                                        # download for current platform
#   ./scripts/fetch_rg.sh --all                                  # download for ALL platforms (CD)
#   ./scripts/fetch_rg.sh --version 14.1.1                       # specific version
#   ./scripts/fetch_rg.sh --archive /path/to/ripgrep-*.tar.gz    # use local archive
#
# When --all is given, binaries are saved with platform-suffixed names
# (e.g. rg-x86_64-apple-darwin, rg-x86_64-pc-windows-msvc.exe) so a single
# wheel can serve every platform.  Without --all, the binary is saved as
# plain "rg" (or "rg.exe") for the current host.
#
# Environment variables (override CLI flags):
#   RG_VERSION   — ripgrep version (default: 15.2.0)
#   RG_BASE_URL  — base URL for downloads (default: GitHub releases URL)
#   RG_ARCHIVE   — path to pre-downloaded archive (skips download entirely)

set -euo pipefail

RG_VERSION="${RG_VERSION:-15.2.0}"
RG_BASE_URL="${RG_BASE_URL:-https://github.com/BurntSushi/ripgrep/releases/download}"
RG_ARCHIVE="${RG_ARCHIVE:-}"
FETCH_ALL=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) RG_VERSION="$2"; shift 2 ;;
        --archive) RG_ARCHIVE="$2"; shift 2 ;;
        --all)     FETCH_ALL=true; shift ;;
        *) echo "Unknown argument: $1"; echo "Usage: $0 [--all] [--version VER] [--archive PATH]"; exit 1 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST_DIR="$SCRIPT_DIR/../src/chrys/foundation/vendor/ripgrep"
mkdir -p "$DEST_DIR"
DEST_DIR="$(cd "$DEST_DIR" && pwd)"

# ── Helper: download and extract one triple ──────────────────────────
fetch_one() {
    local triple="$1"
    local dest_name="$2"
    local is_windows="$3"

    if [ -n "$RG_ARCHIVE" ]; then
        echo "==> Using local archive: $RG_ARCHIVE"
        local archive_path="$RG_ARCHIVE"
    else
        local ext="tar.gz"
        [ "$is_windows" = "true" ] && ext="zip"
        local archive_name="ripgrep-${RG_VERSION}-${triple}.${ext}"
        local url="${RG_BASE_URL}/${RG_VERSION}/${archive_name}"
        echo "==> Downloading rg ${RG_VERSION} for ${triple}..."
        echo "    ${url}"
        local tmpdir
        tmpdir=$(mktemp -d)
        local archive_path="$tmpdir/$archive_name"
        # The release CDN answers 5xx now and then, and an outage can hold for minutes: curl backs
        # off and asks again, bounded overall (the final backoff may overrun the budget slightly).
        # The connect and a stalled transfer are each bounded too, so a wedged CDN fails this step
        # in minutes; a stall that recovers restarts under the retry budget (exit 28 retries).
        curl -fsSL --retry 10 --retry-max-time 300 --retry-connrefused --connect-timeout 30 --speed-limit 1024 --speed-time 60 "$url" -o "$archive_path"
    fi

    echo "==> Extracting $dest_name to $DEST_DIR..."
    local extract_dir
    extract_dir=$(mktemp -d)

    if [ "$is_windows" = "true" ]; then
        unzip -q "$archive_path" -d "$extract_dir"
        find "$extract_dir" -name rg.exe -type f -exec cp {} "$DEST_DIR/$dest_name" \;
    else
        tar xzf "$archive_path" -C "$extract_dir"
        find "$extract_dir" -name rg -type f -exec cp {} "$DEST_DIR/$dest_name" \;
        chmod +x "$DEST_DIR/$dest_name"
    fi

    rm -rf "$extract_dir"
    [ -n "${tmpdir:-}" ] && rm -rf "$tmpdir"
    echo "==> Done: $DEST_DIR/$dest_name"
}

# ── Fetch all platforms (for CD wheel build) ─────────────────────────
if [ "$FETCH_ALL" = "true" ]; then
    echo "==> Fetching rg ${RG_VERSION} for ALL platforms..."
    fetch_one "aarch64-apple-darwin"        "rg-aarch64-apple-darwin"        false
    fetch_one "x86_64-apple-darwin"         "rg-x86_64-apple-darwin"         false
    fetch_one "x86_64-unknown-linux-musl"   "rg-x86_64-unknown-linux-musl"   false
    fetch_one "aarch64-unknown-linux-gnu"   "rg-aarch64-unknown-linux-gnu"   false
    fetch_one "x86_64-pc-windows-msvc"      "rg-x86_64-pc-windows-msvc.exe"  true
    fetch_one "aarch64-pc-windows-msvc"     "rg-aarch64-pc-windows-msvc.exe" true
    echo "==> All platforms fetched."
    exit 0
fi

# ── Single platform (local dev / CI) ─────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS-$ARCH" in
    Darwin-arm64)   TRIPLE="aarch64-apple-darwin" ;;
    Darwin-x86_64)  TRIPLE="x86_64-apple-darwin" ;;
    Linux-x86_64)   TRIPLE="x86_64-unknown-linux-musl" ;;
    Linux-aarch64)  TRIPLE="aarch64-unknown-linux-gnu" ;;
    *)
        echo "Error: unsupported platform $OS-$ARCH"
        echo "Supported: macOS (arm64/x86_64), Linux (x86_64/aarch64)"
        exit 1
        ;;
esac

fetch_one "$TRIPLE" "rg" false
