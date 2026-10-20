#!/bin/sh
# Real apt update + dependency-resolution/download test, with entirely private
# apt config/lists/cache and a temporary localhost server. No root or install.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec python3 "$here/ci/apt-repo-test.py" "$@"
