# bigtop

## TL;DR

On Ubuntu 24.04 (amd64), configure the Bigtop apt repository:

```sh
curl -fsSL https://raw.githubusercontent.com/nekkoai/bigtop/main/install.sh | bash -x
```

Requires `curl`, GnuPG and CA certificates, plus root or `sudo` access. The script
verifies the pinned signing key, installs the apt source and refreshes package
metadata. It does not install application packages. Then choose your packages,
for example `sudo apt install asys-server` or `sudo apt install capsa-server`
(Capsa requires explicit configuration; see below).

## Overview

Ainekko's packaging and deployment repository for Ubuntu 24.04 (noble, amd64).
It packages [asys](https://github.com/glguida/asys) and
[dcomp](https://github.com/glguida/dcomp).
[Capsa packaging](pkg/capsa/README.md) provides a separate experimental
eight-package serving/indexing set for Ubuntu 24.04 amd64.

Upstream sources are pinned as git submodules under `upstream/`. Every push to
`main` is automatically tagged, built and tested by GitHub Actions. Successful
runs publish a complete signed apt repository as assets of that tag.
Contributor and agent guidance: [AGENTS.md](AGENTS.md).

| Package | Built by | Contents |
|---|---|---|
| `dcomp` | `ci/build-dcomp.sh` | `dcomp`, `dcomp-proxy`, `dcomp-healthcheck` |
| `asys` | `ci/build-asys.sh` | all host commands, Python, vendored TUI deps, skills, designs, examples |
| `asys-images` | `ci/build-images.sh` (needs Docker + buildx) | container images of the same build ID plus `release.json`; loaded by postinst / `asys-images-load` |
| `asys-flow-view` | `ci/build-asys.sh` | X11 workflow viewer/editor |
| `asys-server` | `ci/build-asys.sh` | shared team instance under `/srv/asys` (`asys init /srv/asys/host --group asys`, as upstream documents) |

Releases contain these five packages **and all eight Capsa packages** (13 total).
Capsa remains experimental: its integration test uses synthetic data, not existing
production indexes. Build it locally with `ci/build-capsa.sh` (output:
`build/capsa-dist/`). See its README for package selection, explicit snapshot
activation and compatibility caveats.

## Layout

```
pkg/
  asys/
    debian/                   source packaging: asys, asys-server, asys-flow-view
    images/debian/            asys-images control template, hooks and loader
  dcomp/debian/                source packaging: dcomp
  capsa/debian/                core, federation, indexers, services, server
  capsa/python-runtime/        private CPython and locked production dependencies
  capsa/llama-cpu/debian/      CPU server and matching private libraries
  capsa/embedding-assets-nomic/debian/  model and tokenizer assets
upstream/asys, upstream/dcomp  pinned upstream releases (submodules)
upstream/capsa                 pinned combined Capsa source
upstream/llama.cpp             pinned llama release from the aifoundry fork
keys/                         apt repository signing public key
ci/                           shared build, test and publication scripts
tests/                        lightweight packaging-path regression tests
.github/workflows/build.yml   main-push tag -> parallel builds -> tests -> apt -> release
.github/workflows/build-package.yml  reusable independent source-build job
.github/workflows/capsa-core.yml     eight-package Capsa integration test
```

`pkg/<project>/` groups packaging for one **software project**, not necessarily
one binary package. Companion artifacts live beneath their owning project.
Keep related binary packages produced by one source build together; do not
split `asys-server` and `asys-flow-view` out of `pkg/asys/debian`.
Format-specific recipes live below the project or companion artifact
(`debian/` today; other formats can be added later).

`asys-images` is a companion artifact under `pkg/asys/images/`, with a distinct
Docker build/save pipeline. Its `debian/` directory is input to
`ci/build-images.sh` and `dpkg-deb`, **not** a standalone debhelper source
package. It deliberately shares `upstream/asys` and reads its version from
`pkg/asys/debian/changelog`; it must not acquire a separate version or source
pin. The Dockerfiles remain upstream, while the image-bundle packaging lives
here.

Additional projects should follow this convention
and be wired into the build/test pipeline explicitly. Non-package deployment
recipes can live under `deploy/<product>/` when needed; install-time service
units and hooks stay with the packages that ship them. No placeholder
packages or deployment framework are provided.

## Packaging principles

- **Keep upstream code upstream.** This repository owns packaging, deployment
  integration and release orchestration. Pin upstream sources rather than
  copying application implementations into `pkg/`.
- **Build related artifacts together.** The asys host packages and image bundle
  must use the same source revision, package version and upstream build ID
  (`tools/build-id`). Locally, use one `ci/build-all.sh` run for that set.
  Parallel CI builds use the same packaging commit and source pins, exchange
  checksummed build records, and verify installed native host and target-engine
  image build identities. Matching version strings alone are insufficient.
- **Resolve image identity on the target engine.** The image bundle includes a
  build-host release record. `asys-images-load` loads the images and regenerates
  `/usr/share/asys/release.json` with their local Docker IDs; those IDs are not
  portable between Docker image stores.
- **Vendor host dependencies at build time.** The build scripts vendor Go
  modules and private host Python dependencies into the prepared source tree.
  Package installation uses those bundled files, not pip/npm installs into host
  system directories. Container builds have separate network requirements.
- **Preserve installed state and active work.** Keep host tooling separate from
  `asys-server`'s user/group, service and shared-state setup. Maintainer scripts
  should be idempotent, retain user data and defer shared-service updates while
  runs are active. Do not automatically prune images used by running work.

## Build locally

Run full builds in a disposable Ubuntu 24.04 builder with its **own Docker
daemon**: builds can replace existing image tags, even without installing a
package. Installation and apt tests additionally create users, start services
and modify apt configuration; do not run them on a working deployment.

```sh
git clone --recursive https://github.com/nekkoai/bigtop.git
cd bigtop
ci/build-all.sh                 # local asys/dcomp set -> build/dist/*.deb
```

Needs Go ≥ 1.25 on PATH, `debhelper`, `dh-python`, `devscripts`, `fakeroot`,
`python3-pip`, `zstd`, the X11 dev packages from `pkg/asys/debian/control`, Docker
with buildx. See `.github/workflows/build-package.yml` for CI toolchains and
dependency installation. The scripts invoke `dpkg-buildpackage` and `dpkg-deb`
directly; they do not provision an `sbuild` chroot.

Scripts default to the pinned submodules; pass another checkout and revision
to build something else (`ci/build-asys.sh ~/asys HEAD`). Keep the image build
on the same revision when using these overrides. Package versions come from
`pkg/*/debian/changelog` (`dch -i`); image tags use the upstream build ID.

## Testing

For fast layout regression tests without building upstream software or
running Docker:

```sh
python3 -m unittest discover -s tests -v
```

These use temporary fixture sources and stub the expensive build commands.
They require `git`, `dpkg-dev`, `fakeroot`, `zstd` and Python 3.11+. Debhelper/make,
`apt-utils`, GnuPG and `python3-yaml` enable additional vendor-cleanup, signed apt
and workflow-graph tests. Signature tests use disposable keys; GitHub publication
tests use an offline mock API. These do not replace full builds and smoke tests.

After building, run the integration checks **only on a disposable test host**.
It needs systemd, its own Docker daemon with buildx, and the test/publication
tools used by CI (`curl`, Python 3, `dpkg-dev`, `apt-utils`, GnuPG):

```sh
sudo ci/smoke-test.sh build/dist
ci/make-apt-repo.sh build/dist
ci/apt-repo-test.sh build/dist
```

The asys smoke test installs all five packages, checks the dashboard, runs the
Hello workflow as a second user and exports skills. Capsa has its own separate
eight-package VM test. The apt test verifies signatures when present; otherwise
it explicitly tests an unsigned repository. It downloads every indexed package
at its exact version and checks payload hashes, without installing packages or
modifying system apt configuration. Use disposable signing keys for tests, not
a production private key.

For packaging changes, also test reinstall/upgrade behavior and preservation of
existing run records and configuration. The server's update hook may activate
shared services, so runtime-state files need not remain byte-identical. Run
workflows from the shared workspace as the test member, not another user's home.
When comparing builds, distinguish application changes from debug metadata,
timestamps and generated caches; successful smoke tests alone do not establish
bit-for-bit reproducibility.

## Updating upstream

```sh
git -C upstream/dcomp fetch --tags && git -C upstream/dcomp checkout vX.Y.Z
git -C upstream/asys  fetch --tags && git -C upstream/asys  checkout <release commit>
dch -c pkg/dcomp/debian/changelog -v X.Y.Z-1 -D noble "New upstream release."
dch -c pkg/asys/debian/changelog  -v A.B.C-1 -D noble "New upstream release."
git add -A upstream pkg && git commit
# Review/merge to main, then push. Actions creates the release tag.
git push origin main
```

Versions must not contain `~` (GitHub rewrites it in asset names). Releases
are cumulative per tag: each release carries all thirteen binary packages.

## Automatic releases and parallel CI

`.github/workflows/build.yml` runs on **every main push**, without path filters.
Pull requests and manual dispatches run the same build/test graph but never tag,
receive the signing secret or publish. Manually pushing a tag does not trigger
another build. Publication happens in the main-push run itself; it does not rely
on `GITHUB_TOKEN` tag pushes triggering a second workflow.

The graph is:

```
identity -> tag (main pushes only)
          -> seven independent build jobs
             dcomp ───────┐
             asys ────────┼-> asys integration ──┐
             asys-images ─┘                      │
             capsa-core ─────┐                   ├-> complete apt repository
             capsa-runtime ──┤                   │   -> sign + apt download test
             capsa-llama ────┼-> Capsa integration┘   -> draft assets -> publish
             capsa-assets ──┘
fixture/protocol tests ───────────────────────────┘
```

Each build job has its own disposable Ubuntu 24.04 runner/Docker daemon.
Source builds that emit multiple binaries stay together: `asys` emits three
packages and `capsa-core` emits five. The two integration jobs install artifacts
on fresh runners, not on the builders. The final repository is gated by **every
build, both integration tests and the regression suite**. Missing, duplicate,
mixed-commit/pin, wrong-version/architecture or tampered artifacts fail assembly.
The asys host/image build IDs must also agree; `asys-images-load` still
regenerates Docker image IDs on the target engine.

Repository tags follow Shelley's style:
`v0.<full-history-commit-count>.9<octal-of-first-12-SHA-hex-digits>`.
They name the packaging commit, not an upstream application version. A failed
build leaves its tag but no published release. Existing tags are reused only
when they point to exactly that commit; tags are never force-moved.

To make `apt upgrade` notice packaging-only changes, CI appends
`.bigtop.<count>.g<12-char-SHA>` to each primary **Debian revision** in its
workspace. For example, `0.2.2-3` becomes `0.2.2-3.bigtop.123.g0123456789ab`.
Companions inherit their family's exact version. No changelog edits, upstream
version changes or pin changes are committed by CI. Keep `main` fast-forward
and do not decrease package changelog versions: commit counts are not monotonic
across force-pushes or history rewrites.

The `apt-repo` Actions artifact is an unsigned preview, including on PRs.
Only the main-push publication job imports `APT_GPG_PRIVATE_KEY`, verifies it
against `keys/FINGERPRINT`, signs and tests the repository, and uploads the
`apt-repo-signed` Actions artifact. **Missing or mismatched signing material
fails publication; there is no unsigned-release fallback.**

Every flat repository file is a separate GitHub Release asset: 13 `.deb`s,
`Packages`, `Packages.gz`, `Release`, `InRelease`, `Release.gpg`, both public-key
exports, `bigtop.sources`, `release.json` (source/package provenance) and
`SHA256SUMS`. Generated source URLs use the actual `github.repository`.
No private keys, build directories or databases are uploaded. GitHub's per-asset
size limit is checked before publication.

Assets first go to a draft. The publisher verifies the complete uploaded asset
set, sizes and GitHub SHA-256 digests before publishing it. Retry a failed run
using **Re-run failed jobs** (or all jobs if its 14-day artifacts have expired):
incomplete drafts are repaired; published releases are immutable to this
workflow. A retry verifies the published release's own checksums and provenance,
rather than replacing it with timestamp-different rebuilt files.

Publication is serialized with `concurrency.queue: max` (GitHub permits up to
100 pending jobs), not a cancel-older-runs workflow group. A slower older build
can publish its own tag without moving `latest` backwards. Known old `vX.Y.Z-N`
apt tags can transition to automatic tags; unrelated manually selected latest
tags are preserved. Queue overflow or manual cancellations require a rerun.

The workflow uses GitHub's documented `queue: max` setting. Actionlint 1.7.12
predates that key; locally validate the rest with
`actionlint -ignore 'unexpected key "queue" for "concurrency" section'`.
Workflow regression tests separately check queue and publication gates.

## Install from the apt repository

Use the [TL;DR installer](#tldr) above, or configure the repository manually
with the commands below.

The release workflow publishes a flat apt repository at
`https://github.com/nekkoai/bigtop/releases/latest/download/`
(signing key `keys/bigtop-archive-keyring.gpg`, fingerprint in `keys/FINGERPRINT`;
CI signs with the `APT_GPG_PRIVATE_KEY` repository secret).

**Before the first automatic release:** configure `APT_GPG_PRIVATE_KEY` with the
matching ASCII-armored private key, usable by batch GnuPG without interactive
pinentry. Permit the workflow's `contents: write` jobs to create `v*` tags and
releases (including repository ruleset exceptions). The tracked public key
does not include private signing material. Validate the first GitHub run; these
URLs become usable only after its release is published.

The apt source is `bigtop.sources`, public-key exports are
`bigtop-archive-keyring.*`, and apt Origin/Label are both `bigtop`.
The signing key is pinned in `keys/FINGERPRINT` and expires
**October 19, 2028 (UTC)**.
Renew the key and public exports before expiration.
The unencrypted private export belongs only in a secure backup and the
`APT_GPG_PRIVATE_KEY` Actions secret, never in Git or release assets.

Verify the downloaded public key's fingerprint against the reviewed
`keys/FINGERPRINT` before using the repository.

```sh
sudo curl -fsSL https://github.com/nekkoai/bigtop/releases/latest/download/bigtop-archive-keyring.gpg -o /usr/share/keyrings/bigtop-archive-keyring.gpg
sudo curl -fsSL https://github.com/nekkoai/bigtop/releases/latest/download/bigtop.sources -o /etc/apt/sources.list.d/bigtop.sources
sudo apt update && sudo apt install asys-server
# Or the experimental Capsa stack (inert until explicitly configured):
sudo apt install capsa-server
```

`apt upgrade` then follows new releases (`releases/latest` always points at
the newest non-prerelease). Pin a specific release by replacing `latest/download`
with `download/<automatic-tag>` in `bigtop.sources`.

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

## Operational caveats

- **Docker access is privileged.** `asys-adduser` grants membership in both
  `asys` and `docker`. Docker-group access is root-equivalent; the current team
  setup assumes trusted users on a dedicated host, not hostile multi-tenancy.
- **Base-image bundling is not complete offline operation.** Environment images
  are built when workflows run, and starting/updating inference can build the
  gateway image and fetch dependencies. Test network requirements for the
  deployment you intend to support.
- **Minimal images may exclude examples.** A dpkg rule such as
  `path-exclude=/usr/share/doc/*` can discard the packaged Hello workflow.
  Ensure `/usr/share/doc/asys/examples/` is retained before installing/testing;
  if already excluded, adjust the policy and reinstall `asys`.
- **Shared state requires shared permissions.** Use the provided group-aware
  setup under `/srv/asys/host` and `/srv/asys/workspaces`; do not work around
  access errors by making state world-writable. Configure Git trust for specific
  shared checkouts rather than disabling ownership checks globally.
- **Upgrades can start services.** When no run is active, the server upgrade
  hook invokes `asys update`, which can activate the inference and Human
  services. Retain state, credentials and old images needed by existing work.

## Future work

These are directions to evaluate, **not implemented features or release
commitments**:

1. **More projects and compatibility coverage.** Extend the explicit build-unit,
   package/version and repository-validation maps when adding projects. Expand
   Capsa testing against real deployment/index histories beyond the synthetic
   integration suite.
2. **Reproducible, leaner artifacts.** Normalize build paths and timestamps,
   avoid copying host Python bytecode into images, and exclude unnecessary
   package-manager logs/caches and compile caches. Add repeat-build comparisons.
3. **Isolated upgrade coverage.** Extend the existing Capsa reinstall/active-run
   checks and signed apt tests to cross-version asys upgrades, reboot behavior
   and minimal-image policies.
4. **Image delivery and offline deployment.** Evaluate prebuilding the gateway
   and offering registry-based image distribution alongside bundled images.
   Preserve release/build identity and document the different network needs.
5. **Deployment hardening and portability.** Evaluate rootless Docker or scoped
   access with explicit asys/dcomp compatibility tests, and add architectures,
   distributions or packaging formats when needed. Reduce packaging-specific
   helpers where upstream can provide the same functionality.
