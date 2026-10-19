# bigtop

Ainekko's packaging and deployment repository. The first supported package
set is [asys](https://github.com/glguida/asys) and
[dcomp](https://github.com/glguida/dcomp) on Ubuntu 24.04 (noble, amd64),
imported with history from `gh-ainekko/asys-debian`. Capsa and other artifacts
can be added without introducing another packaging repository.

Upstream sources are pinned as git submodules under `upstream/`; GitHub
Actions builds, smoke-tests and (on `v*` tags) releases the `.deb`s.
Historical asys design notes: [PLAN.md](PLAN.md).

| Package | Built by | Contents |
|---|---|---|
| `dcomp` | `ci/build-dcomp.sh` | `dcomp`, `dcomp-proxy`, `dcomp-healthcheck` |
| `asys` | `ci/build-asys.sh` | all host commands, Python, vendored TUI deps, skills, designs, examples |
| `asys-images` | `ci/build-images.sh` (needs Docker + buildx) | container images of the same build ID plus `release.json`; loaded by postinst / `asys-images-load` |
| `asys-flow-view` | `ci/build-asys.sh` | X11 workflow viewer/editor |
| `asys-server` | `ci/build-asys.sh` | shared team instance under `/srv/asys` (`asys init /srv/asys/host --group asys`, as upstream documents) |

## Layout

```
pkg/
  asys/debian/                 source packaging: asys, asys-server, asys-flow-view
  asys-images/debian/          image bundle's control template, hooks and loader
  dcomp/debian/                source packaging: dcomp
upstream/asys, upstream/dcomp  pinned upstream releases (submodules)
keys/                         apt repository signing public key
ci/                           shared build, test and publication scripts
tests/                        lightweight packaging-path regression tests
.github/workflows/build.yml   build -> smoke test -> apt metadata -> release on tag
```

`pkg/<name>/` is a **packaging unit**, not necessarily one binary package.
Keep related binary packages produced by one source build together; do not
split `asys-server` and `asys-flow-view` out of `pkg/asys/debian`.
Format-specific recipes live below the unit (`debian/` today; other formats
can be added later).

`asys-images` is a separate unit because it has a distinct Docker build/save
pipeline. Its `debian/` directory is input to `ci/build-images.sh` and
`dpkg-deb`, **not** a standalone debhelper source package. It deliberately
shares `upstream/asys` and reads its version from
`pkg/asys/debian/changelog`; it must not acquire a separate version or source
pin. The Dockerfiles remain upstream, while the image-bundle packaging lives
here.

Future packages (for example `pkg/capsa/debian/`) should follow this convention
and be wired into the build/test pipeline explicitly. Non-package deployment
recipes can live under `deploy/<product>/` when needed; install-time service
units and hooks stay with the packages that ship them. No placeholder
packages or deployment framework are introduced in this first migration.

## Build locally

```sh
git clone --recursive https://github.com/gh-ainekko/bigtop.git
cd bigtop
ci/build-all.sh                 # -> build/dist/*.deb
sudo ci/smoke-test.sh           # installs them and runs the team workflow as user "smoke"
```

Needs Go ≥ 1.25 on PATH, `debhelper`, `dh-python`, `devscripts`, `fakeroot`,
`python3-pip`, `zstd`, the X11 dev packages from `pkg/asys/debian/control`, Docker
with buildx. `asys` and `asys-images` carry the same upstream **build ID**
(`tools/build-id`, e.g. `0.2.2-7fd29b7`): the `asys` commands only use images
named in `/usr/share/asys/release.json`, which `asys-images` ships together with
those exact images, so the two must come from one `ci/build-all.sh` run. The scripts default to the submodules; pass
another checkout and revision to build something else
(`ci/build-asys.sh ~/asys HEAD`). Package versions come from
`pkg/*/debian/changelog` (`dch -i`); image tags use the upstream build ID.

For fast layout regression tests without building upstream software or
running Docker:

```sh
python3 -m unittest discover -s tests -v
```

These use temporary fixture sources and stub the expensive build commands.
They require `git`, `dpkg-dev`, `fakeroot`, `zstd` and Python 3, and do not
replace the full build and installation smoke tests.

## Updating upstream

```sh
git -C upstream/dcomp fetch --tags && git -C upstream/dcomp checkout vX.Y.Z
git -C upstream/asys  fetch --tags && git -C upstream/asys  checkout <release commit>
dch -c pkg/dcomp/debian/changelog -v X.Y.Z-1 -D noble "New upstream release."
dch -c pkg/asys/debian/changelog  -v A.B.C-1 -D noble "New upstream release."
git add -A upstream pkg && git commit
# After reviewing and validating the build:
git tag vA.B.C-1
git push origin HEAD
git push origin vA.B.C-1
```

Versions must not contain `~` (GitHub rewrites it in asset names). Releases
are cumulative per tag: each release carries all five binary packages.

## Install from the apt repository

The release workflow publishes a flat apt repository at
`https://github.com/gh-ainekko/bigtop/releases/latest/download/`
(signing key `keys/asys-archive-keyring.gpg`, fingerprint in `keys/FINGERPRINT`;
CI signs with the `APT_GPG_PRIVATE_KEY` repository secret).

**Before the first bigtop release:** configure the matching signing secret
in this repository and validate a full CI build. The imported public key does
not include the private key. Without it the existing scripts produce unsigned
metadata, which the installation commands below will not trust. These URLs
become usable only after a release is published.

This migration retains the existing `asys.sources`, `asys-archive-keyring.*`
filenames and apt Origin/Label to avoid combining a layout refactor with a
key or repository-identity migration. Existing `asys-debian` installations are
not redirected automatically; switch their source only after validating a
bigtop release.

```sh
sudo curl -fsSL https://github.com/gh-ainekko/bigtop/releases/latest/download/asys-archive-keyring.gpg -o /usr/share/keyrings/asys-archive-keyring.gpg
sudo curl -fsSL https://github.com/gh-ainekko/bigtop/releases/latest/download/asys.sources -o /etc/apt/sources.list.d/asys.sources
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
