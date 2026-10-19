# asys Debian packaging plan (Ubuntu 24.04 "noble", amd64)

Goal: `apt install asys-server` on a team server gives every engineer in group
`asys` a shared, upgradeable asys instance under `/srv/asys`, used as:

```sh
source /srv/asys/asys-env
asys-run ./env goal "..."       # etc.
```

Based on asys 0.2.0 (`562fa30`) and dcomp 0.3.2, pinned as submodules under `upstream/`.

---

## 1. What has to be shipped (inventory from the upstream Makefiles)

| Piece | Kind | Upstream target | Install location (PREFIX=/usr) | Build/host needs |
|---|---|---|---|---|
| `dcomp`, `dcomp-proxy`, `dcomp-healthcheck` | static Go binaries | `dcomp/Makefile build` | `/usr/bin` | Go 1.25 (build only) |
| `asys`, `asys-run`, `asys-workers`, `asys-environment` | Python 3 scripts + modules | `install-host` | `/usr/bin`, `/usr/share/asys/{python,skills,designs,workers,system_agents}` | python3 ≥ 3.10 only (stdlib) |
| `asys-inference` | static Go binary + Node component sources | `asys-inference/Makefile install` | `/usr/bin/asys-inference`, `/usr/share/asys-inference/{components,python}` | Go 1.25 (build only); gateway image is `docker build`-ed at `asys-inference start` from `components_root` |
| `asys-human-prompt` | Python TUI | `asys-human-interface/Makefile install-host` | `/usr/bin`, `/usr/share/asys-human/{python,vendor}` | textual 8.2.8 + rich 15 + markdown-it 4 (**noble has textual 0.1.13, rich 13.7 → must vendor**) |
| Base container images `asys-runtime`, `asys-workers`, `asys-worlds`, `asys-bpmn`, `asys-human-interface`, `asys-env-default` | Docker images, hard-tagged `:dev` | `make build` (docker build, needs network for `npm ci`/apt) | Docker Engine image store | Docker + network at **build** time; cannot be produced inside sbuild |

Runtime facts that matter for packaging:

* Host tools locate their Python packages relative to the executable
  (`<bin>/../share/asys/python`), so `PREFIX=/usr` works unchanged.
* Environment images (`FROM asys-workers:dev`) and world images are rebuilt by
  `docker build` at run / resume time; only the *base* images must pre-exist.
* Containers run as the invoking uid and the state's shared gid
  (`--user $(id -u):<asys gid>`), so group-shared state and mixed-owner
  workspaces already work multi-user upstream.
* `asys init DIR --group asys` / `dcomp init --group asys` create `2770`
  setgid trees; root may run it. No user/group creation upstream → do it in
  packaging.
* Every member must reach the local Docker socket → they need group `docker`
  too (Docker-group membership is root-equivalent; accepted trade-off, see §6).
* `asys update` only refreshes *running shared services* (inference gateway,
  Human service) to the newly installed sources/images; running workflow
  containers keep their old image IDs until they finish. Exactly the upgrade
  semantics you want, so the packages just have to call it.

---

## 2. Package set (4 packages)

Two source packages (`dcomp`, `asys`) built in sbuild, plus `asys-images`
built by a Docker-capable job. Only two boundaries are real: the image
tarball has a different build pipeline, and the server glue has install-time
side effects that a laptop/CI install must not get. Everything else host-side
is one package.

| Package | Source | Arch | Contents | Depends |
|---|---|---|---|---|
| `dcomp` | dcomp | any | `/usr/bin/{dcomp,dcomp-proxy,dcomp-healthcheck}`, docs | `docker.io \| docker-ce` |
| `asys` | asys | any | all six commands (`asys`, `asys-run`, `asys-workers`, `asys-environment`, `asys-inference`, `asys-human-prompt`), `/usr/share/asys/{python,skills,designs,workers,system_agents}`, `/usr/share/asys-inference/{components,python}`, `/usr/share/asys-human/{python,vendor}`, docs + examples under `/usr/share/doc/asys` | `python3 (>= 3.10)`, `dcomp (>= 0.3.1)`, `asys-images (= ${source:Version})`, `docker.io \| docker-ce` |
| `asys-images` | (docker job) | amd64 | `/usr/share/asys/images/asys-images-VERSION.tar.zst` + load/prune helper | `docker.io \| docker-ce` |
| `asys-server` | asys | all | team-deployment glue (§3): `asys` group + service user, `/srv/asys` init, `asys-adduser`, `asys-skills`, dashboard unit, nginx snippet, upgrade hook | `asys (= ${source:Version})`, `adduser`; Recommends: `nginx` |

