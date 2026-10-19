#!/bin/sh
# Build every package; needs Go >= 1.25, debhelper, dh-python, pip, zstd, fakeroot, Docker.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
"$here/ci/build-dcomp.sh"
"$here/ci/build-asys.sh"
"$here/ci/build-images.sh"
rm -rf "$here/build/dist"; mkdir -p "$here/build/dist"
cp "$here"/build/dcomp/*.deb "$here"/build/asys/*.deb "$here"/build/asys-images/*.deb "$here/build/dist/"
ls -l "$here/build/dist"
