# Contributor and agent guidance

## Scope and sources of truth

- Read [README.md](README.md) and any more-specific guidance before editing.
  README documents current behavior, commands, caveats and future directions;
  future work is not an instruction to implement unrelated changes.
- This repository owns packaging and deployment, not application implementation.
  Prefer upstream fixes; document any packaging-specific patches and their
  rationale rather than silently modifying a pinned upstream checkout.
- Source pins are the Git submodule commits and `.gitmodules`. Package versions
  and dependencies come from changelogs/control files; build behavior comes from
  `ci/`, packaging rules and `.github/workflows/`. Check these before updating docs.

## Layout and invariants

- Use `pkg/<project>/<format>/` for primary packaging and
  `pkg/<project>/<artifact>/<format>/` for companion artifacts.
  In particular, image packaging lives at `pkg/asys/images/debian/`.
- Keep binary packages from one source build together. `asys-server` and
  `asys-flow-view` stay in `pkg/asys/debian/`; shared orchestration stays in `ci/`.
- Keep asys host packages and images on the same source pin, package version and
  build ID. Do not introduce an independent image version or source checkout.
  Preserve target-engine release-record regeneration; Docker image IDs are not
  portable.
- Preserve package identities, dependencies, installation paths, persistent state
  and group ownership unless the task explicitly calls for a tested migration.
  Keep maintainer scripts idempotent and retain the active-run update guard.
- Do not change upstream pins or package versions incidentally during a refactor.

## Validation

- Follow [Testing](README.md#testing). Start with the lightweight regression tests
  and `git diff --check`; run relevant full builds and isolated integration tests
  for packaging/build changes. Documentation-only edits need link/path checks,
  not another full image build.
- For structural refactors, compare against the same upstream pins/toolchain:
  package names, dependencies, contents, permissions, maintainer scripts and
  release/image consistency. Explain provenance/cache differences rather than
  silently normalizing away arbitrary mismatches.
- For install/upgrade changes, check existing state and run preservation,
  dashboard/workflow behavior and apt metadata/signatures. Use disposable signing
  keys; never alter tracked production trust material to make a test pass.
- Report what actually ran: fixture tests versus real builds, cached versus
  cold builds, unsigned versus signed apt checks, failures and untested paths.

## Safety

- Isolate both builds and installation tests in disposable environments with
  their own Docker daemons. Builds can overwrite live image tags; a container
  sharing the host Docker socket is not sufficient isolation.
- Do not install test packages on a live deployment, delete shared state, prune
  shared Docker caches/images, or replace existing tags without explicit approval.
  Check disk/RAM capacity before expensive work; clean only task-owned scratch
  resources and stop only task-owned services.
- Require successful shutdown before removing a test VM's backing storage.
  Retain access needed for teardown until the VM is stopped.

## Repository hygiene and releases

- Keep generated packages, logs, comparison manifests, temporary validation
  reports and machine-specific notes under ignored `build/`, not in Git.
  Never commit credentials, private signing keys or runtime state.
- Keep durable usage, caveats and future-work notes in README; keep this file
  focused on contributor rules. Avoid historical plans and one-off test diaries.
- Preserve unrelated work and review the final diff and submodule status.
- Do not make unrequested version bumps, trust/key changes, release tags or
  publications. Follow [Updating upstream](README.md#updating-upstream) and the
  release prerequisites only when the task authorizes those actions.
