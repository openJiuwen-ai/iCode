#!/bin/sh
# Install or upgrade AIxCoding from its offline package (macOS and Linux).
#
#   curl -fsSL https://raw.githubusercontent.com/openJiuwen-ai/iCode/main/scripts/install.sh | sh
#   curl -fsSL https://raw.gitcode.com/openJiuwen/iCode/raw/main/scripts/install.sh | sh
#
# Options, after `sh -s --` when piped (or as environment variables):
#   --version <X.Y.Z>        install that version instead of the latest   (ICODE_VERSION)
#   --source github|gitcode  download only from there                     (ICODE_SOURCE)
#
# It downloads the package for this machine from GitHub Releases, or from GitCode when GitHub
# fails, checks it against the release's SHA256SUMS.txt and runs `aixcoding-cli install`, which puts
# `chrys` and its `aixcoding-cli` alias in ~/.local/bin. Running it again upgrades.
#
# Everything sits in main() so a download cut short runs nothing.

main() {
    set -eu

    github_repo="openJiuwen-ai/iCode"
    gitcode_repo="openJiuwen/iCode"
    bin_dir="$HOME/.local/bin"

    version="${ICODE_VERSION:-}"
    source="${ICODE_SOURCE:-}"
    while [ $# -gt 0 ]; do
        case "$1" in
            --version)
                [ $# -ge 2 ] && [ -n "$2" ] || fail "--version needs a value"
                version="$2"
                shift 2
                ;;
            --version=*)
                [ -n "${1#*=}" ] || fail "--version needs a value"
                version="${1#*=}"
                shift
                ;;
            --source)
                [ $# -ge 2 ] && [ -n "$2" ] || fail "--source needs a value"
                source="$2"
                shift 2
                ;;
            --source=*)
                [ -n "${1#*=}" ] || fail "--source needs a value"
                source="${1#*=}"
                shift
                ;;
            -h | --help)
                usage
                return 0
                ;;
            *) fail "unknown option: $1" ;;
        esac
    done
    version="${version#v}"
    if [ -n "$version" ] && ! is_version "$version"; then
        fail "--version takes a version number such as 0.29.1, not '$version'"
    fi
    case "$source" in
        "") sources="github gitcode" ;;
        github | gitcode) sources="$source" ;;
        *) fail "--source must be github or gitcode, not '$source'" ;;
    esac

    detect_platform
    check_aixcoding_cli_command
    find_downloader
    find_hasher

    tmp="$(mktemp -d 2>/dev/null || mktemp -d -t icode)"
    trap 'rm -rf "$tmp"' EXIT
    trap 'exit 130' INT TERM

    for host in $sources; do
        if install_from "$host"; then
            return 0
        fi
        say "Could not install from $(host_name "$host")."
    done
    if [ -n "$version" ]; then
        fail "could not install AIxCoding $version. Check that this version is listed at https://github.com/$github_repo/releases and that you are online."
    fi
    fail "the download failed. Check your network connection, or try again later."
}

usage() {
    cat <<'USAGE'
Install or upgrade AIxCoding from its offline package.

Options:
  --version <X.Y.Z>        install that version instead of the latest
  --source github|gitcode  download only from there (default: GitHub, then GitCode)
USAGE
}

say() {
    printf '%s\n' "$*"
}

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

is_version() {
    case "$1" in
        "" | *[!0-9.]* | .* | *. | *..*) return 1 ;;
    esac
    # Exactly three numbers.
    [ "$(printf '%s' "$1" | tr -cd . | wc -c | tr -d ' ')" = 2 ]
}

# is_newer <a> <b>: whether version a comes after version b.
is_newer() {
    old_ifs="$IFS"
    IFS=.
    # shellcheck disable=SC2086 # Split on the dots.
    set -- $1 $2
    IFS="$old_ifs"
    [ "$1" -gt "$4" ] || { [ "$1" -eq "$4" ] && { [ "$2" -gt "$5" ] || { [ "$2" -eq "$5" ] && [ "$3" -gt "$6" ]; }; }; }
}

