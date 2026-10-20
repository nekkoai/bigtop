#!/bin/sh
# Generate a flat apt repository; local runs may be unsigned. Publication must
# set APT_REQUIRE_SIGNATURE=1, then run validate-apt-repo.py on the full set.
# APT_KEY_DIR defaults to tracked public keys; never copy a private key here.
# APT_REPOSITORY_URL overrides the download URL in generated bigtop.sources.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
dist=$(CDPATH= cd -- "${1:-$here/build/dist}" && pwd)
# Resolve relative key paths before changing into DIST, but remove old metadata
# before checking whether the selected key directory actually exists.
keys=${APT_KEY_DIR:-$here/keys}
case $keys in /*) ;; *) keys=$PWD/$keys;; esac
cd "$dist"
# Remove old signatures even when key validation or signing subsequently fails.
rm -f Packages Packages.gz Release Release.gpg InRelease
case ${APT_REQUIRE_SIGNATURE:-0} in 0|1) ;; *) echo 'APT_REQUIRE_SIGNATURE must be 0 or 1' >&2; exit 1;; esac
fpr=$(python3 - "$here/ci/validate-apt-repo.py" "$keys" <<'PY'
from pathlib import Path
import runpy
import sys
try:
    print(runpy.run_path(sys.argv[1])["validate_key_material"](Path(sys.argv[2])))
except (ValueError, OSError) as error:
    sys.exit(str(error))
PY
)
if ! gpg --batch --list-secret-keys "$fpr" >/dev/null 2>&1; then
    if [ "${APT_REQUIRE_SIGNATURE:-0}" = 1 ]; then
        echo "Required signing key $fpr is unavailable" >&2
        exit 1
    fi
    echo "WARNING: secret key $fpr not available; apt repository left unsigned" >&2
    sign=0
else
    sign=1
fi
cp "$keys/bigtop-archive-keyring.gpg" "$keys/bigtop-archive-keyring.asc" .
python3 - "$here/ci/bigtop.sources" "${APT_REPOSITORY_URL:-}" <<'PY'
from pathlib import Path
import sys
from urllib.parse import urlsplit
source = Path(sys.argv[1]).read_text()
url = sys.argv[2]
if url:
    parsed = urlsplit(url)
    if (any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url)
            or parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        sys.exit("APT_REPOSITORY_URL must be an http(s) URL without whitespace, credentials, query or fragment")
    url = url.rstrip("/")
    source = "\n".join("URIs: " + url if line.startswith("URIs: ") else
                       "# Keyring: " + url + "/bigtop-archive-keyring.gpg" if line.startswith("# Keyring: ") else line
                       for line in source.splitlines()) + "\n"
Path("bigtop.sources").write_text(source)
PY
# Use a temporary file rather than a pipeline: POSIX sh cannot propagate a
# dpkg-scanpackages failure through sed, and must never sign a partial index.
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT HUP INT TERM
dpkg-scanpackages --multiversion . /dev/null > "$tmp"
sed 's|^Filename: \./|Filename: |' "$tmp" > Packages
gzip -9nk Packages
# Write outside DIST so Release never includes stale/self-referential checksums.
apt-ftparchive \
    -o APT::FTPArchive::Release::Origin=bigtop \
    -o APT::FTPArchive::Release::Label=bigtop \
    -o APT::FTPArchive::Release::Suite=stable \
    -o APT::FTPArchive::Release::Codename=noble \
    -o APT::FTPArchive::Release::Architectures="amd64 all" \
    -o APT::FTPArchive::Release::Description="Ainekko packages for Ubuntu 24.04 (nekkoai/bigtop)" \
    release . > "$tmp"
cat "$tmp" > Release
if [ "$sign" = 1 ]; then
    # On any signing failure leave neither a partial nor a stale signature pair.
    if ! gpg --batch --yes --local-user "$fpr" --digest-algo SHA256 --clearsign -o InRelease Release ||
       ! gpg --batch --yes --local-user "$fpr" --digest-algo SHA256 -abs -o Release.gpg Release; then
        rm -f InRelease Release.gpg
        exit 1
    fi
    echo "apt repository signed with $fpr"
fi
