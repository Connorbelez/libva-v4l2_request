#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Load, validate and compare VA-API conformance suite results.

Hardware and software modes are distinct. Software fallback, timeouts, aborts
and malformed records cannot be reported as a green hardware result. Pass sets
are compared by vector name, not by totals alone.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "1.0.0"
KIND = "conformance-suite-result"
MODES = ("hardware", "software")
CATEGORIES = (
    "hardware_pass",
    "software_pass",
    "software_fallback",
    "checksum_mismatch",
    "decode_error",
    "timeout",
    "missing",
    "incomplete",
)
PASS_CATEGORIES = {"hardware_pass", "software_pass"}
FRAME_LINE = re.compile(
    r"^frame (\d+) (\d+)x(\d+) ([A-Za-z0-9_]+) ([0-9a-f]{32})$"
)
MD5_LINE = re.compile(r"^MD5=([0-9a-f]{32})$")
REDACT_PATH = re.compile(
    r"(https?://\S+)|(/\S+)|([A-Za-z]:\\[^\s]+)",
)
PRIMARY_SUITES = {
    "hevc": ("JCT-VC-HEVC_V1", 144, 147),
    "avc": ("JVT-AVC_V1", 73, 135),
    "frext": ("JVT-FR-EXT", 27, 69),
    "vp9": ("VP9-TEST-VECTORS", 216, 305),
}
SUITE_KEYS = {
    "JCT-VC-HEVC_V1": "hevc",
    "JVT-AVC_V1": "avc",
    "JVT-FR-EXT": "frext",
    "VP9-TEST-VECTORS": "vp9",
    "VP9-TEST-VECTORS-HIGH-10BIT-420": "vp9-high10",
    "JVT-AVC_V1-profile-mismatch-overrides": "overrides",
}


class ResultError(ValueError):
    """Malformed or unusable conformance record."""


@dataclass
class FrameSummary:
    index: int
    width: int
    height: int
    md5: str
    format: str = ""

    def size(self) -> str:
        return f"{self.width}x{self.height}"

    def to_dict(self) -> dict[str, Any]:
        data = {
            "index": self.index,
            "width": self.width,
            "height": self.height,
            "md5": self.md5,
        }
        if self.format:
            data["format"] = self.format
        return data


@dataclass
class VectorResult:
    name: str
    success: bool
    category: str
    expected_md5: str | None = None
    actual_md5: str | None = None
    frames: int = 0
    native_sizes: list[str] = field(default_factory=list)
    frame_summaries: list[FrameSummary] = field(default_factory=list)
    first_differing_frame: dict[str, Any] | None = None
    returncode: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "success": self.success,
            "category": self.category,
            "expected_md5": self.expected_md5,
            "actual_md5": self.actual_md5,
            "frames": self.frames,
            "native_sizes": list(self.native_sizes),
        }
        if self.frame_summaries:
            data["frame_summaries"] = [frame.to_dict() for frame in self.frame_summaries]
        if self.first_differing_frame is not None:
            data["first_differing_frame"] = self.first_differing_frame
        if self.returncode is not None:
            data["returncode"] = self.returncode
        return data


@dataclass
class SuiteResult:
    suite: str
    mode: str
    complete: bool
    passed: int
    total: int
    vectors: list[VectorResult]
    suite_key: str = ""
    abort: dict[str, Any] | None = None
    passing_frames: int = 0
    high10: str = "off"
    profile_mismatch: bool = False
    subset: dict[str, Any] | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    command: dict[str, Any] = field(default_factory=dict)

    def passing_names(self) -> list[str]:
        return [vector.name for vector in self.vectors if vector.success]

    def names(self) -> list[str]:
        return [vector.name for vector in self.vectors]

    def by_name(self) -> dict[str, VectorResult]:
        return {vector.name: vector for vector in self.vectors}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "suite": self.suite,
            "suite_key": self.suite_key,
            "mode": self.mode,
            "complete": self.complete,
            "abort": self.abort,
            "passed": self.passed,
            "total": self.total,
            "passing_frames": self.passing_frames,
            "high10": self.high10,
            "profile_mismatch": self.profile_mismatch,
            "subset": self.subset,
            "provenance": self.provenance,
            "command": self.command,
            "vectors": [vector.to_dict() for vector in self.vectors],
        }


