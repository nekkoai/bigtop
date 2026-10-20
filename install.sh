#!/usr/bin/env bash
# Configure the Bigtop apt repository; application packages are installed separately.
set -euo pipefail

. /etc/os-release
if [[ ${ID:-} != ubuntu || ${VERSION_ID:-} != 24.04 ]] ||
   [[ $(dpkg --print-architecture) != amd64 ]]; then
    echo "Bigtop requires Ubuntu 24.04 (amd64)." >&2
    exit 1
fi
for command in curl gpg; do
    if ! command -v "$command" >/dev/null; then
        echo "Install prerequisites first: sudo apt-get install ca-certificates curl gnupg" >&2
        exit 1
    fi
done
root=()
if [[ $(id -u) != 0 ]]; then
    root=(sudo)
    sudo -v
fi

base=https://github.com/nekkoai/bigtop/releases/latest/download
expected=5F2A148DC8A12E64DAE910629A009F44A53FBFAB
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
curl -fsSL --retry 3 "$base/bigtop-archive-keyring.gpg" -o "$work/keyring.gpg"
fingerprint=$(gpg --batch --no-options --homedir "$work" --with-colons \
    --show-keys "$work/keyring.gpg" |
    awk -F: '$1 == "pub" { count++ }
             $1 == "fpr" && !fingerprint { fingerprint = $10 }
             END { if (count != 1) exit 1; print fingerprint }')
if [[ $fingerprint != "$expected" ]]; then
    echo "Bigtop signing-key fingerprint mismatch; repository not configured." >&2
    exit 1
fi

cat > "$work/bigtop.sources" <<EOF
# /etc/apt/sources.list.d/bigtop.sources
# Keyring: $base/bigtop-archive-keyring.gpg
Types: deb
URIs: $base
Suites: ./
Signed-By: /usr/share/keyrings/bigtop-archive-keyring.gpg
EOF
"${root[@]}" install -d -m 0755 /usr/share/keyrings /etc/apt/sources.list.d
"${root[@]}" install -m 0644 "$work/keyring.gpg" /usr/share/keyrings/bigtop-archive-keyring.gpg
"${root[@]}" install -m 0644 "$work/bigtop.sources" /etc/apt/sources.list.d/bigtop.sources
"${root[@]}" apt-get update -o APT::Update::Error-Mode=any
echo "Bigtop repository configured. Install the packages you need with apt install."
