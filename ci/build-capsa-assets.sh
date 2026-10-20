#!/bin/sh
# Fetch, verify and package the pinned Nomic GGUF and offline tokenizer cache.
set -eu

here=$(cd "$(dirname "$0")/.." && pwd)
recipe=$here/pkg/capsa/embedding-assets-nomic
upgrade_guard=$here/pkg/capsa/upgrade-guard
inputs=$recipe/inputs.json
work=$here/build/capsa-assets
cache=$here/build/capsa-inputs

fail() {
    echo "build-capsa-assets: $*" >&2
    exit 1
}

[ -r /etc/os-release ] || fail "missing /etc/os-release"
. /etc/os-release
[ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 24.04 ] ||
    fail "only Ubuntu 24.04 builders are supported"
[ "$(dpkg --print-architecture)" = amd64 ] || fail "only amd64 builders are supported"
[ -f "$upgrade_guard" ] || fail "missing shared Capsa upgrade guard: $upgrade_guard"
for command in curl dpkg-buildpackage dpkg-parsechangelog python3 sha256sum; do
    command -v "$command" >/dev/null 2>&1 || fail "missing build command: $command"
done
[ -f "$inputs" ] || fail "missing curated input manifest: $inputs"

# Print exact, immutable artifact data from the package-owned input record.
set -- $(python3 - "$inputs" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    record = json.load(f)
for name in ("model", "tokenizer"):
    artifact = record[name]
    print(artifact["filename"], artifact["url"], artifact["sha256"], artifact["bytes"])
PY
)
model_name=$1 model_url=$2 model_sha=$3 model_bytes=$4
tokenizer_name=$5 tokenizer_url=$6 tokenizer_sha=$7 tokenizer_bytes=$8

verify() {
    artifact=$1 sha=$2 bytes=$3
    [ -f "$artifact" ] || return 1
    [ "$(wc -c < "$artifact")" = "$bytes" ] || return 1
    [ "$(sha256sum "$artifact" | awk '{print $1}')" = "$sha" ]
}
fetch() {
    destination=$1 url=$2 sha=$3 bytes=$4
    if verify "$destination" "$sha" "$bytes"; then
        return
    fi
    rm -f "$destination" "$destination.partial"
    curl --fail --location --retry 3 --output "$destination.partial" "$url"
    verify "$destination.partial" "$sha" "$bytes" || {
        rm -f "$destination.partial"
        fail "download did not match pinned size and SHA-256: $url"
    }
    mv "$destination.partial" "$destination"
}

mkdir -p "$cache"
fetch "$cache/$model_name" "$model_url" "$model_sha" "$model_bytes"
fetch "$cache/$tokenizer_name" "$tokenizer_url" "$tokenizer_sha" "$tokenizer_bytes"

debversion=$(dpkg-parsechangelog -l "$here/pkg/capsa/debian/changelog" -S Version)
version=${debversion%-*}
[ "$version" != "$debversion" ] || fail "changelog version lacks a Debian revision"

rm -rf "$work"
mkdir -p "$work"
src=$work/capsa-embedding-assets-nomic-$version
mkdir -p "$src"
cp -a "$recipe/debian" "$src/"
python3 - "$src/debian/changelog" "$debversion" <<'PY'
import re
import sys

path, version = sys.argv[1:]
text = open(path, encoding="utf-8").read()
updated, count = re.subn(
    r"^([^(]+\()[^)]+(\)\s+\S+\s*;)",
    lambda match: match.group(1) + version + match.group(2),
    text,
    count=1,
)
if count != 1:
    raise SystemExit("cannot replace companion changelog version")
open(path, "w", encoding="utf-8").write(updated)
PY
install -D -m 0755 "$upgrade_guard" "$src/debian/capsa-embedding-assets-nomic.preinst"
mkdir -p "$src/debian/assets"
cp "$inputs" "$src/debian/assets/manifest.json"
cp "$cache/$model_name" "$src/debian/assets/$model_name"
cp "$cache/$tokenizer_name" "$src/debian/assets/$tokenizer_name"

# The manifest declares Apache-2.0.  Package the public license text without
# turning a model-card/license retrieval problem into an artifact audit gate.
cp "$recipe/LICENSE-APACHE-2.0" "$src/debian/assets/MODEL-LICENSE.txt"
printf '%s\n' \
    'Nomic embedding assets are declared Apache-2.0 by the pinned input record.' \
    'The Apache-2.0 text is included in MODEL-LICENSE.txt.' \
    > "$src/debian/assets/MODEL-LICENSE-NOTICE.txt"

tar -C "$work" -cJf "$work/capsa-embedding-assets-nomic_$version.orig.tar.xz" \
    "capsa-embedding-assets-nomic-$version"
(cd "$src" && dpkg-buildpackage -us -uc -b ${DPKG_BUILDPACKAGE_OPTS:-})
ls -l "$work"/*.deb "$work"/*.buildinfo