@dataclass
class Comparison:
    baseline: SuiteResult
    candidate: SuiteResult
    lost_passes: list[str]
    new_passes: list[str]
    missing_vectors: list[str]
    extra_vectors: list[str]
    category_changes: list[dict[str, str]]
    first_differing_frames: list[dict[str, Any]]
    errors: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "baseline_suite": self.baseline.suite,
            "candidate_suite": self.candidate.suite,
            "baseline_total": f"{self.baseline.passed}/{self.baseline.total}",
            "candidate_total": f"{self.candidate.passed}/{self.candidate.total}",
            "lost_passes": self.lost_passes,
            "new_passes": self.new_passes,
            "missing_vectors": self.missing_vectors,
            "extra_vectors": self.extra_vectors,
            "category_changes": self.category_changes,
            "first_differing_frames": self.first_differing_frames,
            "errors": self.errors,
        }


def redact_text(value: str) -> str:
    if not value:
        return value
    redacted = REDACT_PATH.sub("<redacted>", value)
    return redacted


def redact_argv(argv: Iterable[str]) -> list[str]:
    return [redact_text(part) for part in argv]


def parse_frame_log(text: str) -> tuple[list[FrameSummary], str | None]:
    frames: list[FrameSummary] = []
    digest = None
    for line in text.splitlines():
        match = FRAME_LINE.match(line.strip())
        if match:
            frames.append(
                FrameSummary(
                    index=int(match.group(1)),
                    width=int(match.group(2)),
                    height=int(match.group(3)),
                    format=match.group(4),
                    md5=match.group(5),
                )
            )
            continue
        match = MD5_LINE.match(line.strip())
        if match:
            digest = match.group(1)
    return frames, digest


def infer_category(mode: str, success: bool, actual: str | None, expected: str | None,
                   *, timeout: bool = False, fallback: bool = False) -> str:
    if timeout:
        return "timeout"
    if fallback:
        return "software_fallback"
    if success:
        return "hardware_pass" if mode == "hardware" else "software_pass"
    if actual and expected and actual != expected:
        return "checksum_mismatch"
    return "decode_error"


def first_frame_diff(baseline: VectorResult, candidate: VectorResult) -> dict[str, Any] | None:
    if not baseline.frame_summaries or not candidate.frame_summaries:
        if (baseline.actual_md5 and candidate.actual_md5
                and baseline.actual_md5 != candidate.actual_md5):
            return {
                "vector": candidate.name,
                "reason": "stream_md5",
                "baseline_md5": baseline.actual_md5,
                "candidate_md5": candidate.actual_md5,
            }
        return None
    limit = min(len(baseline.frame_summaries), len(candidate.frame_summaries))
    for index in range(limit):
        left = baseline.frame_summaries[index]
        right = candidate.frame_summaries[index]
        if left.md5 != right.md5 or left.width != right.width or left.height != right.height:
            return {
                "vector": candidate.name,
                "index": left.index,
                "baseline": left.to_dict(),
                "candidate": right.to_dict(),
            }
    if len(baseline.frame_summaries) != len(candidate.frame_summaries):
        return {
            "vector": candidate.name,
            "reason": "frame_count",
            "baseline_frames": len(baseline.frame_summaries),
            "candidate_frames": len(candidate.frame_summaries),
        }
    return None


def validate_suite(result: SuiteResult) -> list[str]:
    errors: list[str] = []
    if result.mode not in MODES:
        errors.append(f"invalid mode {result.mode!r}")
    if result.total < 1:
        errors.append("total must be >= 1")
    if result.passed < 0 or result.passed > result.total:
        errors.append(f"invalid passed/total {result.passed}/{result.total}")
    names = result.names()
    if len(names) != len(set(names)):
        errors.append("duplicate vector names")
    if result.complete and not result.subset and len(result.vectors) != result.total:
        errors.append(
            f"vector count {len(result.vectors)} != declared total {result.total}"
        )
    counted = sum(1 for vector in result.vectors if vector.success)
    if counted != result.passed:
        errors.append(f"passed {result.passed} != successful vectors {counted}")
    for vector in result.vectors:
        if vector.category not in CATEGORIES:
            errors.append(f"{vector.name}: invalid category {vector.category!r}")
        if vector.success and vector.category not in PASS_CATEGORIES:
            errors.append(f"{vector.name}: success with non-pass category {vector.category}")
        if vector.success and result.mode == "hardware" and vector.category != "hardware_pass":
            errors.append(
                f"{vector.name}: hardware suite cannot count {vector.category} as success"
            )
        if vector.category == "software_fallback" and vector.success:
            errors.append(f"{vector.name}: software fallback cannot be a pass")
        if vector.success and result.mode == "software" and vector.category == "hardware_pass":
            errors.append(f"{vector.name}: software mode cannot use hardware_pass")
    if result.complete and result.abort:
        errors.append("complete run cannot also record an abort")
    if not result.complete and not result.abort:
        errors.append("incomplete run must record abort")
    return errors