detect_platform() {
    case "$(uname -s)" in
        Darwin) os=macos ;;
        Linux) os=linux ;;
        *) fail "this script installs AIxCoding on macOS and Linux; on Windows, use install.ps1" ;;
    esac
    case "$(uname -m)" in
        x86_64 | amd64) arch=x86_64 ;;
        arm64 | aarch64) arch=aarch64 ;;
        *) fail "there is no AIxCoding package for $(uname -m) processors" ;;
    esac
    # A shell running under Rosetta reports x86_64 on Apple silicon.
    if [ "$os" = macos ] && [ "$arch" = x86_64 ] && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || true)" = 1 ]; then
        arch=aarch64
    fi
    if [ "$os" = linux ] && ldd --version 2>&1 | grep -qi musl; then
        fail "Linux distributions that use musl, such as Alpine, are not supported"
    fi
}

# AIxCoding branding: POSIX install command contract
# `aixcoding-cli install` leaves an `aixcoding-cli` that is not its own alias alone, so after an
# install that `aixcoding-cli` would still start whatever it starts now.
check_aixcoding_cli_command() {
    for found in "$bin_dir/aixcoding-cli" "$(command -v aixcoding-cli 2>/dev/null || true)"; do
        if [ -z "$found" ] || [ ! -e "$found" ] || [ "$found" -ef "$bin_dir/chrys" ]; then
            continue
        fi
        if [ -x "$bin_dir/chrys" ]; then
            # An offline install that already lives with this `aixcoding-cli` is started with `chrys`;
            # upgrading it changes nothing about that.
            printf 'Warning: %s does not start the AIxCoding offline install; start that with chrys.\n' "$found" >&2
            return 0
        fi
        printf 'Error: %s is not an AIxCoding offline install, so this script would not replace it.\n' "$found" >&2
        printf 'If it is AIxCoding installed with uv or pipx, upgrade it with that tool (for example,\n' >&2
        printf 'uv tool upgrade iCode-TUI), or uninstall it first and run this script again.\n' >&2
        exit 1
    done
}

find_downloader() {
    if command -v curl >/dev/null 2>&1; then
        downloader=curl
    elif command -v wget >/dev/null 2>&1; then
        downloader=wget
    else
        fail "this script needs curl or wget"
    fi
}

find_hasher() {
    if command -v sha256sum >/dev/null 2>&1; then
        hasher="sha256sum"
    elif command -v shasum >/dev/null 2>&1; then
        hasher="shasum -a 256"
    else
        fail "this script needs sha256sum or shasum to check the download"
    fi
}

host_name() {
    case "$1" in
        github) printf 'GitHub' ;;
        gitcode) printf 'GitCode' ;;
    esac
}

# fetch <url> <file> [quiet]: download, giving up on a connection slower than 20 KB/s for 30 s.
fetch() {
    if [ "$downloader" = curl ]; then
        if [ "${3:-}" = quiet ]; then
            curl -fsSL --retry 2 --connect-timeout 15 --speed-limit 20480 --speed-time 30 -o "$2" "$1"
        else
            curl -fL --progress-bar --retry 2 --connect-timeout 15 --speed-limit 20480 --speed-time 30 -o "$2" "$1"
        fi
    else
        if [ "${3:-}" = quiet ]; then
            wget -q -T 30 -O "$2" "$1"
        else
            wget -T 30 -O "$2" "$1"
        fi
    fi
}

# GitCode has no releases/latest link, so ask its API, whose anonymous quota is shared by everyone
# and often runs out; the version on main is the next best guess, and the checksum download that
# follows proves whether that release exists.
gitcode_latest_tag() {
    if fetch "https://api.gitcode.com/api/v5/repos/$gitcode_repo/releases/latest" "$tmp/latest.json" quiet 2>/dev/null; then
        latest="$(sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' "$tmp/latest.json" | head -n 1)"
        if [ -n "$latest" ]; then
            printf '%s' "$latest"
            return 0
        fi
    fi
    if fetch "https://raw.gitcode.com/$gitcode_repo/raw/main/pyproject.toml" "$tmp/pyproject.toml" quiet 2>/dev/null; then
        latest="$(sed -n 's/^version *= *"\([^"]*\)".*/\1/p' "$tmp/pyproject.toml" | head -n 1)"
        if [ -n "$latest" ]; then
            printf 'v%s' "$latest"
            return 0
        fi
    fi
    return 1
}

release_url() {
    case "$1" in
        github) printf 'https://github.com/%s/releases/download/%s/%s' "$github_repo" "$2" "$3" ;;
        gitcode) printf 'https://gitcode.com/%s/releases/download/%s/%s' "$gitcode_repo" "$2" "$3" ;;
    esac
}

