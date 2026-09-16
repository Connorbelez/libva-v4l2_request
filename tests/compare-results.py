#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare conformance suite results against a pinned baseline.

Exit status:
  0  hardware-green comparison or successful report
  1  comparison failed (lost pass, missing vector, fallback, incomplete, ...)
  2  malformed input or usage error
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conformance_result import (
    PRIMARY_SUITES,
    ResultError,
    SuiteResult,
    VectorResult,
    companion_record_path,
    compare,
    hardware_green,
    infer_category,
    load_path,
    log_indicates_fallback,
    parse_frame_log,
    r11_totals_match,
    redact_argv,
    report_bundle,
    from_jsonl,
    from_native,
    validate_native_schema,
    validate_suite,
)


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in [here.parent, *here.parents]:
        if (candidate / "docs" / "r11-pass-sets.json").is_file() and (
            candidate / "meson.build"
        ).is_file():
            return candidate
    raise SystemExit("could not locate repository root from tests/compare-results.py")


def select_suite(suites: dict[str, SuiteResult], name: str | None) -> SuiteResult:
    if name:
        if name in suites:
            return suites[name]
        for result in suites.values():
            if result.suite == name or result.suite_key == name:
                return result
        raise ResultError(f"suite {name!r} not in record; have {sorted(suites)}")
    if len(suites) == 1:
        return next(iter(suites.values()))
    raise ResultError("record has multiple suites; pass --suite")


def cmd_report(path: Path) -> int:
    suites = load_path(path)
    for line in report_bundle(suites):
        print(line)
    errors = r11_totals_match(suites)
    if errors:
        print("r11 reconstruction failed:", file=sys.stderr)
        for item in errors:
            print(f"- {item}", file=sys.stderr)
        return 1
    return 0