def require_valid(result: SuiteResult) -> SuiteResult:
    errors = validate_suite(result)
    if errors:
        raise ResultError("; ".join(errors))
    return result


def _vector_from_mapping(raw: dict[str, Any], mode: str) -> VectorResult:
    name = raw.get("name") or raw.get("vector")
    if not name:
        raise ResultError("vector missing name")
    success = bool(raw.get("success"))
    expected = raw.get("expected_md5", raw.get("expected"))
    actual = raw.get("actual_md5", raw.get("actual"))
    timeout = raw.get("category") == "timeout" or raw.get("timeout") is True
    fallback = raw.get("category") == "software_fallback" or raw.get("software_fallback") is True
    category = raw.get("category") or infer_category(
        mode, success, actual, expected, timeout=timeout, fallback=fallback
    )
    frames_raw = raw.get("frame_summaries") or []
    summaries = [
        FrameSummary(
            index=int(frame["index"]),
            width=int(frame["width"]),
            height=int(frame["height"]),
            md5=str(frame["md5"]),
            format=str(frame.get("format") or ""),
        )
        for frame in frames_raw
    ]
    sizes = list(raw.get("native_sizes") or [])
    if not sizes and summaries:
        seen: list[str] = []
        for frame in summaries:
            size = frame.size()
            if size not in seen:
                seen.append(size)
        sizes = seen
    return VectorResult(
        name=str(name),
        success=success,
        category=str(category),
        expected_md5=expected,
        actual_md5=actual,
        frames=int(raw.get("frames") or (len(summaries) if summaries else 0)),
        native_sizes=sizes,
        frame_summaries=summaries,
        first_differing_frame=raw.get("first_differing_frame"),
        returncode=raw.get("returncode"),
    )


def from_native(data: dict[str, Any]) -> SuiteResult:
    if data.get("schema_version") != SCHEMA_VERSION or data.get("kind") != KIND:
        raise ResultError("not a native conformance-suite-result v1.0.0")
    mode = data.get("mode")
    if mode not in MODES:
        raise ResultError("native record missing hardware/software mode")
    vectors = [_vector_from_mapping(item, mode) for item in data.get("vectors") or []]
    result = SuiteResult(
        suite=str(data.get("suite") or ""),
        suite_key=str(data.get("suite_key") or SUITE_KEYS.get(data.get("suite"), "")),
        mode=mode,
        complete=bool(data.get("complete")),
        abort=data.get("abort"),
        passed=int(data.get("passed") or 0),
        total=int(data.get("total") or 0),
        vectors=vectors,
        passing_frames=int(data.get("passing_frames") or 0),
        high10=str(data.get("high10") or "off"),
        profile_mismatch=bool(data.get("profile_mismatch")),
        subset=data.get("subset"),
        provenance=dict(data.get("provenance") or {}),
        command=dict(data.get("command") or {}),
    )
    return require_valid(result)


def from_companion_suite(key: str, suite: dict[str, Any], bundle: dict[str, Any]) -> SuiteResult:
    official, _, _ = PRIMARY_SUITES.get(key, (key, 0, 0))
    if key in PRIMARY_SUITES:
        official = PRIMARY_SUITES[key][0]
    elif key == "vp9-high10":
        official = "VP9-TEST-VECTORS-HIGH-10BIT-420"
    elif key == "overrides":
        official = "JVT-AVC_V1-profile-mismatch-overrides"
    mode = "hardware"
    vectors = []
    for item in suite.get("results") or []:
        vectors.append(_vector_from_mapping(item, mode))
    provenance = {
        "date": bundle.get("date"),
        "machine": bundle.get("machine"),
        "kernel_package": bundle.get("kernel_package"),
        "driver_version": bundle.get("driver_version"),
        "driver_commit": bundle.get("driver_commit"),
        "implementation_commit": bundle.get("implementation_commit"),
        "evidence_record": "companion-validation-json",
        "imported": True,
        "rewritten": False,
    }
    result = SuiteResult(
        suite=official,
        suite_key=key,
        mode=mode,
        complete=True,
        abort=None,
        passed=int(suite.get("passed") or 0),
        total=int(suite.get("total") or 0),
        vectors=vectors,
        passing_frames=int(suite.get("passing_frames") or 0),
        high10=str(suite.get("high10") or "off"),
        profile_mismatch=bool(suite.get("profile_mismatch")),
        provenance=provenance,
        command={"redacted": True, "note": "imported companion record; original evidence unchanged"},
    )
    return require_valid(result)