# The name of this machine's package in a SHA256SUMS.txt, which also carries its version.
# AIxCoding branding: POSIX install command contract
package_in() {
    awk -v prefix="aixcoding-cli-$os-$arch-v" -v suffix="-offline.tar.gz" '
        { name = $2; sub(/^\*/, "", name) }
        index(name, prefix) == 1 && length(name) > length(prefix suffix) &&
            substr(name, length(name) - length(suffix) + 1) == suffix { print name; exit }
    ' "$1"
}

install_from() {
    host="$1"
    if [ -n "$version" ]; then
        sums_url="$(release_url "$host" "v$version" SHA256SUMS.txt)"
    elif [ "$host" = github ]; then
        # Redirects to the newest release without the API and its hourly quota.
        sums_url="https://github.com/$github_repo/releases/latest/download/SHA256SUMS.txt"
    elif tag="$(gitcode_latest_tag)"; then
        sums_url="$(release_url "$host" "$tag" SHA256SUMS.txt)"
    else
        say "Could not find the latest AIxCoding version on $(host_name "$host")."
        return 1
    fi
    rm -f "$tmp/SHA256SUMS.txt"
    fetch "$sums_url" "$tmp/SHA256SUMS.txt" quiet || return 1

    package="$(package_in "$tmp/SHA256SUMS.txt")"
    target="${package#"aixcoding-cli-$os-$arch-v"}"
    target="${target%-offline.tar.gz}"
    if [ -z "$package" ] || ! is_version "$target"; then
        say "That release on $(host_name "$host") has no package for $os ($arch)."
        return 1
    fi

    installed="$("$bin_dir/chrys" --version 2>/dev/null </dev/null || true)"
    # Without its `aixcoding-cli` alias the install is incomplete, and installing again restores it.
    if [ "$installed" = "$target" ] && [ -e "$bin_dir/aixcoding-cli" ]; then
        say "AIxCoding $target is already installed."
        return 0
    fi
    # A host that lags behind, such as GitCode right after a release, must not downgrade.
    if [ -z "$version" ] && is_version "$installed" && is_newer "$installed" "$target"; then
        say "AIxCoding $installed is already installed, which is newer than the latest on $(host_name "$host") ($target)."
        # The installed binary puts back an `aixcoding-cli` alias that went missing.
        if [ ! -e "$bin_dir/aixcoding-cli" ]; then
            "$bin_dir/chrys" install </dev/null || fail "chrys install failed"
        fi
        return 0
    fi

    say "Downloading AIxCoding $target for $os ($arch) from $(host_name "$host")..."
    fetch "$(release_url "$host" "v$target" "$package")" "$tmp/$package" || return 1
    expected="$(awk -v name="$package" '{ file = $2; sub(/^\*/, "", file) } file == name { print $1; exit }' "$tmp/SHA256SUMS.txt")"
    actual="$(cd "$tmp" && $hasher "$package" | awk '{ print $1 }')"
    if [ "$expected" != "$actual" ]; then
        say "The download of $package is damaged (its SHA-256 does not match SHA256SUMS.txt)."
        return 1
    fi

    # From here on a failure is not the download's, so another host would not help.
    if ! { mkdir "$tmp/package" && tar -xzf "$tmp/$package" -C "$tmp/package" && chmod +x "$tmp/package/aixcoding-cli"; }; then
        fail "could not unpack $package"
    fi
    # The binary unpacks its runtime on first run, then copies itself into ~/.local/bin. Its
    # input is this script when piped, so it gets none.
    status=0
    "$tmp/package/aixcoding-cli" install </dev/null || status=$?
    if [ "$status" = 126 ]; then
        fail "could not run the package from $tmp. If that folder does not allow running programs, set TMPDIR to another folder and run this script again."
    fi
    [ "$status" = 0 ] || fail "aixcoding-cli install failed"
}

main "$@"
