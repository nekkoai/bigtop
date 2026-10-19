#!/bin/sh
# Build the asys-images .deb: docker build upstream images under the shared
# build ID, save them with their release.json, compress, wrap with dpkg-deb.
# Needs Docker (with buildx) and network.
# Usage: ci/build-images.sh [UPSTREAM_DIR] [--no-build]
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
upstream=$(cd "${1:-$here/upstream/asys}" && pwd)
# A companion artifact of asys, always versioned with its host packages.
debversion=$(dpkg-parsechangelog -l "$here/pkg/asys/debian/changelog" -S Version)
upstream_version=${debversion%%-*}
build=$("$upstream/tools/build-id")
work=$here/build/asys-images
rm -rf "$work"; mkdir -p "$work"
if [ "${2:-}" != --no-build ]; then
    (cd "$upstream" && make build ASYS_BUILD="$build")
fi
release=$upstream/release.json
test -f "$release"
refs=$(python3 -c 'import json,sys; print(" ".join(e["ref"] for e in json.load(open(sys.argv[1]))["images"].values()))' "$release")
pkg=$work/asys-images_${debversion}_amd64
mkdir -p "$pkg/DEBIAN" "$pkg/usr/share/asys/images" "$pkg/usr/sbin"
docker save $refs | zstd -T0 -19 -q -o "$pkg/usr/share/asys/images/asys-images.tar.zst"
# Build-host record; asys-images-load rewrites it with this engine's image IDs
# into /usr/share/asys/release.json (IDs differ between Docker image stores).
install -m 0644 "$release" "$pkg/usr/share/asys/images/release.json"
install -m 0755 "$here/pkg/asys/images/debian/asys-images-load" "$pkg/usr/sbin/"
install -m 0755 "$here/pkg/asys/images/debian/postinst" "$here/pkg/asys/images/debian/postrm" "$pkg/DEBIAN/"
sed -e "s/@VERSION@/$debversion/" -e "s/@UPSTREAM@/$upstream_version/" -e "s/@BUILD@/$build/" "$here/pkg/asys/images/debian/control.in" > "$pkg/DEBIAN/control"
size=$(du -sk "$pkg/usr" | cut -f1); echo "Installed-Size: $size" >> "$pkg/DEBIAN/control"
(cd "$pkg" && find usr -type f -exec md5sum {} + > DEBIAN/md5sums)
fakeroot dpkg-deb -Zzstd -z0 --build "$pkg" "$work/" >/dev/null 2>&1 || fakeroot dpkg-deb -Znone --build "$pkg" "$work/"
rm -rf "$pkg"
echo "asys-images for build $build: $refs"
ls -l "$work"/*.deb
