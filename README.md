# bigtop

Ainekko's packaging and deployment repository. The first supported package
set is [asys](https://github.com/glguida/asys) and
[dcomp](https://github.com/glguida/dcomp) on Ubuntu 24.04 (noble, amd64),
imported with history from `gh-ainekko/asys-debian`.
[Capsa packaging](pkg/capsa/README.md) provides a separate experimental
eight-package serving/indexing set for Ubuntu 24.04 amd64.

Upstream sources are pinned as git submodules under `upstream/`; GitHub
Actions builds, smoke-tests and (on `v*` tags) releases the `.deb`s.
Contributor and agent guidance: [AGENTS.md](AGENTS.md).

| Package | Built by | Contents |
|---|---|---|
| `dcomp` | `ci/build-dcomp.sh` | `dcomp`, `dcomp-proxy`, `dcomp-healthcheck` |
| `asys` | `ci/build-asys.sh` | all host commands, Python, vendored TUI deps, skills, designs, examples |
| `asys-images` | `ci/build-images.sh` (needs Docker + buildx) | container images of the same build ID plus `release.json`; loaded by postinst / `asys-images-load` |
| `asys-flow-view` | `ci/build-asys.sh` | X11 workflow viewer/editor |
| `asys-server` | `ci/build-asys.sh` | shared team instance under `/srv/asys` (`asys init /srv/asys/host --group asys`, as upstream documents) |

The table above is the current published-release design. Build Capsa separately
with `ci/build-capsa.sh` (output: `build/capsa-dist/`); its CI job installs and
tests that set without adding it to the asys/dcomp publication workflow. See its
README for package selection, explicit snapshot activation and testing caveats.

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
.github/workflows/build.yml   build -> smoke test -> apt metadata -> release on tag
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
packages or deployment framework are introduced in this first migration.

## Packaging principles

- **Keep upstream code upstream.** This repository owns packaging, deployment
  integration and release orchestration. Pin upstream sources rather than
  copying application implementations into `pkg/`.
- **Build related artifacts together.** The asys host packages and image bundle
  must use the same source revision, package version and upstream build ID
  (`tools/build-id`). Use one `ci/build-all.sh` run for a release set; matching
  version strings alone do not prove that separately built images match.
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
git clone --recursive https://github.com/gh-ainekko/bigtop.git
cd bigtop
ci/build-all.sh                 # current asys/dcomp release set -> build/dist/*.deb
```

Needs Go ≥ 1.25 on PATH, `debhelper`, `dh-python`, `devscripts`, `fakeroot`,
`python3-pip`, `zstd`, the X11 dev packages from `pkg/asys/debian/control`, Docker
with buildx. See `.github/workflows/build.yml` for the CI toolchain and dependency
installation commands. The current scripts invoke `dpkg-buildpackage` and
`dpkg-deb` directly; they do not provision an `sbuild` chroot.

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
They require `git`, `dpkg-dev`, `fakeroot`, `zstd` and Python 3, and do not
replace the full build and installation smoke tests.

After building, run the integration checks **only on a disposable test host**.
It needs systemd, its own Docker daemon with buildx, and the test/publication
tools used by CI (`curl`, Python 3, `dpkg-dev`, `apt-utils`, GnuPG):

```sh
sudo ci/smoke-test.sh build/dist
ci/make-apt-repo.sh build/dist
sudo ci/apt-repo-test.sh build/dist
```

The smoke test installs all five packages, checks the dashboard, runs the Hello
workflow as a second user and exports skills. The apt test verifies signatures
when signed metadata is present; otherwise it explicitly tests an unsigned
repository. Use a disposable signing key for signature-path testing, not a
production private key.

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

1. **More projects and release conventions.** Expand Capsa integration/compatibility
   coverage before publishing its experimental packages. Define how independently versioned
   projects participate in repository-wide releases before generalizing the
   current asys-oriented tag and cumulative-release scheme.
2. **Reproducible, leaner artifacts.** Normalize build paths and timestamps,
   avoid copying host Python bytecode into images, and exclude unnecessary
   package-manager logs/caches and compile caches. Add repeat-build comparisons.
3. **Isolated upgrade coverage.** Automate disposable build/install environments
   and expand CI beyond the existing smoke test to state-preserving upgrades,
   active-run handling, signed apt checks and minimal-image policies.
4. **Image delivery and offline deployment.** Evaluate prebuilding the gateway
   and offering registry-based image distribution alongside bundled images.
   Preserve release/build identity and document the different network needs.
5. **Deployment hardening and portability.** Evaluate rootless Docker or scoped
   access with explicit asys/dcomp compatibility tests, and add architectures,
   distributions or packaging formats when needed. Reduce packaging-specific
   helpers where upstream can provide the same functionality.
