# Packaging-layout validation — 2026-10-19

**Result: no functional regression observed in the exercised packaging,
installation, workflow and same-version migration paths.** Both complete builds
passed. This is not a claim of bit-for-bit reproducibility or exhaustive upstream
test coverage.

## Inputs

| | Revision |
|---|---|
| Baseline `asys-debian` | `cfc29d13a93c86c36c303cf6951d0574f1ddc360` |
| Candidate bigtop | `e611b3f36a8b10593f8d09f78980cda09ca96add` |
| Shared asys source | `7fd29b725f7bc945a69f26d207ea3fc3bedc0330` |
| Shared dcomp source | `8db561fe7fd91622ece67be6986ba8d1a8c223d7` |

All 26 relocated packaging files have identical Git blob hashes and modes.
Both repositories ran:

```sh
git submodule update --init
DEB_BUILD_OPTIONS=parallel=2 ci/build-all.sh
ci/make-apt-repo.sh build/dist
```

These were real host builds, upstream Docker builds, image exports and Debian
package builds—not fixture/stub builds. Existing Docker build caches were
available; neither build used `--no-build`.

Build host: Ubuntu 24.04, Go 1.26.5, GCC 13, debhelper 13.14.1ubuntu5,
dpkg-dev 1.22.6ubuntu6.6, Docker 29.1.3, buildx 0.30.1, zstd 1.5.5.
The workflow's exact Go 1.25 environment was not separately tested.

## Results

| Check | Result |
|---|---|
| Complete build, both repositories | PASS: all five release packages |
| Candidate's lightweight packaging regression tests | PASS: 2 tests |
| Installation, image loading and release/image agreement, both repositories | PASS |
| Systemd dashboard and HTTP/API checks, both repositories | PASS |
| Hello workflow as a second Unix user, both repositories | PASS |
| Packaged skill export, both repositories | PASS |
| Unsigned local apt metadata/install checks, both repositories | PASS |
| Candidate signed apt metadata and download-only reinstall check | PASS with a disposable signing key |
| Baseline-to-candidate same-version reinstall | PASS: original run records preserved; new workflow succeeds |
| Shared inference gateway and Human service after reinstall | PASS: healthy, reporting the packaged build |
| Final guest package audit | PASS: no incomplete package transactions |

Tests used separate writable QEMU/KVM guests backed by the same pristine
Ubuntu 24.04 minimal cloud-image/prerequisite snapshot. Guests had real systemd
and their own Docker daemon using classic overlay2. Repositories were mounted
read-only; the host Docker socket was never passed into a guest.

### Test-environment findings

The minimal image's `path-exclude=/usr/share/doc/*` policy initially discarded
the packaged Hello workflow. **Both original and candidate smoke tests failed
identically**, although both packages contain the file. In each guest, that
exclusion was disabled and `asys` reinstalled before rerunning the unchanged
`ci/smoke-test.sh`. This is an existing minimal-image installation limitation,
not a layout regression.

Fresh instances also emit a warning about unapplied inference configuration;
the deterministic smoke test does not configure an inference provider.

The same-version reinstall exercised the existing-version maintainer-script
path with `apt-get install --reinstall`. The existing server upgrade hook
activates shared services, legitimately changing `inference/machine.json`.
An initial harness assertion that this runtime file remain byte-identical was
therefore discarded. Verification instead checked preservation of all original
run records, healthy/current services, the dashboard, and a new Hello workflow.
The follow-up workflow also had to run from `/srv/asys/workspaces`, as the
original smoke script does—not the SSH user's private home. These harness
corrections required no packaging changes.

## Artifact comparison

| Package | Version | Comparison |
|---|---|---|
| `dcomp` | 0.3.2-1 | Entire `.deb` byte-identical |
| `asys` | 0.2.2-3 | Entire `.deb` byte-identical |
| `asys-server` | 0.2.2-3 | Entire `.deb` byte-identical |
| `asys-flow-view` | 0.2.2-3 | Runtime code/data identical; ELF build ID and debug link differ |
| `asys-images` | 0.2.2-3 | Build/cache/provenance differences detailed below |

The viewer executable differs in only its GNU build-ID note and debug link.
Its runtime sections, including `.text` and `.rodata`, are identical. This is
consistent with debug/build-path provenance; the precise linker-input cause
was not established.

The image package has identical loader and maintainer scripts. Control fields
match except the calculated `Installed-Size`; checksums follow the changed
archive and release record. Its six image refs/tags and runtime configurations
match. The non-identical bytes were traced to:

- OCI creation/history timestamps, image IDs, and archive ordering/timestamps;
- Python bytecode cache source timestamps and checkout-root filenames
  (`co_filename`); corresponding Python source and normalized code objects match;
- apt installation logs;
- npm cache/log/index records and Node compile caches.

The actual `node_modules` file contents match. No changed application source,
installed application dependency, or runtime image configuration was found.
`release.json` differs only in the six image IDs, and the existing package loader
correctly regenerates those IDs for the guest's Docker engine.

Publication metadata retains the same public keys, Origin, Label, Suite,
Codename and Architectures. The new bigtop repository URL/description is
intentional. Signed-apt testing used candidate packages and unchanged candidate
scripts with a disposable key; it did not exercise the production signing secret
or publish a release.

## Evidence and limitations

Candidate artifacts remain in `build/dist/`. Machine-local evidence is under
`build/validation/`, notably:

- `bigtop-build.log`, `source-parity.txt`, `build-environment.txt`;
- `package-comparison.json`, `artifact-findings.md`, `publication-and-hashes.txt`;
- `candidate-clean-docs-enabled.log`;
- `baseline-clean-and-upgrade-docs-enabled.log`;
- `migration-and-signed-apt-corrected.log`, `final-guest-state.log`;
- `signed-fixture/logs/`, `host-images-restored.txt`.

Baseline build/metadata logs remain under
`/home/exedev/asys-debian/build/validation/`. Logs retain initial harness failures
as well as the successful corrected checks.

This validation did not run the entire upstream test suites, interactive X11
editing, authenticated inference-provider calls, cold-cache builds, or migration
from a different upstream version.

Test VMs and writable disks were removed afterward. The final guest required
terminating its QEMU process after a teardown helper failed to match its disk
pathname; application/apt checks had already passed. All six original host image
tags were restored to the unchanged installed release record, and the four
pre-existing host containers remained running without restart. Only disposable
files/caches generated by this validation were cleaned; no pre-existing Docker
cache was pruned.
