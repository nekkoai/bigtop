# Validation of asys-debian (built for asys 0.2.0) against asys 0.2.2 / dcomp 0.3.2

Historical validation from `asys-debian`; paths below predate the move to
`pkg/` in bigtop. See [README.md](README.md) for the current layout.

Date: 2026-09-30. Reviewed upstream `562fa30` (0.2.0, our pin) → `7fd29b7`
(tag `v0.2.2`, 46 commits) and dcomp `v0.3.2` → `origin/main` (1 commit).

## Verdict

* **dcomp pin is right.** Upstream asys 0.2.2 requires dcomp 0.3.2 and pins
  the Go module to v0.3.2. The only commit after the tag (`a46989e`, macOS
  socket publication) is irrelevant on Linux. Keep `upstream/dcomp @ v0.3.2`.
* **Server layout and group handling are compatible**, with one cosmetic
  difference: upstream's INSTALL.md now documents
  `asys init /srv/asys/host --group asys` (dcomp state inside, `host/dcomp`),
  while `asys-server` uses `/srv/asys/state` + `/srv/asys/dcomp`. Both work;
  the paths are configurable in `/etc/asys/server.conf`. Suggest switching the
  default to upstream's spelling so the package matches the docs people read.
* **A plain submodule bump to 0.2.2 does not produce working packages.**
  0.2.2 changed the build/installation contract in ways `asys/debian/rules`,
  `ci/build-*.sh` and `asys-images` must follow. Details below.

## What 0.2.2 changed that the packaging depends on

| Change (upstream commit) | Effect on current packaging | Required fix |
|---|---|---|
| **Release identity** (`1632480`, `build.mk`, `tools/build-id`, `tools/build-image`): images are tagged `NAME:$ASYS_BUILD` (e.g. `asys-workers:0.2.2-7fd29b7`), recorded with their image IDs in `release.json`, installed to `PREFIX/share/asys/release.json`. Host tools resolve images **only** through that file and verify the tag still names the recorded ID. `:dev` is a legacy spelling. | `debian/rules` never runs `make build` (sbuild has no Docker) → no `release.json` in the `asys` package → every `asys-run` fails with "No asys release found". `asys-images` tags `:dev` and `:VERSION`, which nothing reads any more. | Produce `release.json` in the image job and ship it **in `asys-images`** at `/usr/share/asys/release.json` (the only package that can guarantee the IDs match the loaded images). `asys-images-load` becomes `docker load` only; drop the retagging. Pin `asys` ↔ `asys-images` to the same upstream version (already done). |
| **Build ID in binaries and `asys version`**: `asys-inference` is linked with `-X Version=… -X Build=$ASYS_BUILD`; `asys version` and `asys update` compare the installation's build with running services. | `build-asys.sh` builds from `git archive` (no `.git`) so `tools/build-id` prints `0.2.2`; `build-images.sh` builds in the checkout → `0.2.2-7fd29b7`. Mismatch → everything reports "outdated". | Compute `ASYS_BUILD` once in CI (`upstream/asys/tools/build-id`) and pass the same value to `dpkg-buildpackage` (exported from `debian/rules`) and to `make build` for the images. |
| **Host Python deps moved/added**: core commands now need `python/requirements-host.txt` (ruamel.yaml) vendored into `share/asys/vendor`; `make host` / `install-host` require `$(HOST_DEPS)/.installed` for the root and for `asys-human-interface` separately. | `build-asys.sh` only vendors the human-interface deps; `install-host` would abort ("Host dependencies are not built"). | Vendor both requirement files into the orig tarball (`hostdeps/` and `hostdeps-human/`) and pass `HOST_DEPS=` to both makes. Add ruamel.yaml to `debian/copyright`. |
| **Makefile targets no longer build**: `install`, `install-host`, `asys-inference install`, `asys-human-interface install-host` just copy; `make install` runs `make build` as `$SUDO_USER`. | `override_dh_auto_build` calls `make host`, `make -C asys-inference build`, `make -C asys-human-interface host` — still valid, but must pass `ASYS_BUILD` and `HOST_DEPS`. | Minor rules update. |
| **`--allow-host` landed upstream** (`5f697cf`), plus `dashboard --run`. | Our quilt patch `0001-dashboard-allow-host.patch` conflicts. | Drop the patch; `ASYS_DASHBOARD_ARGS="--allow-host …"` keeps working. |
| **Dockerfiles use bare `FROM asys-workers`** resolved via `--build-context` (BuildKit). INSTALL.md: "Docker Engine 23+ with BuildKit". | `docker.io` 29 on noble has no legacy builder; `--build-context` needs the buildx plugin, which `docker.io` does not depend on. | `asys` and `asys-images` Depends: `docker-buildx`. Example `hello/env/dummy/Dockerfile` already uses the bare form. |
| **`asys update` drains active runs** (`f0b8f02`, `388c3d9`): with running workflows it waits for them to reach a barrier (polling, no bound), or stops and lists them; `--force` updates anyway and they fail. | `asys-server` postinst runs `asys update` on upgrade → dpkg could block for the whole duration of running workflows. | Postinst must not call `asys update` directly. Options: (a) detect active runs (`asys ps --json`) and print "run `asys-server-update --update` when idle"; (b) run it with a timeout in a transient `systemd-run` unit so dpkg returns. Recommend (a) + leave `asys-server-update --update` for the operator; document that `asys-run --upgrade RUN` exists. |
| **`asys init` initializes dcomp under `STATE/dcomp` by default** (`6b3e12e`), same group. | `--dcomp /srv/asys/dcomp` still works. | Optional: drop `--dcomp` and `DCOMP_STATE_ROOT` from `server.conf`, adopt `/srv/asys/host`. |
| **New commands**: `asys version`, `asys check/resolve/convert` (workflow files, YAML), `asys-run --upgrade`. | Nothing to package; smoke test can add `asys version`. | Add `asys version` to `ci/smoke-test.sh` (verifies release.json ↔ images). |
| **`asys-flow-view`** (`c4a05fa`): optional X11 viewer/editor, built when Xaw/Xft dev files exist. | Not built today. | Either skip (installer prints a notice) or add a `asys-flow-view` binary package with `Build-Depends: libxaw7-dev libxft-dev libxmu-dev libxt-dev libxrender-dev libfontconfig-dev pkg-config` — cheap, so add it. |
| **`asys-bpmn-convert`** kept out of the installation (`93b29f2`). | – | Nothing. |
| **Pi 0.99.1, inference overflow fixes, senate changes** | – | Runtime only; covered by rebuilding the images. |

