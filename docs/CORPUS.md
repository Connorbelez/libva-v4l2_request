# Regression corpus: licensing, provenance and acquisition

The r11 codec evidence was produced from locally downloaded Fluster resources, generated
media and an unpinned Fluster checkout. Nothing recorded **where** an input came from, under
which terms it could be used, which bytes were tested, or why each remaining failure is
expected. This document and `tests/corpus/manifest.json` close that gap.

The manifest does not claim decoding support. It records acquisition and provenance; support
claims stay in [SUPPORT.md](SUPPORT.md) and the [support matrix](support-matrix.json).

## What is pinned

| Layer | Pinned by | Checked by |
|---|---|---|
| Upstream suite definitions | Fluster commit + `suite_file_sha256` per suite | `corpus.py validate --fluster DIR` |
| Usage terms | `terms_status`/`terms_reference`/`terms_checked`/`terms_note` per entry | `corpus.py validate` rejects a title or URL presented as a licence |
| Upstream downloads | `source_checksum` (MD5) from the pinned suite file | `corpus.py fetch` before an asset is accepted |
| Assets consumed by the runner | `asset_sha256` (SHA-256) of the extracted input file | `corpus.py verify`, and `corpus.py lock` for assets not yet recorded |
| Failure reasons | 13 documented classes with the explicit vector lists they cover | `corpus.py validate` re-derives each list and proves the r11 failing sets are fully covered, without contradiction |
| Generated matrices | producer script + runnable invocation, frame counts, determinism rules | `corpus.py validate` checks the producer exists, the invocation runs it, and cross-checks frame counts against `docs/r11-pass-sets.json` |

Two hash layers are deliberate. SHA-256 identifies the bytes a result was produced from.
The upstream MD5 only detects that the distributor replaced a file; it is taken from the
pinned suite definition, so the pinned suite digest also makes that MD5 tamper-evident.

## Commands

```sh
# Offline: policies, hashes, derivations, pass-set completeness
python3 tests/corpus.py validate --fluster /path/to/fluster

# Bounded acquisition (the subset CI and a new contributor need)
python3 tests/corpus.py fetch --fluster /path/to/fluster --cache ~/.cache/libva-corpus --smoke

# Offline integrity check of what is in the cache
python3 tests/corpus.py verify --fluster /path/to/fluster --cache ~/.cache/libva-corpus --require smoke

# See what a suite-wide acquisition would select, without acquiring anything
python3 tests/corpus.py fetch --fluster /path/to/fluster --cache ~/.cache/libva-corpus --all --dry-run

# Report SHA-256 for acquired assets; a reviewer records them in the manifest
python3 tests/corpus.py lock --fluster /path/to/fluster --cache ~/.cache/libva-corpus

# Hermetic negative-case and acquisition checks (no network, no decoder)
python3 tests/corpus.py self-test
```

`--mirror` takes an HTTP(S) base URL or a local directory and rewrites upstream URLs the way
Fluster's `--mirror` does (`<mirror>/<host>/<path>`), falling back to the original source. It is
how an air-gapped or CI machine serves pre-seeded resources.

The cache keeps Fluster's `<suite_name>/<vector_name>/<input_file>` layout, so it can be passed
straight to the runner:

```sh
python3 tests/conformance.py /path/to/fluster/test_suites/h.265/JCT-VC-HEVC_V1.json \
  ~/.cache/libva-corpus --driver /path/to/build/src --output /path/to/new-results
```

**Selection enumerates the pinned suite definition, not the manifest's pinned assets.** Pinning
a vector's SHA-256 and acquiring it are separate steps:

| Selection | Resolves to | Needs |
|---|---|---|
| `--smoke` | the bounded smoke entries | nothing beyond the manifest |
| `--suite ID` | every vector of that suite in the pinned definition | `--fluster` |
| `--vector SUITE#NAME` | that vector, pinned or not | `--fluster` |
| `--all` | every vector of every suite (662 here) | `--fluster` + `--confirm-large-corpus` |

