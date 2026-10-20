# Capsa packages

Experimental **Ubuntu 24.04 amd64** packages for functional testing. They are
built separately from the existing asys/dcomp release set; nothing is published
to the apt repository by these build commands.

## Package set

| Package | Contents |
|---|---|
| `capsa-core` | Rust CLI/server, including its libSQL vector implementation |
| `capsa-python-runtime` | Private CPython 3.12.15 and 29 hash-locked production dependencies; no pip, test tools or Tk |
| `capsa-federation` | Read-only MCP gateway and private-runtime launcher |
| `capsa-llama-cpu` | CPU/OpenMP llama-server and matching private libraries |
| `capsa-embedding-assets-nomic` | Verified Nomic GGUF, tokenizer and exact HF cache layout |
| `capsa-indexers` | Optional Jira/Confluence ingestion launchers; unsupported Drive reference source |
| `capsa-services` | Inert-by-default units, snapshot registration and coordinated recovery |
| `capsa-server` | Serving metapackage; does not depend on indexers |

The primary `debian/` source recipe builds core, federation, indexers, services
and server together. Companion recipes live in `python-runtime/`, `llama-cpu/`
and `embedding-assets-nomic/`. All eight binary versions derive from
`debian/changelog` so their exact-version dependencies remain coherent.

## Pins and runtime profile

- Capsa: `587d679984ba0973f7b5a2cd8228baf159928ad5` at `upstream/capsa`.
- Rust: exact toolchain in `rust-toolchain.toml`; unmodified upstream Cargo lock.
- llama.cpp: upstream release **v0.7.0**, published October 15, 2026, commit
  `620d2225cc436ab0b001e3200e590e3b286ef9ab`, fetched from the required
  `aifoundry-org/llama.cpp` fork and pinned at `upstream/llama.cpp`.
- CPython: python-build-standalone 3.12.15 / release `20261013`; archive and wheel
  hashes are recorded under `python-runtime/`.
- Model: `nomic-embed-text-v1.5.Q4_K_M.gguf`, 768 dimensions, revision
  `18d1044f4866e224159fce8c6fc5c4f3920176e7`. Tokenizer revision:
  `e9b6763023c676ca8431644204f50c2b100d9aab`. Exact URLs, sizes, hashes and cache
  layout are in `embedding-assets-nomic/inputs.json`.

The embedding unit uses loopback port 9001, 24 threads and batch/ubatch 4096,
following the supplied launch record. CPU baseline builds disable native ISA
optimizations and GPU backends. The old deployment used a different,
*native-optimized* llama build (6875): these test packages intentionally use the
new release, not a claim of numerical parity with existing indexes.
Set `CAPSA_THREADS` in `/etc/capsa/embeddings.env` to adapt thread count to the
test host; the smoke test uses two threads.

## Build

Use a disposable Noble amd64 builder, never a serving host. Allow several GiB
for the compiler/vendor/build trees. Native prerequisites are listed in the
recipes and `.github/workflows/capsa-core.yml`. Install the exact Rust toolchain
from `rust-toolchain.toml` using rustup before building.

```sh
ci/build-capsa.sh                 # all eight -> build/capsa-dist/*.deb
```

The driver runs `build-capsa-core.sh`, `build-capsa-runtime.sh`,
`build-capsa-llama.sh` and `build-capsa-assets.sh`. Source pins are read from the
Git index, so stage a deliberate submodule update before building.

Preparation downloads and verifies inputs; Cargo compilation is locked/offline
and Python assembly uses hash-required, no-index inputs. Downloads are cached in
`build/capsa-inputs/`. Package installation never fetches models, runs pip or
resolves application dependencies. Ubuntu runtime-library dependencies come from
actual ELF analysis, not assumptions about ordinary SQLite or a developer home.

## Install and activate

```sh
sudo apt install ./build/capsa-dist/*.deb
```

This installs the optional indexers too. From a repository containing this set,
the corresponding selection is `apt install capsa-server capsa-indexers`.
Installation creates the dedicated `capsa` account and empty state/configuration
directories, but **does not enable or start services, create databases or ingest**.

