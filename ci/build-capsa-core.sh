#!/bin/sh
# Build capsa-core from the exact capsa gitlink recorded in this repository's index.
set -eu

here=$(cd "$(dirname "$0")/.." && pwd)
toolchain_file=$here/pkg/capsa/rust-toolchain.toml
upstream=$here/upstream/capsa
work=$here/build/capsa-core

fail() {
    echo "build-capsa-core: $*" >&2
    exit 1
}

[ -r /etc/os-release ] || fail "missing /etc/os-release"
. /etc/os-release
[ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 24.04 ] ||
    fail "only Ubuntu 24.04 builders are supported"
[ "$(dpkg --print-architecture)" = amd64 ] || fail "only amd64 builders are supported"
[ -f "$toolchain_file" ] || fail "missing $toolchain_file"
toolchain=$(python3 - "$toolchain_file" <<'PY'
import sys
import tomllib

print(tomllib.load(open(sys.argv[1], "rb"))["toolchain"]["channel"])
PY
) || fail "cannot read the Rust toolchain file"
printf '%s\n' "$toolchain" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' >/dev/null ||
    fail "toolchain file must pin an exact x.y.z Rust release"
grep -Fx 'profile = "minimal"' "$toolchain_file" >/dev/null ||
    fail "toolchain file must select the minimal profile"
command -v rustup >/dev/null 2>&1 || fail "rustup with Rust $toolchain is required"
rustup toolchain list | awk '{print $1}' |
    grep -F -x -e "$toolchain" -e "$toolchain-x86_64-unknown-linux-gnu" >/dev/null ||
    fail "Rust $toolchain is not installed; install it before source preparation"
[ "$(rustup run "$toolchain" rustc --version | awk '{print $2}')" = "$toolchain" ] ||
    fail "rustup did not select Rust $toolchain"
cargo_version=$(rustup run "$toolchain" cargo --version) ||
    fail "Cargo from Rust $toolchain is unavailable"
echo "build-capsa-core: Rust $toolchain; $cargo_version"

pin=$(git -C "$here" ls-files --stage -- upstream/capsa |
    awk '$1 == "160000" { print $2; exit }')
[ -n "$pin" ] || fail "upstream/capsa is not a gitlink in the index"
git -C "$upstream" cat-file -e "$pin^{commit}" ||
    fail "gitlink commit $pin is unavailable in $upstream"
debversion=$(dpkg-parsechangelog -l "$here/pkg/capsa/debian/changelog" -S Version)
version=${debversion%-*}
[ "$version" != "$debversion" ] || fail "changelog version lacks a Debian revision"

rm -rf "$work"
mkdir -p "$work"
src=$work/capsa-$version
git -C "$upstream" archive --format=tar --prefix="capsa-$version/" "$pin" |
    tar -C "$work" -xf -
install -m 0644 "$toolchain_file" "$src/rust-toolchain.toml"

# Source preparation is intentionally network-enabled.  The resulting orig
# tarball contains Cargo's locked vendor tree and its offline source config.
mkdir -p "$src/.cargo"
(cd "$src" && rustup run "$toolchain" cargo vendor --locked vendor > .cargo/config.toml)
git -C "$upstream" show "$pin:Cargo.lock" | cmp -s - "$src/Cargo.lock" ||
    fail "cargo vendor changed Cargo.lock"

tar -C "$work" -cJf "$work/capsa_$version.orig.tar.xz" "capsa-$version"
cp -a "$here/pkg/capsa/debian" "$src/"
(cd "$src" && dpkg-buildpackage -us -uc -b ${DPKG_BUILDPACKAGE_OPTS:-})
ls -l "$work"/*.deb "$work"/*.buildinfo