def from_pass_sets_suite(key: str, suite: dict[str, Any], bundle: dict[str, Any]) -> SuiteResult:
    official = PRIMARY_SUITES.get(key, (key, suite.get("passed"), suite.get("total")))[0]
    if key == "vp9-high10":
        official = "VP9-TEST-VECTORS-HIGH-10BIT-420"
    elif key == "overrides":
        official = "JVT-AVC_V1-profile-mismatch-overrides"
    vectors = [
        VectorResult(name=name, success=True, category="hardware_pass")
        for name in suite.get("passing_vectors") or []
    ]
    vectors.extend(
        VectorResult(name=name, success=False, category="decode_error")
        for name in suite.get("failing_vectors") or []
    )
    source = bundle.get("source") or {}
    result = SuiteResult(
        suite=official,
        suite_key=key,
        mode="hardware",
        complete=True,
        abort=None,
        passed=int(suite.get("passed") or 0),
        total=int(suite.get("total") or 0),
        vectors=vectors,
        passing_frames=int(suite.get("passing_frames") or 0),
        high10=str(suite.get("high10") or "off"),
        profile_mismatch=bool(suite.get("profile_mismatch")),
        provenance={
            "date": source.get("date"),
            "machine": source.get("machine"),
            "kernel_package": source.get("kernel_package"),
            "driver_version": source.get("driver_version"),
            "evidence_record": source.get("record"),
            "imported": True,
            "rewritten": False,
        },
        command={"redacted": True, "note": "pinned r11 pass-set names; original evidence unchanged"},
    )
    return require_valid(result)


def from_jsonl(text: str) -> SuiteResult:
    events = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ResultError(f"malformed jsonl line {line_no}: {exc}") from exc
    start = next((event for event in events if event.get("event") == "start"), None)
    if not start:
        raise ResultError("jsonl missing start event")
    decoder = start.get("decoder")
    if decoder == "vaapi":
        mode = "hardware"
    elif decoder == "software":
        mode = "software"
    else:
        raise ResultError("jsonl start missing decoder vaapi|software")
    abort = None
    complete = False
    vectors: list[VectorResult] = []
    for event in events:
        kind = event.get("event")
        if kind == "timeout":
            abort = {"kind": "timeout", "vector": event.get("vector")}
            vectors.append(
                VectorResult(
                    name=str(event.get("vector") or "unknown"),
                    success=False,
                    category="timeout",
                )
            )
        elif kind == "result":
            vectors.append(_vector_from_mapping(event, mode))
        elif kind == "finished":
            complete = abort is None
    passed = sum(1 for vector in vectors if vector.success)
    total = int(
        next((event.get("total") for event in events if event.get("event") == "finished"),
             len(vectors))
        or len(vectors)
        or 1
    )
    if not complete and abort is None:
        abort = {"kind": "user", "vector": vectors[-1].name if vectors else ""}
        total = max(total, len(vectors), 1)
    result = SuiteResult(
        suite=str(start.get("suite") or "unknown"),
        suite_key=SUITE_KEYS.get(start.get("suite"), ""),
        mode=mode,
        complete=complete,
        abort=abort,
        passed=passed,
        total=total,
        vectors=vectors,
        high10=str(start.get("high10") or "off"),
        profile_mismatch=bool(start.get("profile_mismatch")),
        command={
            "redacted": True,
            "driver": redact_text(str(start.get("driver") or "")),
        },
    )
    return require_valid(result)


def detect_kind(data: Any) -> str:
    if isinstance(data, dict) and data.get("kind") == KIND:
        return "native"
    if isinstance(data, dict) and "suites" in data and "source" in data:
        return "pass-sets"
    if isinstance(data, dict) and "suites" in data and isinstance(data["suites"], dict):
        first = next(iter(data["suites"].values()), None)
        if isinstance(first, dict) and "results" in first:
            return "companion"
        if isinstance(first, dict) and "passing_vectors" in first:
            return "pass-sets"
    raise ResultError("unrecognised conformance record")


def load_path(path: Path) -> dict[str, SuiteResult]:
    try:
        text = path.read_text()
    except OSError as exc:
        raise ResultError(f"cannot read {path}: {exc}") from exc
    if path.suffix == ".jsonl" or text.lstrip().startswith('{"event"'):
        result = from_jsonl(text)
        key = result.suite_key or result.suite
        return {key: result}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResultError(f"malformed JSON in {path}: {exc}") from exc
    kind = detect_kind(data)
    if kind == "native":
        result = from_native(data)
        return {result.suite_key or result.suite: result}
    suites = data.get("suites") or {}
    loaded: dict[str, SuiteResult] = {}
    for key, suite in suites.items():
        if kind == "companion":
            loaded[key] = from_companion_suite(key, suite, data)
        else:
            loaded[key] = from_pass_sets_suite(key, suite, data)
    if not loaded:
        raise ResultError(f"{path} contains no suites")
    return loaded