## Submodule / version plan for the update

* `upstream/asys` → `7fd29b7` (`v0.2.2`), `upstream/dcomp` stays `8db561f` (`v0.3.2`).
* `asys (0.2.2-1)`, `asys-images` from the same build ID, `dcomp (0.3.2-1)` unchanged.
* Smoke test must additionally cover: `asys version` (all images current),
  environment build from a bare `FROM asys-workers` Dockerfile, and an upgrade
  from 0.2.0-3 (→ exercises the postinst change and `asys update` with no
  active runs).

## Answer to "push the PR or let Shelley redo it?"

Push the PR. A bump commit is the right place to start, but it will fail CI on
the first point above (no `release.json`), so expect the review to turn into
the checklist here rather than a rubber stamp. The remaining work is a few
hours in `asys/debian/rules`, `ci/build-asys.sh`, `ci/build-images.sh`,
`asys-images/*` and `asys-server.postinst`, and I can do it on top of the PR
branch rather than from scratch.

## Found while releasing 0.2.2-2: image IDs are not portable

`release.json` records `docker image inspect --format {{.Id}}` on the build
host. With the classic graphdriver (`Storage Driver: overlay2`, e.g. the GitHub
runner's docker-ce) that is the image *config* digest; with the containerd
image store (`Storage Driver: overlayfs`, e.g. docker.io 29 on noble) it is the
*manifest* digest. The same `docker save` archive therefore loads with
different IDs on the two kinds of host and `asys version` / `asys-run` reject
every image ("no longer names the image of release …").

Packaging fix: `asys-images` ships the build-host record as
`/usr/share/asys/images/release.json`; `asys-images-load` (postinst) loads the
archive and writes `/usr/share/asys/release.json` with the IDs this engine
reports. Worth reporting upstream: verify images by tag + `RepoDigests`/config
digest, or offer an `asys images load` that re-records.
