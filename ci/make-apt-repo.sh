#!/bin/sh
# Turn a directory of .debs into a flat, signed apt repository (in place).
# Usage: ci/make-apt-repo.sh [DIST_DIR]
# Signs with the key in keys/FINGERPRINT if its secret key is in the GnuPG
# keyring (CI imports it from the APT_GPG_PRIVATE_KEY secret); otherwise the
# repository is left unsigned and only usable with [trusted=yes].
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
dist=$(cd "${1:-$here/build/dist}" && pwd)
fpr=$(cat "$here/keys/FINGERPRINT")
cd "$dist"
rm -f Packages Packages.gz Release Release.gpg InRelease
dpkg-scanpackages --multiversion . /dev/null | sed 's|^Filename: \./|Filename: |' > Packages
gzip -9nk Packages
apt-ftparchive \
    -o APT::FTPArchive::Release::Origin=asys \
    -o APT::FTPArchive::Release::Label=asys \
    -o APT::FTPArchive::Release::Suite=stable \
    -o APT::FTPArchive::Release::Codename=noble \
    -o APT::FTPArchive::Release::Architectures="amd64 all" \
    -o APT::FTPArchive::Release::Description="asys and dcomp packages for Ubuntu 24.04 (gh-ainekko/asys-debian)" \
    release . > Release
if gpg --batch --list-secret-keys "$fpr" >/dev/null 2>&1; then
    gpg --batch --yes --local-user "$fpr" --digest-algo SHA256 --clearsign -o InRelease Release
    gpg --batch --yes --local-user "$fpr" --digest-algo SHA256 -abs -o Release.gpg Release
    echo "apt repository signed with $fpr"
else
    echo "WARNING: secret key $fpr not available; apt repository left unsigned" >&2
fi
cp "$here/keys/asys-archive-keyring.gpg" "$here/keys/asys-archive-keyring.asc" .
cp "$here/ci/asys.sources" .
ls -l
