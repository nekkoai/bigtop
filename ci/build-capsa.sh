#!/bin/sh
# Run in an isolated Ubuntu 24.04 amd64 builder, never on a serving host.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
"$here/ci/build-capsa-core.sh"
"$here/ci/build-capsa-runtime.sh"
"$here/ci/build-capsa-llama.sh"
"$here/ci/build-capsa-assets.sh"
mkdir -p "$here/build/capsa-dist"
for component in core runtime llama assets; do
    cp "$here/build/capsa-$component/"*.deb "$here/build/capsa-dist/"
done
ls -lh "$here/build/capsa-dist/"*.deb