def compare(baseline: SuiteResult, candidate: SuiteResult) -> Comparison:
    errors: list[str] = []
    errors.extend(f"baseline: {item}" for item in validate_suite(baseline))
    errors.extend(f"candidate: {item}" for item in validate_suite(candidate))
    if candidate.mode != "hardware":
        errors.append("candidate mode is not hardware; software cannot be a green hardware result")
    if baseline.mode == "hardware" and candidate.mode != "hardware":
        errors.append("hardware baseline compared against non-hardware candidate")
    if not candidate.complete:
        errors.append("candidate run is incomplete")
    if candidate.abort:
        errors.append(f"candidate aborted: {candidate.abort}")
    fallback = [
        vector.name for vector in candidate.vectors
        if vector.category == "software_fallback" or (
            candidate.mode == "hardware" and vector.category == "software_pass"
        )
    ]
    if fallback:
        errors.append("software fallback cannot produce a green hardware result: "
                      + ", ".join(fallback))
    if any(vector.success and vector.category != "hardware_pass" for vector in candidate.vectors):
        errors.append("candidate counted a non-hardware success in a hardware comparison")

    base_pass = set(baseline.passing_names())
    cand_pass = set(candidate.passing_names())
    lost = sorted(base_pass - cand_pass)
    new = sorted(cand_pass - base_pass)
    missing = sorted(set(baseline.names()) - set(candidate.names()))
    extra = sorted(set(candidate.names()) - set(baseline.names()))
    if lost:
        errors.append(
            f"lost {len(lost)} passing vector(s) while totals are "
            f"{candidate.passed}/{candidate.total}: " + ", ".join(lost)
        )
    if missing:
        errors.append("missing vectors: " + ", ".join(missing))
    if not candidate.subset and candidate.total != baseline.total and not missing:
        errors.append(
            f"full-suite total changed {baseline.passed}/{baseline.total} -> "
            f"{candidate.passed}/{candidate.total}"
        )

    category_changes = []
    diffs = []
    base_map = baseline.by_name()
    cand_map = candidate.by_name()
    for name in sorted(set(base_map) & set(cand_map)):
        left = base_map[name]
        right = cand_map[name]
        if left.category != right.category:
            category_changes.append(
                {"vector": name, "baseline": left.category, "candidate": right.category}
            )
        diff = first_frame_diff(left, right)
        if diff:
            diffs.append(diff)
            right.first_differing_frame = diff

    return Comparison(
        baseline=baseline,
        candidate=candidate,
        lost_passes=lost,
        new_passes=new,
        missing_vectors=missing,
        extra_vectors=extra,
        category_changes=category_changes,
        first_differing_frames=diffs,
        errors=errors,
    )


def hardware_green(comparison: Comparison) -> bool:
    return comparison.ok and comparison.candidate.mode == "hardware" and comparison.candidate.complete


def report_bundle(suites: dict[str, SuiteResult]) -> list[str]:
    lines = []
    for key, (official, passed, total) in PRIMARY_SUITES.items():
        result = suites.get(key)
        if result is None:
            lines.append(f"{official}: MISSING")
            continue
        lines.append(f"{official}: {result.passed}/{result.total}")
        if result.passed != passed or result.total != total:
            lines.append(
                f"  ERROR expected {passed}/{total} from r11, got {result.passed}/{result.total}"
            )
        if len(result.passing_names()) != result.passed:
            lines.append("  ERROR pass-set size disagrees with passed count")
    return lines


def r11_totals_match(suites: dict[str, SuiteResult]) -> list[str]:
    errors = []
    for key, (official, passed, total) in PRIMARY_SUITES.items():
        result = suites.get(key)
        if result is None:
            errors.append(f"missing primary suite {key} ({official})")
            continue
        if result.passed != passed or result.total != total:
            errors.append(
                f"{official} is {result.passed}/{result.total}, r11 is {passed}/{total}"
            )
        if not result.complete or result.mode != "hardware":
            errors.append(f"{official} is not a complete hardware record")
    return errors


def write_summary(path: Path, result: SuiteResult) -> None:
    require_valid(result)
    path.write_text(json.dumps(result.to_dict(), indent=2) + "\n")
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
