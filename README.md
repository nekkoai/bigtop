# asys-debian

Self-contained Debian packaging for [asys](https://github.com/glguida/asys)
and [dcomp](https://github.com/glguida/dcomp) on Ubuntu 24.04 (noble, amd64).
Upstream sources are pinned as git submodules under `upstream/`; GitHub
Actions builds, smoke-tests and (on `v*` tags) releases the `.deb`s.
Design notes: [PLAN.md](PLAN.md).

| Package | Built by | Contents |
|---|---|---|
| `dcomp` | `ci/build-dcomp.sh` | `dcomp`, `dcomp-proxy`, `dcomp-healthcheck` |
| `asys` | `ci/build-asys.sh` | all host commands, Python, vendored TUI deps, skills, designs, examples |
| `asys-images` | `ci/build-images.sh` (needs Docker + buildx) | container images of the same build ID plus `release.json`; loaded by postinst / `asys-images-load` |
| `asys-flow-view` | `ci/build-asys.sh` | X11 workflow viewer/editor |
| `asys-server` | `ci/build-asys.sh` | shared team instance under `/srv/asys` (`asys init /srv/asys/host --group asys`, as upstream documents) |

## Layout

```
upstream/dcomp, upstream/asys   pinned upstream releases (submodules)
dcomp/debian, asys/debian       Debian packaging (asys/debian also builds asys-server)
asys-images/                    control template + load script for the image package
keys/                           apt repository signing public key
ci/                             build-*.sh, build-all.sh, smoke-test.sh, make-apt-repo.sh, apt-repo-test.sh
.github/workflows/build.yml     CI: build -> smoke test -> apt metadata -> artifacts -> release on tag
```

## Build locally

```sh
git clone --recursive <this repo> && cd asys-debian
ci/build-all.sh                 # -> build/dist/*.deb
sudo ci/smoke-test.sh           # installs them and runs the team workflow as user "smoke"
```

Needs Go ≥ 1.25 on PATH, `debhelper`, `dh-python`, `devscripts`, `fakeroot`,
`python3-pip`, `zstd`, the X11 dev packages from `asys/debian/control`, Docker
with buildx. `asys` and `asys-images` carry the same upstream **build ID**
(`tools/build-id`, e.g. `0.2.2-7fd29b7`): the `asys` commands only use images
named in `/usr/share/asys/release.json`, which `asys-images` ships together with
those exact images, so the two must come from one `ci/build-all.sh` run. The scripts default to the submodules; pass
another checkout and revision to build something else
(`ci/build-asys.sh ~/asys HEAD`). Package versions come from
`*/debian/changelog` (`dch -i`), image tags from the upstream part of the
asys version.

## Updating upstream

```sh
git -C upstream/dcomp fetch --tags && git -C upstream/dcomp checkout vX.Y.Z
git -C upstream/asys  fetch --tags && git -C upstream/asys  checkout <release commit>
dch -c dcomp/debian/changelog -v X.Y.Z-1~noble1 -D noble "New upstream release."
dch -c asys/debian/changelog  -v A.B.C-1~noble1 -D noble "New upstream release."
git add -A upstream dcomp asys && git commit && git tag vA.B.C-1 && git push --tags
```

Versions must not contain `~` (GitHub rewrites it in asset names). Releases
are cumulative per tag: each release carries all four packages.

```
```

## Install from the apt repository

Every `v*` tag publishes a GitHub Release whose assets form a flat, signed apt
repository at `https://github.com/gh-ainekko/asys-debian/releases/latest/download/`
(signing key `keys/asys-archive-keyring.gpg`, fingerprint in `keys/FINGERPRINT`;
CI signs with the `APT_GPG_PRIVATE_KEY` repository secret).

```sh
sudo curl -fsSL https://github.com/gh-ainekko/asys-debian/releases/latest/download/asys-archive-keyring.gpg -o /usr/share/keyrings/asys-archive-keyring.gpg
sudo curl -fsSL https://github.com/gh-ainekko/asys-debian/releases/latest/download/asys.sources -o /etc/apt/sources.list.d/asys.sources
sudo apt update && sudo apt install asys-server
```

`apt upgrade` then follows new releases (`releases/latest` always points at
the newest non-prerelease). Pin a specific release by replacing `latest/download`
with `download/vX.Y.Z-N` in `asys.sources`.

## Team server

```sh
sudo apt install asys-server                 # or the local .debs: sudo apt install ./dcomp_*.deb ./asys-images_*.deb ./asys_*.deb ./asys-server_*.deb
sudo asys-adduser alice bob            # group asys + docker; re-login
source /srv/asys/asys-env              # in every shell that uses asys
asys-inference gateway login PROVIDER --as ACCOUNT && asys-inference start   # once
asys system-model set simple MODEL
```

`asys-skills --codex|--claude|DIR [--force]` exports the bundled skills for
agents. The dashboard runs as `asys-dashboard.service` on 127.0.0.1:8766; front
it with `/usr/share/asys-server/nginx-asys-dashboard.conf`. Upgrading
`asys-server` runs `asys update` only when no run is active (0.2.2's update
drains running workflows first, which could take hours); otherwise it tells you
to run `sudo asys-server-update --update` later or `asys-run --upgrade RUN`. Shared git checkouts under
`/srv/asys/workspaces` need a per-member
`git config --global --add safe.directory /srv/asys/workspaces/NAME`.