Paths:

- `/usr/bin/capsa`, `capsa-federation`, `capsa-llama-server` and operator commands.
- `/usr/lib/capsa/`: private interpreter, application code, helpers and llama libs.
- `/usr/share/capsa/`: model/tokenizer assets and their manifest.
- `/etc/capsa/`: operator configuration; `/var/lib/capsa/` and `/var/cache/capsa/`:
  writable service state/cache.
- `/srv/capsa/snapshots/`: default immutable database location, separate from
  writable ingestion staging.

Provision **already checkpointed** snapshots separately. Files and their snapshot
parent directories must be root-owned and not group/other writable, with read
access for `capsa`. Registration never checkpoints, rewrites or chowns a database.
Do not copy a database away from a live writer or assume a missing destination WAL
proves its source was checkpointed.

```sh
for source in drive slides confluence jira; do
    sudo capsa-register-snapshot "$source" "/srv/capsa/snapshots/$source.db" --checkpointed
done
sudo capsa-managed-recover
# Optional, only after successful configuration/testing:
sudo systemctl enable capsa-gateway.service
```

Registration records destination-local file identity and SHA-256 generations;
it emits federation configuration after all four sources are registered. Native
Capsa immutable startup validates the libSQL schema. No generic SQLite integrity
check is substituted for libSQL vector support.

A root-owned `/etc/capsa/paths.json` may select another absolute `snapshot_root`;
keep it outside writable service-state/cache directories. For remote mounts,
also add an appropriate systemd `RequiresMountsFor=` dependency. Do not reuse
host inode stamps or database generations from another machine.

The service chain is embeddings, four immutable readers (10080–10083), then the
loopback gateway (10084, `/mcp`). Asset/configuration checks and readiness checks
fail closed. `/etc/capsa/STOP_MANAGED_SERVERS` blocks gateway admission.

## Updates and recovery

The gateway deliberately has `Restart=no`. Do not clear uncertain-work state by
restarting only the gateway. `capsa-managed-recover` stops it and all four readers,
checks that old backend processes are gone, restarts/readiness-checks the readers,
and only then reopens gateway admission. Failure leaves the gateway stopped.

Runtime package preinst guards refuse upgrades while this stack is active:

```sh
sudo systemctl stop capsa-gateway.service
sudo systemctl stop capsa-snapshot@{drive,slides,confluence,jira}.service capsa-embeddings.service
sudo apt install ./build/capsa-dist/*.deb
sudo capsa-managed-recover
```

Stop any separately managed ingestion writers before changing their packages too.
Use `apt install --reinstall` when testing a rebuilt set with unchanged versions.
Configuration, snapshots and writable ingestion state are retained. Maintainer
scripts do not migrate databases, disable unrelated services or remove data.

## Indexers

`capsa-index-jira` and `capsa-index-confluence` use the private interpreter and
require explicit archive/work/database/port arguments plus the upstream writable
service acknowledgement. Packaging guards reject the default snapshot roots,
registered snapshot directories and hardlink aliases. They do not start writers;
use separate local ingestion staging, not published snapshots or the gateway.

The collected Drive adapter still expects a CLI contract that the pinned core
does not reliably provide; it is reference material, not a supported entrypoint.
The original Slides discovery/export wrapper remains absent. Native PPTX parsing
is not that missing workflow. Historical scripts and corpus data are not shipped.

## Tests

```sh
python3 -m unittest discover -s tests -v
python3 pkg/capsa/python-runtime/tests/validate-inputs.py
# Installs packages and explicitly starts test services: disposable host ONLY.
sudo env CAPSA_DISPOSABLE_TEST_HOST=1 ci/smoke-test-capsa.sh build/capsa-dist
```

Fixture tests do not compile native code. The smoke test is for real package and
synthetic-data behavior, not proof that existing production indexes are compatible
with the new llama release. Offline target dependency closure, broader upgrade/
recovery coverage and production index compatibility remain further validation,
not prerequisites to producing these experimental test packages.