`--dry-run` prints the plan (per suite, and how many of the selection carry a recorded
SHA-256) and acquires nothing, so a large selection can be inspected before it runs. `validate`
prints the same plan and **fails if the full-corpus selection does not match the declared vector
counts** — the regression for a defect where `--all` silently resolved to the 13 pinned assets.
`fetch` refuses `--all` without `--confirm-large-corpus`, so a harness cannot turn a smoke run
into a multi-gigabyte download by accident, and it stops after a bounded number of reported
failures while still exiting non-zero.

`verify --require smoke` means the smoke subset; `verify --require all` means the whole corpus
and reports per-suite missing counts, so a smoke-only cache can no longer satisfy it. Assets
acquired without a recorded SHA-256 are still covered: `fetch` writes the identities it acquired
to `<cache>/corpus-lock.json`, and `verify` checks any asset in that lock, so a tampered
unpinned vector is caught before anyone records a hash as an expectation.

## Licence policy

Nothing in this repository redistributes third-party media, and the manifest records **both** the
decision and whether usable terms were actually found:

| Suite family | terms_status | What was checked (2026-09-16) |
|---|---|---|
| ITU/JCT-VC and JVT (HEVC, AVC, FRExt) | `not-established` | ITU's Disclaimer and Copyright notice states that permission to reproduce ITU materials should be requested from `jur@itu.int` and that third-party copyrights must be respected; it grants no redistribution right, and the wftp3 working file area carries no per-file terms |
| VP9 (WebM project test data) | `not-established` | the `test_data/libvpx/` directory carries no LICENSE file (404) and the WebM project licence page covers the project's software and format, not this data |
| Generated clips | `identified` | produced locally from synthetic `lavfi` sources by this repository's scripts; the repository licence in `COPYING` (GPL-3.0-or-later) applies and no third-party media is copied |

A suite title or a storage URL is not a licence, so the validator requires a real answer:
`terms_status` is `identified` or `not-established`, `terms_checked` is a date, and `terms_note`
must say what was checked. Terms that are `not-established` must state that explicitly (or point
at the place where terms would be stated), may not be marked `redistributable`, and are recorded
as a gap instead of an assumption. `identified` terms require a named licence and a reference.

Rules the validator enforces:

- `redistributable` requires identified terms, a named licence and a reference. An assumption is
  not accepted, and there is currently no redistributable third-party media here.
- `download-on-demand-only` entries must record where the upstream terms are stated, or say that
  no terms source could be found.
- Personal recordings, private media, tokens, process arguments and extracted core dumps are
  rejected. The validator walks every string in the manifest and fails on host paths such as
  `/Users/<name>/...` or `~`.

## Smoke subset

`smoke.entries` is the bounded set a clean environment and CI acquire. Its budget
(`smoke.max_bytes`) is 4 MiB of extracted inputs, of which 1.8 MiB is currently used. The
recorded cold run downloaded 20,809,931 bytes (19.8 MiB) of upstream files, which are discarded
after extraction; `fetch` reports the downloaded volume on every run.
`smoke.required_coverage` lists the states a contributor must be able to reproduce, and the
validator fails if no smoke entry provides one:

| Coverage | Provided by |
|---|---|
| progressive, 8-bit 4:2:0 | `JVT-AVC_V1#AUD_MW_E`, `JCT-VC-HEVC_V1#AMP_A_Samsung_7`, `VP9-TEST-VECTORS#vp90-2-00-quantizer-00.webm`, generated matrices |
| interlaced | `JVT-AVC_V1#cabac_mot_fld0_full` (unimplemented syntax) |
| 10-bit 4:2:0 | `VP9-TEST-VECTORS-HIGH#vp92-2-20-10bit-yuv420.webm`, `JCT-VC-HEVC_V1#TSUNEQBD_A_MAIN10_Technicolor_2`, generated `h264-high10` |
| 4:2:2 | `VP9-TEST-VECTORS#vp91-2-04-yuv422.webm`, `JVT-FR-EXT#Hi422FR1_SONY_A` |
| long-term reference | `JCT-VC-HEVC_V1#RPS_E_qualcomm_5` (known wrong output) |
| multiple slices / tiles | generated `h264-high10` (four slices) and `vp9-matrix` (`-tile-columns 1 -tile-rows 1`) |
| multi-context interleave | generated `shared-contexts` |
| resolution change | `VP9-TEST-VECTORS#vp90-2-21-resize_inter_640x360_5_1-2.webm` and generated `frame-check-resolution-change` |
| cropped / odd dimensions | generated `frame-check-crop` (60x44) and `h264-high10` |
| sub-64 dimension | `VP9-TEST-VECTORS#vp90-2-02-size-08x08.webm` (rejected in userspace) |
| profile override | `JVT-AVC_V1#BA3_SVA_C` |
| truncated input | generated `frame-check-truncated` (corrupt by construction; must fail) |

A smoke entry is never presented as a pass. Assets that are expected to be rejected carry
`expected-rejection`, and an asset's class must agree with the classification that covers it.

## Failure classification

Each class names the vectors it covers in the manifest and a declarative selection
(`derive`, evaluated against the pinned suite and the r11 pass sets). `validate` recomputes the
selection and fails if the two disagree, if any r11 failing vector has no class, or if a vector
the r11 record passes is classified as a failure.

| Class | Vectors | Meaning |
|---|---|---|
| `unimplemented-syntax` | 57 AVC + 21 FRExt | Interlacing, MBAFF, field pictures, FMO, ASO, data partitions and related syntax are not implemented; needs work, not a workaround |
| `requires-profile-override` | 5 AVC | Progressive Baseline/Extended streams FFmpeg's profile selection rejects; decoded bit-exact with an explicit `allow_profile_mismatch` |
| `unsupported-hardware-format` | 21 FRExt + 5 VP9-high | High 4:2:2, 12-bit, 4:4:4: outside the tested hardware formats |
| `unsupported-profile` | 2 VP9 | Profile 1 streams outside the advertised profiles (0 and 2) |
| `unsupported-dimension` | 60 VP9 | Below the 64-pixel minimum; software passes them, so the input is not corrupt |
| `expected-rejection` | 2 VP9 | Resize streams that previously wedged the decoder and are now rejected in userspace without new kernel messages. Rejection is not decode support |
| `known-wrong-output` | 2 HEVC + 25 VP9 | RPS_E long-term references, FFmpeg's parameter-set parser losing pictures in one HEVC stream, 24 inter-frame resize checksums, and one scalable vector that also fails in software with a different digest |
| `capability-boundary` | 1 HEVC | Main 10 stream in an 8-bit-configured suite run; the 10-bit path must be enabled deliberately |

Corrupt fixtures are marked as such (`expected-rejection`, corrupt generated clips) and are
never treated as reference output. Suites that are deliberately absent are listed in
`excluded_suites` with a reason code, so "not tested" cannot be read as "supported".

## Generated fixtures are compared, not pinned

The generated matrices (`frame-check.sh`, `hwdownload.sh`, `h264-high10.sh`,
`shared-contexts.sh`, `vp9-matrix.sh`) build synthetic `lavfi` clips and compare decoded output
with the software decoder on the same machine. No encoder hash is committed: encoder output
depends on tool version and thread count, so a committed hash would either drift silently or
pin a toolchain instead of a behaviour. Each entry records the **producer script, its runnable
invocation** (through `tests/hwguard.py` for the hardware scripts) and a description of what it
generates — not a prose sentence presented as an exact command. The validator fails if the
producer is not a file in this repository or if the invocation does not run it. `expected_asset_hashes_committed: false` is
required and a "compared" entry that claims committed hashes is rejected. Frame counts are cross-checked against `docs/r11-pass-sets.json`
(HEVC 144/147, AVC 73/135, FRExt 27/69, VP9 216/305; 144 + 384 + 336 = 864 generated
comparisons), so the manifest cannot quietly inflate the evidence.