def cmd_compare(baseline_path: Path, candidate_path: Path, suite: str | None) -> int:
    try:
        baseline_bundle = load_path(baseline_path)
        candidate_bundle = load_path(candidate_path)
        baseline = select_suite(baseline_bundle, suite)
        candidate = select_suite(candidate_bundle, suite or baseline.suite_key or baseline.suite)
    except ResultError as exc:
        print(f"malformed record: {exc}", file=sys.stderr)
        return 2
    result = compare(baseline, candidate)
    json.dump(result.to_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    if not hardware_green(result):
        for error in result.errors:
            print(error, file=sys.stderr)
        return 1
    return 0


def _suite(vectors: list[VectorResult], *, mode: str = "hardware", complete: bool = True,
           abort=None, suite: str = "JCT-VC-HEVC_V1", key: str = "hevc") -> SuiteResult:
    passed = sum(1 for vector in vectors if vector.success)
    return SuiteResult(
        suite=suite,
        suite_key=key,
        mode=mode,
        complete=complete,
        abort=abort,
        passed=passed,
        total=len(vectors),
        vectors=vectors,
    )


def _pass(name: str, **kwargs) -> VectorResult:
    return VectorResult(name=name, success=True, category="hardware_pass", **kwargs)


def _fail(name: str, category: str = "decode_error", **kwargs) -> VectorResult:
    return VectorResult(name=name, success=False, category=category, **kwargs)


def run_self_test() -> int:
    root = repo_root()
    errors: list[str] = []

    def check(cond: bool, message: str) -> None:
        if not cond:
            errors.append(message)

    pass_sets = root / "docs" / "r11-pass-sets.json"
    schema = root / "docs" / "conformance-result.schema.json"
    check(pass_sets.is_file(), "missing docs/r11-pass-sets.json")
    check(schema.is_file(), "missing docs/conformance-result.schema.json")
    schema_data = json.loads(schema.read_text())
    check(schema_data.get("$defs", {}).get("vector"), "schema missing vector definition")
    check("abort" in schema_data.get("properties", {}), "schema missing abort")

    try:
        r11 = load_path(pass_sets)
    except ResultError as exc:
        errors.append(f"cannot load r11 pass sets: {exc}")
        r11 = {}
    reconstruction = r11_totals_match(r11)
    check(not reconstruction, "r11 report drift: " + "; ".join(reconstruction))
    report = "\n".join(report_bundle(r11))
    check("JCT-VC-HEVC_V1: 144/147" in report, "report missing HEVC 144/147")
    check("JVT-AVC_V1: 73/135" in report, "report missing AVC 73/135")
    check("JVT-FR-EXT: 27/69" in report, "report missing FRExt 27/69")
    check("VP9-TEST-VECTORS: 216/305" in report, "report missing VP9 216/305")

    for key in PRIMARY_SUITES:
        identity = compare(r11[key], r11[key])
        check(hardware_green(identity), f"{key} identity compare is not hardware-green")

    hevc = r11["hevc"]
    swapped_vectors = [copy.deepcopy(vector) for vector in hevc.vectors]
    pass_idx = next(i for i, vector in enumerate(swapped_vectors) if vector.success)
    fail_idx = next(i for i, vector in enumerate(swapped_vectors) if not vector.success)
    lost_name = swapped_vectors[pass_idx].name
    gained_name = swapped_vectors[fail_idx].name
    swapped_vectors[pass_idx].success = False
    swapped_vectors[pass_idx].category = "decode_error"
    swapped_vectors[fail_idx].success = True
    swapped_vectors[fail_idx].category = "hardware_pass"
    swapped = copy.deepcopy(hevc)
    swapped.vectors = swapped_vectors
    swapped.passed = sum(1 for vector in swapped.vectors if vector.success)
    swapped_compare = compare(hevc, swapped)
    check(swapped.passed == hevc.passed, "swapped fixture must keep the same total")
    check(lost_name in swapped_compare.lost_passes, "swapped fixture did not report lost pass")
    check(gained_name in swapped_compare.new_passes, "swapped fixture did not report new pass")
    check(not hardware_green(swapped_compare), "swapped same-count pass set was green")

    truncated_vectors = [copy.deepcopy(vector) for vector in hevc.vectors[:-3]]
    truncated = _suite(truncated_vectors, complete=False, abort={"kind": "timeout", "vector": "tail"})
    truncated.suite = hevc.suite
    truncated.suite_key = hevc.suite_key
    truncated_compare = compare(hevc, truncated)
    check(truncated_compare.missing_vectors, "truncated run did not report missing vectors")
    check(not hardware_green(truncated_compare), "truncated run was green")

    fallback = _suite([
        _pass("A"),
        VectorResult(name="B", success=True, category="software_fallback"),
    ])
    check(validate_suite(fallback), "software fallback success must be invalid")
    fallback_valid = _suite([
        _pass("A"),
        _fail("B", "software_fallback"),
    ])
    fallback_compare = compare(_suite([_pass("A"), _pass("B")]), fallback_valid)
    check(not hardware_green(fallback_compare), "software fallback candidate was green")
    check(any("fallback" in item for item in fallback_compare.errors),
          "fallback candidate missing fallback error")

    software = _suite([_pass("A"), _fail("B")], mode="software")
    # hardware_pass in software mode is invalid; rebuild as software_pass
    software.vectors[0].category = "software_pass"
    software_compare = compare(_suite([_pass("A"), _fail("B")]), software)
    check(not hardware_green(software_compare), "software-mode candidate was green hardware")

    try:
        from_native({"schema_version": "1.0.0", "kind": "nope"})
        errors.append("native loader accepted a bad kind")
    except ResultError:
        pass

    try:
        from_jsonl("{not json")
        errors.append("jsonl loader accepted malformed JSON")
    except ResultError:
        pass

    try:
        json.loads("{")
        malformed_ok = False
    except json.JSONDecodeError:
        malformed_ok = True
    check(malformed_ok, "control: json.loads should reject '{'")
    try:
        load_malformed = Path(root / "tests" / "fixtures" / "conformance-result" / "fail-malformed.json")
        if load_malformed.is_file():
            load_path_mod = __import__("conformance_result", fromlist=["load_path"]).load_path
            load_path_mod(load_malformed)
            errors.append("malformed fixture did not raise")
        else:
            errors.append("missing fail-malformed.json fixture")
    except ResultError:
        pass

    timeout_jsonl = "\n".join([
        json.dumps({"event": "start", "suite": "JCT-VC-HEVC_V1", "decoder": "vaapi"}),
        json.dumps({"event": "result", "vector": "A", "success": True, "expected": "aa",
                    "actual": "aa", "frames": 1}),
        json.dumps({"event": "timeout", "vector": "B"}),
    ])
    timeout_result = from_jsonl(timeout_jsonl)
    check(not timeout_result.complete, "timeout jsonl marked complete")
    check(timeout_result.abort and timeout_result.abort["kind"] == "timeout",
          "timeout jsonl missing abort")
    timeout_compare = compare(_suite([_pass("A"), _pass("B")]), timeout_result)
    check(not hardware_green(timeout_compare), "timeout candidate was green")

    frames, digest = parse_frame_log(
        "frame 0 640x360 yuv420p " + "a" * 32 + "\n"
        "frame 1 1920x1080 yuv420p " + "b" * 32 + "\n"
        "MD5=" + "c" * 32 + "\n"
    )
    check(len(frames) == 2, "frame log should parse two frames")
    check(frames[0].size() == "640x360" and frames[1].size() == "1920x1080",
          "resolution change not represented")
    check(digest == "c" * 32, "stream MD5 not parsed")
    check(frames[0].index == 0 and frames[1].index == 1, "output ordering lost")

    left = _pass(
        "clip",
        actual_md5="c" * 32,
        frames=2,
        native_sizes=["640x360", "1920x1080"],
        frame_summaries=frames,
    )
    right_frames, _ = parse_frame_log(
        "frame 0 640x360 yuv420p " + "a" * 32 + "\n"
        "frame 1 1920x1080 yuv420p " + "d" * 32 + "\n"
        "MD5=" + "e" * 32 + "\n"
    )
    right = _fail(
        "clip",
        "checksum_mismatch",
        actual_md5="e" * 32,
        expected_md5="c" * 32,
        frames=2,
        native_sizes=["640x360", "1920x1080"],
        frame_summaries=right_frames,
    )
    frame_compare = compare(_suite([left]), _suite([right]))
    check(frame_compare.first_differing_frames, "checksum mismatch missing first differing frame")
    check(frame_compare.first_differing_frames[0].get("index") == 1,
          "first differing frame should be index 1")
    check(not hardware_green(frame_compare), "frame mismatch was green")

    redacted = redact_argv([
        "frame-check",
        "vaapi",
        "/home/chrisk/src/fluster/resources/clip.bit",
        "https://example.test/secret",
        "yuv420p",
    ])
    check(all("chrisk" not in part and "https://" not in part for part in redacted),
          "publishable argv still contains path or URL")
    check(redacted[0] == "frame-check" and redacted[-1] == "yuv420p",
          "redaction removed non-sensitive argv")

    companion = {
        "date": "2026-09-15",
        "driver_version": "1.3.r11",
        "machine": "Apple M1 T8103 / MacBookPro17,1",
        "kernel_package": "linux-asahi 7.1.13.asahi3-1",
        "suites": {
            "hevc": {
                "passed": 2,
                "total": 3,
                "passing_frames": 3,
                "high10": "off",
                "profile_mismatch": False,
                "results": [
                    {"vector": "KEEP_A", "success": True, "expected": "aa", "actual": "aa",
                     "frames": 1, "returncode": 0},
                    {"vector": "KEEP_B", "success": True, "expected": "bb", "actual": "bb",
                     "frames": 2, "returncode": 0},
                    {"vector": "FAIL_C", "success": False, "expected": "cc", "actual": None,
                     "frames": 0, "returncode": 1},
                ],
            }
        },
    }
    from conformance_result import from_companion_suite
    imported = from_companion_suite("hevc", companion["suites"]["hevc"], companion)
    check(imported.vectors[0].expected_md5 == "aa", "companion import rewrote expected hash")
    check(imported.vectors[2].actual_md5 is None, "companion import rewrote failing actual")
    check(imported.provenance.get("rewritten") is False, "import must not rewrite evidence")
    check(imported.passed == 2 and imported.total == 3, "companion totals not preserved")

    native = from_native(imported.to_dict())
    check(native.passing_names() == imported.passing_names(), "native round-trip changed pass set")
    schema_errors = validate_native_schema(imported.to_dict())
    check(not schema_errors, "imported native record failed schema: " + "; ".join(schema_errors))
    try:
        from_native({**imported.to_dict(), "suite": ""})
        errors.append("empty suite name was accepted")
    except ResultError:
        pass

    hide = copy.deepcopy(hevc)
    hide.subset = {"rationale": "hide denominator", "selected": hide.names()}
    hide.total = hide.passed
    check(validate_suite(hide), "subset 144/144 record must be invalid")
    shrink = copy.deepcopy(hevc)
    shrink.vectors = [vector for vector in hevc.vectors if vector.success]
    shrink.passed = len(shrink.vectors)
    shrink.total = len(shrink.vectors)
    shrink.subset = {"rationale": "eligible only", "selected": shrink.names()}
    shrink_errors = validate_suite(shrink)
    check(not shrink_errors, "complete 144-vector subset record should validate counts")
    shrink_compare = compare(hevc, shrink)
    check(not hardware_green(shrink_compare), "144/144 subset candidate was hardware-green")
    check(any("subset" in item or "total changed" in item for item in shrink_compare.errors),
          "subset denominator hide missing error")

    pin_mismatch = copy.deepcopy(hevc.vectors[0])
    pin_mismatch.success = False
    pin_mismatch.category = "checksum_mismatch"
    pin_mismatch.expected_md5 = "aa" * 16
    pin_mismatch.actual_md5 = "bb" * 16
    pin_mismatch.frame_summaries = frames
    pin_mismatch.native_sizes = ["640x360", "1920x1080"]
    pin_candidate = copy.deepcopy(hevc)
    pin_candidate.vectors = [copy.deepcopy(vector) for vector in hevc.vectors]
    pin_candidate.vectors[0] = pin_mismatch
    pin_candidate.passed = sum(1 for vector in pin_candidate.vectors if vector.success)
    pin_compare = compare(hevc, pin_candidate)
    check(pin_compare.first_differing_frames, "r11 pin vs checksum mismatch missing first-frame evidence")
    check(not hardware_green(pin_compare), "checksum mismatch against r11 pin was green")

    check(log_indicates_fallback("Failed setup for format vaapi: hwaccel initialisation returned error."),
          "hwaccel init failure should count as software fallback")
    check(infer_category("hardware", False, None, "aa", fallback=True) == "software_fallback",
          "infer_category fallback=True did not return software_fallback")

    fixtures = root / "tests" / "fixtures" / "conformance-result"
    manifest = json.loads((fixtures / "manifest.json").read_text())
    load = __import__("conformance_result", fromlist=["load_path"]).load_path
    for entry in manifest:
        path = fixtures / entry["file"]
        expect = entry["expect"]
        try:
            if expect == "load-error":
                try:
                    load(path)
                    errors.append(f"{path.name} should fail to load")
                except ResultError:
                    pass
                continue
            loaded = load(path)
            result = next(iter(loaded.values()))
            if expect == "compare-fail":
                other = load(fixtures / entry["against"])
                comparison = compare(next(iter(other.values())), result)
                if hardware_green(comparison):
                    errors.append(f"{path.name} should not be hardware-green")
                needle = entry.get("error", "")
                blob = " ".join(comparison.errors + comparison.lost_passes)
                if needle and needle not in blob:
                    errors.append(f"{path.name} missing {needle!r} in {blob}")
            elif expect == "compare-pass":
                other = load(fixtures / entry["against"])
                comparison = compare(next(iter(other.values())), result)
                if not hardware_green(comparison):
                    errors.append(f"{path.name} should be hardware-green: {comparison.errors}")
        except ResultError as exc:
            if expect != "load-error":
                errors.append(f"{path.name} unexpected load error: {exc}")

    for fixture_file in fixtures.glob("*"):
        if fixture_file.suffix in {".json", ".jsonl"}:
            text = fixture_file.read_text()
            check("/home/" not in text and "/Users/" not in text,
                  f"{fixture_file.name} contains a private home path")

    for fixture_file in fixtures.glob("pass-*.json"):
        native_errors = validate_native_schema(json.loads(fixture_file.read_text()))
        check(not native_errors, f"{fixture_file.name} schema: " + "; ".join(native_errors))

    companion_record = companion_record_path(root)
    if companion_record is not None:
        full = load(companion_record)
        drift = r11_totals_match(full)
        check(not drift, "full companion r11 drift: " + "; ".join(drift))
        for key in PRIMARY_SUITES:
            check(
                set(full[key].passing_names()) == set(r11[key].passing_names()),
                f"{key} companion pass set disagrees with pinned names",
            )

    if errors:
        print(f"{len(errors)} conformance-result self-test error(s):", file=sys.stderr)
        print("\n".join(f"- {item}" for item in errors), file=sys.stderr)
        return 1
    print(
        "conformance-result ok: r11 144/147 73/135 27/69 216/305, "
        "swapped/truncated/fallback/malformed/timeout fixtures fail as required"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, help="pinned baseline record")
    parser.add_argument("--candidate", type=Path, help="candidate record")
    parser.add_argument("--suite", help="suite key or official name")
    parser.add_argument("--report", type=Path, help="print reconstructed suite totals")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    try:
        if args.self_test:
            return run_self_test()
        if args.report:
            return cmd_report(args.report)
        if args.baseline and args.candidate:
            return cmd_compare(args.baseline, args.candidate, args.suite)
        parser.error("use --self-test, --report FILE, or --baseline FILE --candidate FILE")
    except ResultError as exc:
        print(f"malformed record: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