### `dcomp`

* Build: `go mod vendor` in the orig tarball (offline sbuild), plain
  `make build`, `CGO_ENABLED=0 -trimpath`.
* Go toolchain: noble only has golang-1.22/1.24. Use `golang-1.25-go` from
  `ppa:longsleep/golang-backports` in the build chroot (or vendor a `go` tarball
  in the CI image). Same for the `asys-inference` binary inside `asys`.

### `asys`

Arch:any because it carries the Go `asys-inference` binary; the Python parts
don't care. Installed layout is upstream's `make install-host` +
`make -C asys-inference install` + `make -C asys-human-interface install-host`
with `PREFIX=/usr`, unchanged.

Vendored Python (textual & friends) goes into the orig tarball as a second
component `asys_0.2.0.orig-hostdeps.tar.xz` (`pip download --no-deps --no-binary :none:`
of the pinned `requirements-host.txt`, unpacked to `/usr/share/asys-human/vendor`
at build time — never at install time, never touching system site-packages).
All licenses (MIT, BSD) go into `debian/copyright`.

### `asys-images` (built by a Docker-capable job, not sbuild)

Ships the base images so that installs are deterministic, offline-capable and
apt-versioned:

```
make -C asys build                              # builds *:dev images
docker save asys-runtime:0.2.0 asys-workers:0.2.0 asys-worlds:0.2.0 \
            asys-bpmn:0.2.0 asys-human-interface:0.2.0 asys-env-default:0.2.0 \
  | zstd -19 > /usr/share/asys/images/asys-images-0.2.0.tar.zst
```

* Shared layers (node:22-slim + asys-runtime) mean the archive is ~1 GB raw,
  ~300–400 MB compressed; fine for an internal apt repo.
* `postinst`: if `docker info` works → `docker load`, then tag
  `asys-*:current` (and, until upstream patch §5.1 lands, also `:dev`). If
  Docker is not running (e.g. building a golden image), print a notice; the
  same load is exposed as `asys images load` / `asys-images-load` so it can be
  run later and is also invoked by `asys-server`'s upgrade hook.
* `prerm` never deletes images (running containers reference IDs, and old
  runs must finish); a `asys images prune` helper removes *untagged* asys
  images later.
* Alternative for the future: `asys-images-registry` variant whose postinst
  `docker pull`s from GHCR/internal registry instead of shipping the tarball.
  Same tags, same `asys` dependency; pick per site.

---

## 3. `asys-server`: the team workflow, packaged

What the admin does:

```sh
sudo apt install asys-server            # 1+3: group + /srv/asys created
sudo asys-adduser alice bob             # 2: usermod -aG asys,docker
sudo -u asys asys-inference gateway login openai-codex --as team   # once
```

What each engineer does:

```sh
source /srv/asys/asys-env               # 4
asys-run ./env goal "..."
```

`asys-server` implements:

1. **postinst (configure)**
   * `addgroup --system asys`; `adduser --system --ingroup asys --home /srv/asys asys`
     (service user for the dashboard/inference ownership; humans are *members*
     of the group, not this user).
   * `install -d -m 2775 -g asys /srv/asys /srv/asys/workspaces`
   * `asys init /srv/asys/state --dcomp /srv/asys/dcomp --group asys`
     (idempotent upstream; run as root). Symlink `/srv/asys/asys-env → state/asys-env`.
   * `asys-inference --root /srv/asys/state init` (no login, no start).
   * Copy `/usr/share/asys/skills/{asys,asys-authoring}` → `/srv/asys/skills/`
     via `asys skill /srv/asys/skills` (refreshed on upgrade only if unmodified
     — it is what agents get pointed at; see `asys-skills` below).
   * Enable `asys-dashboard.service`.
   * **Upgrade path** (`postinst configure <old-version>`): if
     `/srv/asys/state/inference/machine.json` says `running`, run
     `runuser -u asys -- asys --root /srv/asys/state update` (best effort:
     warn, don't fail the dpkg transaction if Docker is unavailable). This is
     the "new install just does `asys update`" requirement.
2. **`/usr/sbin/asys-adduser USER…`** – `usermod -aG asys,docker`, prints the
   `source /srv/asys/asys-env` reminder. `asys-deluser` is the inverse.
3. **`/srv/asys/asys-env`** – upstream-generated; additionally the package
   drops `/etc/profile.d/asys.sh` that only defines a shell function
   `asys-env() { . /srv/asys/asys-env; }` (nothing sourced automatically, per
   your workflow). Also `/etc/asys/server.conf` with `ASYS_HOME=/srv/asys` for
   the scripts above.
4. **`asys-dashboard.service`** – `User=asys`, `Group=asys`,
   `EnvironmentFile=/srv/asys/state/asys-env`-equivalent,
   `ExecStart=/usr/bin/asys dashboard --port 8766`, hardened (`ProtectSystem=strict`,
   `ReadWritePaths=/srv/asys`). Plus
   `/etc/nginx/sites-available/asys-dashboard` (loopback 8765 → 8766 with the
   `Host`/`Origin` rewrite we validated on this VM) until upstream patch §5.2
   makes it unnecessary; then the unit exposes `--public-host` directly.
5. **`asys-skills` command** (thin wrapper, ships with `asys-server`):
   `asys-skills install [--codex|--claude|--dir DIR]` copies the two skills
   from `/srv/asys/skills` into `~/.codex/skills`, `~/.claude/skills` or a
   directory, and `asys-skills refresh` re-syncs after an upgrade. Upstream's
   `asys skill DEST` already does the copy; the wrapper only knows the
   standard destinations. Propose upstream (§5.4).
6. **Logrotate/tmpfiles**: `tmpfiles.d` re-asserts `/srv/asys` perms;
   `/srv/asys/state/runs` is never rotated by us (that's the evidence).
7. **Multi-user hygiene**: umask 002 is not required — upstream already
   fchmods state files 0660 in shared roots. Workspaces are the users' own
   dirs; containers write as the invoking uid. `/srv/asys/workspaces` is
   provided as a setgid shared area for team projects.

---

## 4. Build & release pipeline

```
asys-packaging/             (this repo)
├── dcomp/debian/           gbp, upstream tag v0.3.2 → dcomp_0.3.2-1~noble1
├── asys/debian/            gbp, upstream tag 0.2.0 → asys_0.2.0-1~noble1
│   ├── control  rules  copyright  changelog
│   ├── asys.install  asys-server.install
│   ├── asys-server.{postinst,prerm,postrm}
│   ├── asys-dashboard.service  asys-adduser  asys-skills  nginx-asys-dashboard.conf
│   └── patches/  (see §5)
├── asys-images/            Makefile → docker build/save → dpkg-deb (nfpm-style, no sbuild)
├── ci/                     sbuild chroot recipe (noble + golang-1.25 PPA), aptly publish
└── PLAN.md
```

* Versions: `<upstream>-<rev>~noble1`; `asys` ↔ `asys-images` locked with
  `(= ${source:Version})` so a half-upgraded host cannot run mismatched
  Python vs. container code.
* `debian/rules` for `asys`: `dh $@`; `override_dh_auto_build`: `make host`
  + `make -C asys-inference build` (`GOFLAGS=-mod=vendor`) + unpack vendored
  hostdeps; `override_dh_auto_install`: `make install-host DESTDIR PREFIX=/usr`,
  `make -C asys-inference install …`, `make -C asys-human-interface install-host …`
  with `HOST_DEPS=$(CURDIR)/hostdeps` (skip the pip step). `dh_python3` for
  byte-compilation of `/usr/share/asys/python` (declare via `--shebang`/`X-Python3-Version`).
* Lintian will complain about vendored JS in `/usr/share/asys-inference/components`
  (`node_modules` are *not* shipped; `npm ci` happens inside the image build) —
  acceptable for an internal repo, override with a comment.
* Repository: `aptly` (or `reprepro`) with a signing key; `deb [signed-by=…] https://apt.internal/asys noble main`.
* CI order: dcomp → asys (sbuild) → asys-images (docker job, uses the just-built
  source tree) → smoke test in a fresh noble VM: install `asys-server`, add a
  user, run `asys-run /usr/share/doc/asys/examples/hello/env/dummy …/workflow.bpmn`
  (deterministic, no tokens) → publish.

---

## 5. Upstream patches to carry (and send to glguida/asys)

Status after 0.2.2: items 1–2 are solved upstream (release identity / versioned
image tags; `dashboard --allow-host`); no patches are carried any more. Item 3
is covered by `asys-images-load`; 4–6 remain nice-to-haves.


1. **Image tag selection** – replace hard-coded `:dev` in
   `worker_definitions.py`, `workers.py`, `workflow.py`, `human_service.py` by
   `ASYS_IMAGE_TAG` (default `dev`, packages set `current`/version). Lets old
   runs keep old tags and lets us keep several versions loaded.
2. **Dashboard `--allow-host HOST`** (or `--trust-proxy`) so the loopback
   nginx rewriter is optional.
3. **`asys images {load,status,prune}`** subcommand (or accept a hook dir) so
   `asys update` can also refresh base images from `/usr/share/asys/images`.
4. **`asys skill --codex/--claude`** standard destinations (§3.5).
5. Makefile: `install-host` should not `rm -rf` unrelated prefix dirs when
   `DESTDIR` is set (harmless in a clean DESTDIR, but noisy); add an
   `install-images`/`save-images` target.
6. Record `components_root` in `inference/machine.json` relative to the
   installed prefix — it currently stores an absolute path, which is fine for
   `/usr/share/asys-inference` but breaks if the package moves it.

---

## 6. Open decisions / risks

* **Docker group = root-equivalent.** Acceptable for a trusted engineering
  team on a dedicated server. Hardening options later: rootless Docker under
  the `asys` service user with `DOCKER_HOST` in `asys-env` (then members only
  need group `asys`), or a socket proxy. dcomp rejects TCP daemons, so it has
  to stay a Unix socket.
* **Shared checkouts and git `safe.directory`.** A working tree owned by one
  member is refused by git for the others (also inside worker containers).
  noble's git 2.43 cannot glob `safe.directory`, so either members add the
  path once, or teams use per-user clones and keep `/srv/asys/workspaces`
  for shared artifacts. `asys-adduser` prints the one-liner.
* **Image package size** (~350 MB/version). Keep only N versions in the repo;
  `apt-get autoclean` on servers.
* **Gateway image build at `asys-inference start`** needs network on the
  server (`npm ci`). Option: pre-build it into `asys-images` too and teach
  `asys-inference` to use a preloaded image (upstream patch).
* **Go 1.25 in noble** – PPA dependency in the build chroot only; runtime has
  no Go dependency.
* **0.1.x command names.** Your description mentions `asys-oneshot`,
  `asys-goal`, `asys skills`; in 0.2.0 these are
  `asys-run ENV WORKER "prompt"` (worker kinds `simple`/`goal`), `asys-run ENV
  WORKFLOW.bpmn`, and `asys skill DEST`. The `asys-server` wrappers can add
  `asys-oneshot`/`asys-goal` aliases if you want the short forms back.

---

## 7. Milestones

Status (2026-10-19): rebased on asys 0.2.2 (see VALIDATION-0.2.2.md): release.json
ships in asys-images, one build ID across packages, docker-buildx dependency,
asys-flow-view package, /srv/asys/host layout, upgrade hook skips active runs.

Status (2026-09-30): M1–M3 done and verified on try-cyclo with a second Unix
user (`alice`): hello workflow + LLM-backed hello-greeter workflow ran from the
packages alone; `asys-server` upgrade -1 → -2 refreshed the running gateway via
`asys update` with credentials intact. Remaining: M4 (upstream patches, versioned
image tags), M5 (apt repo + CI, sbuild chroot with the Go PPA).

1. `dcomp` deb + `asys` deb building in sbuild, installable on a clean noble VM with `PREFIX=/usr`.
2. `asys-images` deb + postinst load; deterministic hello workflow passes from the packages alone.
3. `asys-server`: group, `/srv/asys`, `asys-adduser`, dashboard unit, upgrade hook → team workflow end-to-end on this VM (second Unix user).
4. Upstream patches (§5.1–5.3) submitted; packaging switched to versioned image tags.
5. Internal apt repo + CI; retire the `~/.local` install on this VM.