## Verified environment

Recorded 2026-09-16 on an Apple Silicon Mac running macOS 26.7 with an OrbStack Ubuntu 24.04
aarch64 container: Meson 1.3.2, Ninja 1.11.1, GCC 13.3.0, Clang 18.1.3, libva 1.20.0,
libdrm 2.4.125, FFmpeg 6.1.1 (`libx264`, `libx265`, `libvpx-vp9`), Python 3.12, Fluster
`f3ad284a9e6cac70dc01b02e0de71c2994181d34`.

Observations worth keeping:

- The ITU/JVT and JCT-VC archives were rejected by the distributor's request filtering when
  fetched from the macOS host and downloaded normally from the Ubuntu container. Contributors
  on macOS need a Linux environment or a mirror for those suites. The VP9 vectors downloaded
  from both.
- Fluster `input_file` may contain a directory component (for example
  `RPS_E_qualcomm_5/RPS_E_qualcomm_5.bit`). Tools that assume a flat vector directory miss those
  assets; the cache layout keeps the declared relative path.
- `corpus.py self-test` runs 31 checks with no network and no decoder, including a valid
  fixture, suite-digest drift, unlicensed redistribution, missing provenance, an unverified
  hash presented as an expectation, classification drift, an undocumented r11 failure, a
  passing vector classified as a failure, the smoke budget, missing required coverage, leaked
  host paths, suite-wide selection enumerating the pinned definition rather than the pinned
  assets, explicit selection of an unpinned vector, a licence declared without being named,
  redistribution on unidentified terms, a licence field that is only a title, a reproduction
  path that does not point at a repository script, an invocation that does not run it,
  acquisition into the Fluster layout, cache reuse, partial-cache tolerance, a missing smoke
  asset, a tampered pinned asset, a tampered unpinned asset caught by the acquisition lock,
  refusal to overwrite a pinned asset, offline failure with an actionable command, an upstream
  checksum mismatch, and lock output.

## Known gaps

- The r11 pass sets came from an unpinned Fluster checkout. Re-running the evidence must state
  which pin it used; totals are only reproducible against a stated pin.
- Per-vector SHA-256 is pinned for the smoke subset. Other vectors are verified against the
  upstream MD5 on acquisition and receive a SHA-256 through `corpus.py lock`, which a reviewer
  records deliberately. Commands are never silently rewritten into expectations, and acquired
  identities live in the cache lock as evidence rather than in the manifest.
- Usage terms for the official vectors are **not established** and are recorded that way: the ITU
  notice grants no reproduction right and the WebM test-data directory carries no licence. Nothing
  here is redistributed; anyone redistributing those bitstreams must settle terms with the
  distributor first.
- The classifications encode the documented reasons behind the r11 failures, not proof that no
  other reason applies to a specific vector. They are re-checked against the pass sets on every
  run and must be re-reviewed when a suite pin or a driver behaviour changes.
- No hardware qualification comes from this manifest. Hardware runs stay in `tests/hwguard.py`
  with an exclusive lease; see [../tests/README.md](../tests/README.md).
- `docs/ROADMAP.md` tracks the codec work behind every remaining failure class.

## Adding an asset or a suite

1. Add the suite entry with the pinned commit, `suite_file_sha256`, the Fluster download tool
   and the licence decision. Pin only suites the evidence actually uses; list the rest in
   `excluded_suites` with a reason code.
2. Classify every failure the evidence records, with an explicit `vectors` list and a `derive`
   selection. `validate` fails until the two agree and the r11 failing set is fully covered.
3. Mark smoke assets with `covers` tokens and `hash_status: "pending"`, then run `fetch` and
   `lock` and record the verified SHA-256 and byte size by hand.
4. Run `validate --fluster`, `verify --require smoke` and `self-test`, and state which parts
   were not run (hardware, other suites) in the PR.
