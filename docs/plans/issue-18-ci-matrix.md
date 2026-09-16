# Build and sanitizer CI implementation plan

Related: [issue #18](https://github.com/iconidentify/libva-v4l2_request/issues/18),
under [roadmap #7](https://github.com/iconidentify/libva-v4l2_request/issues/7).

This initial draft defines the work and validation for the CI expansion. It does
not change the build or establish new supported dependency versions.

## Starting point

At `b9e30f3fee022b67daa28cecc22658db453ccc27`,
`.github/workflows/checks.yml` has one `userspace` job on `ubuntu-latest`.
It installs distribution dependencies, runs ASan/UBSan tests, and runs the two
software frame checks. External checkout uses the mutable `actions/checkout@v4`
tag. The workflow grants only `contents: read`.

`meson.build` declares libva >= 1.10.0 and probes six stateless codec controls
plus the optional HEVC `num_delta_pocs_of_ref_rps_idx` member. A successful
control probe does not prove every type or field used by that codec exists.
`src/meson.build` compiles every codec source; the sources and test registration
use feature guards. These are the boundaries the compatibility jobs must test.

## Implementation sequence

1. **Establish dependency bounds.** Audit the actual VA, V4L2 and DRM types,
   constants and fields used by each enabled codec. Build against candidate
   minimum and current libva/kernel-UAPI/compiler combinations before documenting
   supported versions. Pin dependency source revisions or image digests and
   record compiler, Meson, libva, libdrm and header package versions in each job.
   If the declared libva minimum fails, either repair the guards or raise the
   minimum with the failing API named in the configure diagnostic.
2. **Split executed and build-only coverage.** Run GCC and Clang ASan/UBSan
   builds and offline tests on x86_64 and aarch64 GitHub-hosted runners where
   available. Label any cross-compiled fallback as build-only, and document the
   missing execution coverage. Build the shared driver explicitly as well as the
   test executables. Keep both software shell checks in executed environments.
3. **Exercise feature guards.** Add Meson codec feature options with automatic
   detection as the default. Test all available codecs, individual disabled
   codecs, and an all-disabled configuration. An explicitly enabled codec whose
   required UAPI is absent must fail during configuration with an actionable
   message. Use genuinely older headers to test missing members; forcing macros
   off on modern headers alone is insufficient. Keep tests that do not require
   the disabled codec, and report the expected test set for each configuration.
4. **Preserve the required check.** Keep `userspace` as a stable aggregate job
   depending on every required child job. Evaluate it even after dependency
   failure. Only a complete set of successful required jobs may pass; failure,
   cancellation, skipped jobs and missing results must prevent success. Add
   negative fixtures for these states and check that the workflow's dependencies
   match the evaluator's required job set. Use `fail-fast: false` to retain
   evidence from other matrix entries after one fails.
5. **Add analysis and durable evidence.** Run a bounded static-analysis job on
   driver sources and triage its findings. Fix defects or document narrow
   suppressions with reproductions. Preserve assertions and sanitizer checks.
   Pin every external action to a reviewed full commit SHA. Retain configure,
   build and test logs on failure, with source and dependency identities.

Expected implementation paths: `.github/workflows/checks.yml`, `meson.build`,
`meson_options.txt`, `src/meson.build`, `tests/meson.build`, CI validation helpers,
and `tests/README.md`. Recheck active claims before changing shared test files.
Changes to decoder behavior require their own regression evidence and review.

## Validation and acceptance

The implementation PR must supply evidence for every acceptance criterion in
issue #18 before it is ready. In particular:

- Execute both compilers' offline tests and show architecture and dependency
  versions. Publish aarch64 execution evidence or the explicit build-only limit.
- Demonstrate oldest-supported and current dependency builds. Deliberately
  configure an unsupported combination and inspect its diagnostic.
- Demonstrate a passing aggregate, then a deliberately failing child and the
  cancelled, skipped and missing-result cases. Verify the actual workflow
  wiring, not only a helper tested in isolation.
- Record static-analysis findings and the disposition of each suppression.
- Use only GitHub-hosted runners for public PRs, with read-only permissions and
  no repository secrets. Never use `pull_request_target` to execute PR code.
  Run `tests/frame-check.sh` and `tests/shared-contexts.sh` without a driver
  argument. Hardware suites remain outside this workflow.
- Link exact run results and record tests that did not run. Keep the issue open
  until the implementation is merged and the evidence meets its criteria.

## Baseline commands

Run from a clean checkout with a fresh build directory:

```sh
meson setup build-baseline -Db_sanitize=address,undefined
meson compile -C build-baseline
meson test -C build-baseline --print-errorlogs
sh tests/frame-check.sh
sh tests/shared-contexts.sh
```

The initial draft PR records observed baseline results. Those results validate
the existing checkout only; they do not prove the proposed matrix, minimum
versions, aggregate behavior or hardware support.
