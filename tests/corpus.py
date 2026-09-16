#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Acquire and verify the codec regression corpus described by tests/corpus/manifest.json.

The manifest pins where every regression input comes from, under which licence terms
it may be used, and which hash identifies it. This tool never guesses:

  self-test   hermetically exercise the validators, the negative cases and the
              acquisition/verification paths against checked-in fixtures (no network).
  validate    check manifest schema, internal consistency, licence policy, the pinned
              Fluster suite digests, the derived failure classifications and the r11
              pass/fail sets. Offline.
  verify      validate, then check the local cache: every asset present must match its
              pinned hash and every requested asset must be present. Offline.
  fetch       download selected assets on demand, verify the upstream checksum before
              accepting a file, and keep the Fluster <suite>/<vector>/<input_file>
              layout so the cache can be passed to tests/conformance.py directly.
  lock        print or write the SHA-256 of acquired assets. The manifest is never
              edited automatically: a hash only becomes an expectation when a
              reviewer records it.

Examples:

  python3 tests/corpus.py self-test
  python3 tests/corpus.py validate --fluster /path/to/fluster
  python3 tests/corpus.py fetch --fluster /path/to/fluster --cache ~/.cache/libva-corpus --smoke
  python3 tests/corpus.py verify --cache ~/.cache/libva-corpus --require smoke
  python3 tests/corpus.py lock --cache ~/.cache/libva-corpus

A cache produced by ``fetch`` is a Fluster resources directory. The smoke subset is
bounded by ``smoke.max_bytes`` in the manifest; ``--all`` refuses to run without an
explicit acknowledgement because the full corpus is large.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_MANIFEST = HERE / "corpus" / "manifest.json"
DEFAULT_PASS_SETS = ROOT / "docs" / "r11-pass-sets.json"
DEFAULT_FIXTURES = HERE / "fixtures" / "corpus"
DEFAULT_CACHE = Path(os.environ.get("LIBVA_V4L2_CORPUS_CACHE", Path.home() / ".cache" / "libva-corpus"))

SCHEMA_VERSION = "1.0.0"
MANIFEST_ID = "libva-v4l2_request-corpus"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MD5_RE = re.compile(r"^[0-9a-f]{32}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ASSET_REF_RE = re.compile(r"^(?P<suite>[^#]+)#(?P<vector>.+)$")
SIZE_RE = re.compile(r"-size-(\d+)x(\d+)")

TERMS_STATUS_VALUES = ["identified", "not-established"]
REDISTRIBUTION_VALUES = ["generated-locally", "download-on-demand-only", "redistributable"]
ASSET_CLASSES = [
    "pass",
    "expected-rejection",
    "known-wrong-output",
    "unsupported-hardware-format",
    "unsupported-profile",
    "unsupported-dimension",
    "unimplemented-syntax",
    "requires-profile-override",
    "capability-boundary",
]
# A failure inside a suite that is expected to be rejected before submission is still
# a rejection; the class documents why the vector does not produce reference pixels.
CHECKSUM_POLICIES = ["compare-to-software-output", "derived-at-runtime"]
DERIVE_BASES = ["suite_all", "r11_failing", "r11_passing"]
EXCLUSION_REASONS = ["outside-tested-scope", "unsupported-hardware-format"]
COVERAGE_VALUES = [
    "progressive",
    "interlaced",
    "8-bit-420",
    "10-bit-420",
    "422-format",
    "444-format",
    "12-bit",
    "lossless",
    "full-range",
    "long-term-reference",
    "multi-slice",
    "multi-tile",
    "multi-context-interleave",
    "resolution-change",
    "crop-odd-dimensions",
    "sub-64-dimension",
    "profile-override",
    "truncated-input",
]
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024


class CorpusError(Exception):
    """An actionable corpus problem: missing, changed, unlicensed or unsupported input."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def md5_file(path: Path) -> str:
    """Upstream Fluster suites pin MD5 ``source_checksum`` values.

    MD5 only detects a changed upstream file here; the recorded corpus identity is
    SHA-256 in every manifest pin and lock record.
    """
    try:
        digest = hashlib.md5(usedforsecurity=False)
    except TypeError:
        # Python 3.8 in the oldest CI tier predates this keyword. MD5 here
        # verifies the upstream transport checksum; cache identity is SHA-256.
        digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path, what: str) -> dict:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise CorpusError(f"{what} not found: {path}")
    except json.JSONDecodeError as error:
        raise CorpusError(f"{what} is not valid JSON: {path}: {error}")
    if not isinstance(data, dict):
        raise CorpusError(f"{what} must be a JSON object: {path}")
    return data


# --------------------------------------------------------------------------------------
# Manifest schema and policy validation
# --------------------------------------------------------------------------------------

def _require(condition: bool, message: str, issues: list[str]) -> bool:
    if not condition:
        issues.append(message)
    return condition


def _require_keys(obj: dict, keys: list[str], where: str, issues: list[str]) -> None:
    for key in keys:
        if key not in obj:
            issues.append(f"{where}: missing required key '{key}'")


def _check_license(obj: object, where: str, issues: list[str]) -> None:
    """A redistribution decision is not a licence, and a licence is not a title.

    Every entry states whether usable terms were actually identified, where they were checked,
    and what that implies. "Not established" is a valid, explicit answer; presenting a suite
    title or a storage URL as licence terms is not.
    """
    if not isinstance(obj, dict):
        issues.append(f"{where}: license must be an object")
        return
    _require_keys(
        obj,
        ["redistribution", "terms_status", "terms_reference", "terms_checked", "terms_note", "basis", "notes"],
        where + ".license",
        issues,
    )
    value = obj.get("redistribution")
    if value not in REDISTRIBUTION_VALUES:
        issues.append(f"{where}: license.redistribution must be one of {REDISTRIBUTION_VALUES}, got {value!r}")
        return
    terms = obj.get("terms_status")
    if terms not in TERMS_STATUS_VALUES:
        issues.append(f"{where}: license.terms_status must be one of {TERMS_STATUS_VALUES}, got {terms!r}")
        return
    if not isinstance(obj.get("terms_note"), str) or not obj.get("terms_note", "").strip():
        issues.append(f"{where}: license.terms_note must describe what was checked for terms")
    if not DATE_RE.match(str(obj.get("terms_checked", ""))):
        issues.append(f"{where}: license.terms_checked must be YYYY-MM-DD")
    reference = obj.get("terms_reference")
    if terms == "identified":
        if not obj.get("license_name"):
            issues.append(f"{where}: terms_status 'identified' needs a named licence in license_name")
        if not reference:
            issues.append(f"{where}: terms_status 'identified' needs terms_reference pointing at the terms")
    else:
        if not isinstance(reference, str) and reference is not None:
            issues.append(f"{where}: terms_reference must be a string or null for not-established terms")
        if reference is None and "no terms" not in str(obj.get("terms_note", "")).lower():
            issues.append(
                f"{where}: terms_reference is null, so terms_note must state explicitly that no terms "
                "source could be found"
            )
    if value == "redistributable":
        if terms != "identified":
            issues.append(
                f"{where}: redistribution is 'redistributable' but the terms are not identified; "
                "nothing may be redistributed on an assumption"
            )
        if not obj.get("license_name"):
            issues.append(
                f"{where}: license.redistribution is 'redistributable' but license_name is missing; "
                "a redistributed asset needs a named licence, not an assumption"
            )
    if obj.get("contains_personal_media"):
        issues.append(f"{where}: personal recordings must never enter the corpus")


def _iter_strings(node: object, path: str = ""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_strings(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _iter_strings(value, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


def _check_no_private_paths(text: str, where: str, issues: list[str]) -> None:
    if re.search(r"/(Users|home)/[^/]+", text):
        issues.append(f"{where}: personal filesystem path leaked into the manifest: {text!r}")
    if text.startswith("~"):
        issues.append(f"{where}: home-relative path is not reproducible: {text!r}")


def validate_schema(manifest: dict) -> list[str]:
    issues: list[str] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        issues.append(f"schema_version must be {SCHEMA_VERSION!r}, got {manifest.get('schema_version')!r}")
    if manifest.get("manifest_id") != MANIFEST_ID:
        issues.append(f"manifest_id must be {MANIFEST_ID!r}, got {manifest.get('manifest_id')!r}")
    _require_keys(
        manifest,
        [
            "schema_version",
            "manifest_id",
            "date",
            "purpose",
            "policy",
            "upstreams",
            "suites",
            "known_issues",
            "generated",
            "smoke",
            "excluded_suites",
            "known_gaps",
            "evidence",
        ],
        "manifest",
        issues,
    )
    if not DATE_RE.match(str(manifest.get("date", ""))):
        issues.append("date must be YYYY-MM-DD")

    policy = manifest.get("policy")
    if not isinstance(policy, dict):
        issues.append("policy must be an object")
    else:
        _require_keys(policy, ["redistribution_values", "terms_status_values", "asset_classes", "coverage_vocabulary", "hash_pinning", "redaction"], "policy", issues)
        if policy.get("redistribution_values") != REDISTRIBUTION_VALUES:
            issues.append("policy.redistribution_values must match the tool's redistribution vocabulary")
        if policy.get("terms_status_values") != TERMS_STATUS_VALUES:
            issues.append("policy.terms_status_values must match the tool's terms vocabulary")
        if policy.get("asset_classes") != ASSET_CLASSES:
            issues.append("policy.asset_classes must match the tool's asset class vocabulary")
        if policy.get("coverage_vocabulary") != COVERAGE_VALUES:
            issues.append("policy.coverage_vocabulary must match the tool's coverage vocabulary")

    upstreams = manifest.get("upstreams")
    if not isinstance(upstreams, dict) or "fluster" not in upstreams:
        issues.append("upstreams.fluster is required")
    else:
        fluster = upstreams["fluster"]
        _require_keys(fluster, ["repo", "commit", "commit_date", "test_suites_path"], "upstreams.fluster", issues)
        if not re.match(r"^[0-9a-f]{40}$", str(fluster.get("commit", ""))):
            issues.append("upstreams.fluster.commit must be a full 40-character commit")

    suites = manifest.get("suites")
    if not isinstance(suites, list) or not suites:
        issues.append("suites must be a non-empty list")
        suites = []
    seen_suite_ids: set[str] = set()
    for index, suite in enumerate(suites):
        where = f"suites[{index}]"
        if not isinstance(suite, dict):
            issues.append(f"{where}: must be an object")
            continue
        _require_keys(
            suite,
            [
                "id",
                "kind",
                "codec",
                "suite_name",
                "suite_file",
                "suite_file_sha256",
                "suite_upstream_commit",
                "vector_count",
                "r11_pass_set_key",
                "acquisition",
                "license",
                "assets",
            ],
            where,
            issues,
        )
        suite_id = suite.get("id")
        if suite_id in seen_suite_ids:
            issues.append(f"{where}: duplicate suite id {suite_id!r}")
        seen_suite_ids.add(suite_id)
        if suite.get("kind") != "fluster-suite":
            issues.append(f"{where}: kind must be 'fluster-suite'")
        if suite_id is not None and not re.match(r"^fluster/", str(suite_id)):
            issues.append(f"{where}: suite id must start with 'fluster/'")
        if not SHA256_RE.match(str(suite.get("suite_file_sha256", ""))):
            issues.append(f"{where}: suite_file_sha256 must be 64 lowercase hex characters")
        _check_no_private_paths(str(suite.get("suite_file", "")), where + ".suite_file", issues)
        if not isinstance(suite.get("vector_count"), int) or suite.get("vector_count", 0) <= 0:
            issues.append(f"{where}: vector_count must be a positive integer")
        _check_license(suite.get("license"), where, issues)
        acquisition = suite.get("acquisition")
        if not isinstance(acquisition, dict):
            issues.append(f"{where}: acquisition must be an object")
        else:
            _require_keys(acquisition, ["method", "tool", "resources_layout", "mirror_compatible"], where + ".acquisition", issues)
            if "<suite_name>/<vector_name>/<input_file>" not in str(acquisition.get("resources_layout", "")):
                issues.append(
                    f"{where}: acquisition.resources_layout must keep the Fluster "
                    "<suite_name>/<vector_name>/<input_file> layout so the cache is usable by conformance.py"
                )
        assets = suite.get("assets")
        if not isinstance(assets, dict):
            issues.append(f"{where}: assets must be an object (possibly empty)")
            continue
        for name, asset in assets.items():
            asset_where = f"{where}.assets[{name!r}]"
            if not isinstance(asset, dict):
                issues.append(f"{asset_where}: must be an object")
                continue
            _require_keys(asset, ["class", "covers", "asset_sha256", "bytes"], asset_where, issues)
            if asset.get("class") not in ASSET_CLASSES:
                issues.append(f"{asset_where}: class must be one of {ASSET_CLASSES}, got {asset.get('class')!r}")
            covers = asset.get("covers")
            if not isinstance(covers, list) or not covers:
                issues.append(f"{asset_where}: covers must be a non-empty list")
            else:
                for token in covers:
                    if token not in COVERAGE_VALUES:
                        issues.append(f"{asset_where}: unknown coverage token {token!r}")
            value = asset.get("asset_sha256")
            if value is not None and not SHA256_RE.match(str(value)):
                issues.append(f"{asset_where}: asset_sha256 must be null or 64 lowercase hex characters")
            if asset.get("asset_sha256") is None and asset.get("hash_status") != "pending":
                issues.append(
                    f"{asset_where}: asset_sha256 is unset; mark hash_status 'pending' until a verified "
                    "acquisition records it (never paste an unverified hash)"
                )
            if asset.get("hash_status") is not None and asset.get("hash_status") != "pending":
                issues.append(f"{asset_where}: hash_status must be 'pending' when present")
            if not isinstance(asset.get("bytes"), (int, type(None))):
                issues.append(f"{asset_where}: bytes must be an integer or null")

    known_issues = manifest.get("known_issues")
    if not isinstance(known_issues, list):
        issues.append("known_issues must be a list")
        known_issues = []
    seen_issue_ids: set[str] = set()
    for index, entry in enumerate(known_issues):
        where = f"known_issues[{index}]"
        if not isinstance(entry, dict):
            issues.append(f"{where}: must be an object")
            continue
        _require_keys(entry, ["id", "suite", "class", "summary", "evidence", "vectors", "derive", "hardware_needed", "review"], where, issues)
        if entry.get("id") in seen_issue_ids:
            issues.append(f"{where}: duplicate id {entry.get('id')!r}")
        seen_issue_ids.add(entry.get("id"))
        if entry.get("suite") not in seen_suite_ids:
            issues.append(f"{where}: suite {entry.get('suite')!r} is not a declared suite")
        if entry.get("class") not in ASSET_CLASSES or entry.get("class") == "pass":
            issues.append(f"{where}: class must be a failure/limitation class from {ASSET_CLASSES[1:]}")
        if not isinstance(entry.get("vectors"), list) or not entry.get("vectors"):
            issues.append(f"{where}: vectors must be a non-empty explicit list")
        derive = entry.get("derive")
        if derive is not None and not isinstance(derive, dict):
            issues.append(f"{where}: derive must be an object or null")
        elif isinstance(derive, dict):
            if derive.get("base") not in DERIVE_BASES:
                issues.append(f"{where}: derive.base must be one of {DERIVE_BASES}")
            for token in derive.get("output_format_in", []) or []:
                if not isinstance(token, str) or not token:
                    issues.append(f"{where}: derive.output_format_in entries must be non-empty strings")
            for ref in derive.get("exclude_entries", []) or []:
                if ref not in seen_issue_ids and ref not in [e.get("id") for e in known_issues if isinstance(e, dict)]:
                    issues.append(f"{where}: derive.exclude_entries references unknown entry {ref!r}")

    generated = manifest.get("generated")
    if not isinstance(generated, list) or not generated:
        issues.append("generated must be a non-empty list")
        generated = []
    for index, entry in enumerate(generated):
        where = f"generated[{index}]"
        if not isinstance(entry, dict):
            issues.append(f"{where}: must be an object")
            continue
        _require_keys(entry, ["id", "kind", "codec", "producer", "reproduction", "frames", "checksum_policy", "determinism", "covers", "license", "tools"], where, issues)
        _require_keys(entry, ["r11_part", "r11_frames"], where, issues)
        reproduction = entry.get("reproduction")
        if not isinstance(reproduction, dict):
            issues.append(f"{where}: reproduction must be an object describing the producer script and its invocation")
        else:
            _require_keys(reproduction, ["producer", "invocation", "description"], where + ".reproduction", issues)
            producer = reproduction.get("producer")
            invocation = reproduction.get("invocation")
            description = reproduction.get("description")
            if not isinstance(description, str) or not description.strip():
                issues.append(f"{where}: reproduction.description must explain what the producer generates")
            if isinstance(producer, str) and producer:
                if not (ROOT / producer).is_file():
                    issues.append(
                        f"{where}: reproduction.producer {producer!r} is not a file in this repository; "
                        "a reproduction path must point at the checked-in script that generates the input"
                    )
                if entry.get("producer") and entry.get("producer") != producer:
                    issues.append(f"{where}: producer and reproduction.producer disagree")
                if isinstance(invocation, str) and producer not in invocation:
                    issues.append(
                        f"{where}: reproduction.invocation must be the executable command for {producer} "
                        "(it does not reference it)"
                    )
        if entry.get("kind") != "generated":
            issues.append(f"{where}: kind must be 'generated'")
        if entry.get("checksum_policy") not in CHECKSUM_POLICIES:
            issues.append(f"{where}: checksum_policy must be one of {CHECKSUM_POLICIES}")
        if entry.get("checksum_policy") == "compare-to-software-output" and entry.get("expected_asset_hashes_committed") is not False:
            issues.append(
                f"{where}: generated clips compared against software output must set "
                "expected_asset_hashes_committed=false; committed generator hashes drift silently"
            )
        covers = entry.get("covers")
        if not isinstance(covers, list) or not covers:
            issues.append(f"{where}: covers must be a non-empty list")
        else:
            for token in covers:
                if token not in COVERAGE_VALUES:
                    issues.append(f"{where}: unknown coverage token {token!r}")
        determinism = entry.get("determinism")
        if not isinstance(determinism, dict):
            issues.append(f"{where}: determinism must be an object")
        else:
            _require_keys(determinism, ["basis", "seed_policy", "tool_pins"], where + ".determinism", issues)
        _check_license(entry.get("license"), where, issues)

    smoke = manifest.get("smoke")
    if not isinstance(smoke, dict):
        issues.append("smoke must be an object")
    else:
        _require_keys(smoke, ["description", "max_bytes", "required_coverage", "entries"], "smoke", issues)
        if not isinstance(smoke.get("max_bytes"), int) or smoke.get("max_bytes", 0) <= 0:
            issues.append("smoke.max_bytes must be a positive integer")
        required = smoke.get("required_coverage")
        if not isinstance(required, list) or not required:
            issues.append("smoke.required_coverage must be a non-empty list")
        else:
            for token in required:
                if token not in COVERAGE_VALUES:
                    issues.append(f"smoke.required_coverage: unknown coverage token {token!r}")
        entries = smoke.get("entries")
        if not isinstance(entries, list) or not entries:
            issues.append("smoke.entries must be a non-empty list")
            entries = []
        covered: set[str] = set()
        total_bytes = 0
        suite_by_id = {s.get("id"): s for s in suites if isinstance(s, dict)}
        generated_ids = {g.get("id") for g in generated if isinstance(g, dict)}
        for ref in entries:
            if not isinstance(ref, str):
                issues.append(f"smoke.entries: {ref!r} is not a string reference")
                continue
            if ref in generated_ids:
                entry = next(g for g in generated if g.get("id") == ref)
                covered.update(entry.get("covers", []))
                continue
            match = ASSET_REF_RE.match(ref)
            if not match:
                issues.append(
                    f"smoke.entries: {ref!r} must be a declared generated id or "
                    "'<suite id>#<vector name>'"
                )
                continue
            suite_id, vector = match.group("suite"), match.group("vector")
            suite = suite_by_id.get(suite_id)
            if suite is None:
                issues.append(f"smoke.entries: unknown suite {suite_id!r} in {ref!r}")
                continue
            asset = (suite.get("assets") or {}).get(vector)
            if asset is None:
                issues.append(
                    f"smoke.entries: {ref!r} has no pinned asset block; smoke entries need a class, "
                    "coverage tokens and a verified hash"
                )
                continue
            covered.update(asset.get("covers", []))
            if isinstance(asset.get("bytes"), int):
                total_bytes += asset["bytes"]
        for token in required or []:
            if token not in covered:
                issues.append(f"smoke: required coverage {token!r} is not provided by any smoke entry")
        if isinstance(smoke.get("max_bytes"), int) and total_bytes > smoke["max_bytes"]:
            issues.append(
                f"smoke: pinned assets total {total_bytes} bytes, above the {smoke['max_bytes']} byte budget"
            )

    excluded = manifest.get("excluded_suites")
    if not isinstance(excluded, list):
        issues.append("excluded_suites must be a list")
    else:
        for index, entry in enumerate(excluded):
            where = f"excluded_suites[{index}]"
            if not isinstance(entry, dict):
                issues.append(f"{where}: must be an object")
                continue
            _require_keys(entry, ["suite_file", "reason_code", "summary"], where, issues)
            if entry.get("reason_code") not in EXCLUSION_REASONS:
                issues.append(f"{where}: reason_code must be one of {EXCLUSION_REASONS}")

    gaps = manifest.get("known_gaps")
    if not isinstance(gaps, list) or not gaps:
        issues.append("known_gaps must be a non-empty list of strings")

    evidence = manifest.get("evidence")
    if not isinstance(evidence, dict):
        issues.append("evidence must be an object")
    else:
        _require_keys(evidence, ["pass_set_file", "record", "note"], "evidence", issues)

    issue_ids = {e.get("id") for e in known_issues if isinstance(e, dict)}
    for index, entry in enumerate(known_issues):
        derive = entry.get("derive") if isinstance(entry, dict) else None
        if isinstance(derive, dict):
            for ref in derive.get("exclude_entries", []) or []:
                if ref not in issue_ids:
                    issues.append(f"known_issues[{index}]: derive.exclude_entries references unknown entry {ref!r}")

    # Publishable manifests must not carry host paths: they are neither reproducible
    # nor appropriate for a public repository.
    for path, text in _iter_strings(manifest):
        _check_no_private_paths(text, path, issues)
    return issues


# --------------------------------------------------------------------------------------
# Cross-checks against the pinned Fluster checkout and the r11 pass sets
# --------------------------------------------------------------------------------------

class SuiteIndex:
    """Vectors of a pinned Fluster suite file, verified against the manifest digest."""

    def __init__(self, suite: dict, fluster_dir: Path) -> None:
        self.suite = suite
        self.suite_id = suite["id"]
        path = fluster_dir / suite["suite_file"]
        if not path.is_file():
            raise CorpusError(
                f"suite file for {self.suite_id} not found: {path}\n"
                f"        clone the pinned Fluster commit "
                f"{suite['suite_upstream_commit']} (upstreams.fluster in the manifest)"
            )
        actual = sha256_file(path)
        if actual != suite["suite_file_sha256"]:
            raise CorpusError(
                f"suite definition changed for {self.suite_id}\n"
                f"        expected sha256 {suite['suite_file_sha256']}\n"
                f"        actual   sha256 {actual}\n"
                f"        file {path}\n"
                "        A different suite definition invalidates vector names, checksums and totals. "
                "Re-review the manifest before updating the pin."
            )
        data = load_json(path, f"Fluster suite {suite['suite_name']}")
        vectors = data.get("test_vectors")
        if not isinstance(vectors, list) or not vectors:
            raise CorpusError(f"suite {path} has no test_vectors")
        self.by_name: dict[str, dict] = {}
        for vector in vectors:
            name = vector.get("name")
            if not name:
                raise CorpusError(f"suite {path} has a test vector without a name")
            if name in self.by_name:
                raise CorpusError(f"suite {path} lists vector {name!r} twice")
            self.by_name[name] = vector
        if suite.get("vector_count") != len(self.by_name):
            raise CorpusError(
                f"suite {self.suite_id}: manifest vector_count is {suite.get('vector_count')} "
                f"but the pinned suite file defines {len(self.by_name)} vectors"
            )
        self.name = data.get("name", suite["suite_name"])

    def formats(self, name: str) -> str:
        return str(self.by_name[name].get("output_format", ""))


def derive_vectors(
    entry: dict,
    suites: dict[str, SuiteIndex],
    pass_sets: dict,
    issue_vectors: dict[str, list[str]],
) -> list[str]:
    """Recompute a known-issue vector list from its declarative selection."""
    derive = entry.get("derive")
    suite = suites[entry["suite"]]
    if derive is None:
        return list(entry["vectors"])
    base = derive["base"]
    if base == "suite_all":
        selected = set(suite.by_name)
    else:
        key = suite.suite.get("r11_pass_set_key")
        sets = pass_sets.get("suites", {}).get(key)
        if not sets:
            raise CorpusError(
                f"known issue {entry['id']}: pass set {key!r} is missing from docs/r11-pass-sets.json; "
                "the classification cannot be derived"
            )
        field = "failing_vectors" if base == "r11_failing" else "passing_vectors"
        selected = set(sets.get(field, []))
        unknown = selected - set(suite.by_name)
        if unknown:
            raise CorpusError(
                f"known issue {entry['id']}: pass set {key}.{field} lists vectors that the pinned suite "
                f"does not define: {sorted(unknown)[:5]}"
            )
    pattern = derive.get("name_regex")
    if pattern:
        regex = re.compile(pattern)
        selected = {name for name in selected if regex.fullmatch(name)}
    dimension_below = derive.get("dimension_below")
    if dimension_below is not None:
        kept = set()
        for name in selected:
            match = SIZE_RE.search(name)
            if match and (int(match.group(1)) < dimension_below or int(match.group(2)) < dimension_below):
                kept.add(name)
        selected = kept
    formats = derive.get("output_format_in")
    if formats:
        selected = {name for name in selected if suite.formats(name) in formats}
    for ref in derive.get("exclude_entries", []) or []:
        selected -= set(issue_vectors.get(ref, []))
    return sorted(selected)


def cross_check(manifest: dict, fluster_dir: Path, pass_sets: dict) -> list[str]:
    issues: list[str] = []
    suites: dict[str, SuiteIndex] = {}
    for suite in manifest["suites"]:
        suites[suite["id"]] = SuiteIndex(suite, fluster_dir)

    issue_vectors: dict[str, list[str]] = {}
    for entry in manifest["known_issues"]:
        suite = suites[entry["suite"]]
        unknown = sorted(set(entry["vectors"]) - set(suite.by_name))
        if unknown:
            issues.append(f"known issue {entry['id']}: vectors not in the pinned suite: {unknown[:5]}")
        expected = derive_vectors(entry, suites, pass_sets, issue_vectors)
        declared = sorted(entry["vectors"])
        if expected != declared:
            missing = sorted(set(expected) - set(declared))
            extra = sorted(set(declared) - set(expected))
            issues.append(
                f"known issue {entry['id']}: the declared vector list no longer matches its selection "
                f"(missing {missing[:5]}, unexpected {extra[:5]})"
            )
        issue_vectors[entry["id"]] = declared

    # Every r11 failure must carry exactly one documented reason, and no vector may be
    # classified as failing while the r11 record passes it.
    for suite in manifest["suites"]:
        key = suite.get("r11_pass_set_key")
        sets = pass_sets.get("suites", {}).get(key)
        if not sets:
            continue
        failing = set(sets.get("failing_vectors", []))
        passing = set(sets.get("passing_vectors", []))
        classified: dict[str, list[str]] = {}
        for entry in manifest["known_issues"]:
            if entry["suite"] != suite["id"]:
                continue
            for name in entry["vectors"]:
                classified.setdefault(name, []).append(entry["id"])
        uncovered = sorted(failing - set(classified))
        if uncovered:
            issues.append(
                f"suite {suite['id']}: {len(uncovered)} r11 failing vector(s) have no documented class: "
                f"{uncovered[:5]}"
            )
        double = sorted(name for name, entries in classified.items() if len(entries) > 1)
        if double:
            issues.append(f"suite {suite['id']}: vectors classified twice: {double[:5]}")
        contradiction = sorted(passing & set(classified))
        if contradiction:
            issues.append(
                f"suite {suite['id']}: r11 passes these vectors but the manifest calls them failures: "
                f"{contradiction[:5]}"
            )

    # Generated frames counted in the r11 record must stay consistent with it: the
    # manifest is not allowed to inflate or quietly drop compared frames.
    comparisons = pass_sets.get("generated_hardware_comparisons")
    if comparisons:
        parts = comparisons.get("parts", {})
        declared: dict[str, int] = {}
        for entry in manifest["generated"]:
            part = entry.get("r11_part")
            if part is None:
                continue
            if part not in parts:
                issues.append(
                    f"generated {entry['id']}: r11_part {part!r} is not a part of the r11 "
                    "generated_hardware_comparisons record"
                )
                continue
            if entry.get("r11_frames") != parts[part]:
                issues.append(
                    f"generated {entry['id']}: r11_frames is {entry.get('r11_frames')} but the r11 record "
                    f"counts {parts[part]} frames for {part!r}"
                )
            declared[part] = entry.get("r11_frames") or 0
        for part in sorted(set(parts) - set(declared)):
            issues.append(
                f"generated matrices: the r11 record counts {parts[part]} frames for {part!r} but no "
                "generated entry declares r11_part " + repr(part)
            )
        total = sum(declared.values())
        if comparisons.get("total_frames") and total != comparisons["total_frames"]:
            issues.append(
                f"generated matrices: declared r11 frames total {total}, "
                f"the r11 record totals {comparisons['total_frames']}"
            )

    # A smoke asset class must agree with the classification that covers it.
    for suite in manifest["suites"]:
        for name, asset in (suite.get("assets") or {}).items():
            if name not in suites[suite["id"]].by_name:
                issues.append(f"suite {suite['id']}: pinned asset {name!r} is not in the pinned suite file")
                continue
            covering = [
                entry["class"]
                for entry in manifest["known_issues"]
                if entry["suite"] == suite["id"] and name in entry["vectors"]
            ]
            declared = asset.get("class")
            if declared == "pass":
                if covering:
                    issues.append(
                        f"suite {suite['id']}: asset {name!r} is marked 'pass' but classified as {covering}"
                    )
            elif declared not in covering:
                issues.append(
                    f"suite {suite['id']}: asset {name!r} class {declared!r} does not match its "
                    f"documented classification {covering or 'none'}"
                )
    return issues


# --------------------------------------------------------------------------------------
# Cache layout, acquisition and verification
# --------------------------------------------------------------------------------------

def asset_path(cache: Path, suite: SuiteIndex, vector: str) -> Path:
    input_file = suite.by_name[vector]["input_file"]
    return cache / suite.name / vector / input_file


def normalize_mirror(mirror: str) -> str:
    """Accept a mirror as an HTTP(S) base URL or a local directory."""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", mirror):
        return mirror.rstrip("/")
    return Path(mirror).expanduser().resolve().as_uri().rstrip("/")


def mirror_url(source: str, mirror: str) -> str:
    """Rewrite an upstream URL the way Fluster's --mirror does: <mirror>/<host>/<path>."""
    parsed = urllib.parse.urlsplit(source)
    return f"{normalize_mirror(mirror)}/{parsed.netloc}/{parsed.path.lstrip('/')}"


def download(url: str, dest: Path, timeout: int = 300) -> int:
    """Download ``url`` to ``dest``; returns the number of bytes written (0 if cached)."""
    if dest.exists():
        return 0
    request = urllib.request.Request(url, headers={"User-Agent": "libva-v4l2-request-corpus/1.0"})
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            with partial.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise CorpusError(f"refusing to download more than {MAX_DOWNLOAD_BYTES} bytes from {url}")
                    handle.write(chunk)
    except urllib.error.HTTPError as error:
        raise CorpusError(
            f"download failed for {url}: HTTP {error.code}\n"
            "        Distributors may reject non-browser clients (the ITU/JCT-VC archives are known to).\n"
            "        Retry from another host or pass --mirror with a mirror holding this asset."
        )
    except urllib.error.URLError as error:
        raise CorpusError(
            f"download failed for {url}: {error.reason}\n"
            "        Offline? Use a populated cache with verify, or --mirror for a local mirror."
        )
    partial.replace(dest)
    return total


def acquire(
    cache: Path,
    suite: SuiteIndex,
    vector: str,
    mirror: str | None = None,
    offline: bool = False,
    timeout: int = 300,
) -> dict:
    """Fetch one vector into the Fluster layout and return its verified identity."""
    manifest_asset = (suite.suite.get("assets") or {}).get(vector) or {}
    source = suite.by_name[vector]["source"]
    expected_md5 = suite.by_name[vector].get("source_checksum")
    expected_sha = manifest_asset.get("asset_sha256")
    dest = asset_path(cache, suite, vector)
    staging = cache / ".staging"

    if dest.is_file():
        record = describe_asset(dest)
        if expected_sha and record["sha256"] != expected_sha:
            raise CorpusError(
                f"cached asset does not match its pinned hash: {dest}\n"
                f"        expected sha256 {expected_sha}\n"
                f"        actual   sha256 {record['sha256']}\n"
                "        Delete the file to re-acquire it; the tool never overwrites a pinned asset."
            )
        if not expected_sha:
            ref = f"{suite.suite_id}#{vector}"
            local = load_lock(cache).get(ref)
            if not local or local["file"] != suite.by_name[vector]["input_file"]:
                raise CorpusError(
                    f"cached asset has no verified acquisition identity: {ref}\n"
                    "        Delete the cached file and fetch it again to verify the upstream checksum."
                )
            if record["sha256"] != local["asset_sha256"] or record["bytes"] != local["bytes"]:
                raise CorpusError(f"hash mismatch against the local acquisition lock for {ref}")
        return {**record, "action": "cached", "path": str(dest), "download_bytes": 0}
    if offline:
        raise CorpusError(
            f"asset missing from the cache and --offline was requested: {dest}\n"
            f"        acquire it first: python3 tests/corpus.py fetch --fluster DIR --cache {cache} "
            f"--vector {suite.suite_id}#{vector}"
        )

    urls = [mirror_url(source, mirror)] if mirror else []
    urls.append(source)
    errors = []
    downloaded: Path | None = None
    download_bytes = 0
    for url in urls:
        staged = staging / urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
        try:
            download_bytes = download(url, staged, timeout=timeout)
            downloaded = staged
            break
        except CorpusError as error:
            errors.append(str(error))
    if downloaded is None:
        raise CorpusError("could not acquire " + suite.suite_id + "#" + vector + "\n        " + "\n        ".join(errors))

    if expected_md5:
        actual_md5 = md5_file(downloaded)
        if actual_md5 != expected_md5:
            raise CorpusError(
                f"upstream file changed for {suite.suite_id}#{vector}\n"
                f"        suite pins md5 {expected_md5}\n"
                f"        actual      md5 {actual_md5}\n"
                f"        source {source}\n"
                "        Do not accept the new file: confirm the upstream change before updating the pin."
            )
    archive_sha = sha256_file(downloaded)
    input_file = suite.by_name[vector]["input_file"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    if downloaded.suffix.lower() in {".zip", ".tar", ".gz", ".bz2", ".xz", ".7z"}:
        if downloaded.suffix.lower() != ".zip":
            raise CorpusError(
                f"unsupported archive format for {suite.suite_id}#{vector}: {downloaded.name}; "
                "extend acquire() with an explicit extractor before using this suite"
            )
        with zipfile.ZipFile(downloaded) as archive:
            names = archive.namelist()
            member = input_file if input_file in names else None
            if member is None:
                candidates = [n for n in names if Path(n).name == input_file]
                member = candidates[0] if len(candidates) == 1 else None
            if member is None:
                raise CorpusError(
                    f"{input_file} is not in {downloaded.name} for {suite.suite_id}#{vector}; "
                    f"archive contains {names[:8]}"
                )
            partial = dest.with_suffix(dest.suffix + ".part")
            with archive.open(member) as src, partial.open("wb") as out:
                shutil.copyfileobj(src, out)
            partial.replace(dest)
        downloaded.unlink()
    else:
        downloaded.replace(dest)

    record = describe_asset(dest)
    if expected_sha and record["sha256"] != expected_sha:
        dest.unlink(missing_ok=True)
        raise CorpusError(
            f"acquired asset does not match the pinned hash for {suite.suite_id}#{vector}\n"
            f"        expected sha256 {expected_sha}\n"
            f"        actual   sha256 {record['sha256']}"
        )
    return {
        **record,
        "action": "fetched",
        "path": str(dest),
        "source": source,
        "archive_sha256": archive_sha,
        "upstream_md5": expected_md5,
        "download_bytes": download_bytes,
    }


def describe_asset(path: Path) -> dict:
    stat = path.stat()
    return {"sha256": sha256_file(path), "md5": md5_file(path), "bytes": stat.st_size}


def load_lock(cache: Path) -> dict:
    """Read the local acquisition lock produced by `corpus.py lock`, if present."""
    path = cache / "corpus-lock.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise CorpusError(f"cannot read acquisition lock {path}: {error}") from error
    if not isinstance(document, dict) or not isinstance(document.get("assets"), dict):
        raise CorpusError(f"invalid acquisition lock {path}: assets must be an object")
    for ref, record in document["assets"].items():
        if (not ASSET_REF_RE.fullmatch(ref) or not isinstance(record, dict)
                or not SHA256_RE.fullmatch(str(record.get("asset_sha256", "")))
                or type(record.get("bytes")) is not int or record["bytes"] < 0
                or not isinstance(record.get("file"), str) or not record["file"]
                or Path(record["file"]).is_absolute() or ".." in Path(record["file"]).parts):
            raise CorpusError(f"invalid acquisition identity in lock {path}: {ref}")
    return document["assets"]


def record_acquisition(cache: Path, suite: SuiteIndex, vector: str, record: dict) -> int:
    """Persist only the newly checksum-verified asset; never rehash unrelated cache files."""
    records = load_lock(cache)
    ref = f"{suite.suite_id}#{vector}"
    identity = {
        "asset_sha256": record["sha256"], "md5": record["md5"], "bytes": record["bytes"],
        "file": suite.by_name[vector]["input_file"],
        "pinned": bool((suite.suite.get("assets") or {}).get(vector, {}).get("asset_sha256")),
    }
    previous = records.get(ref)
    if previous and any(previous[key] != identity[key] for key in ("asset_sha256", "bytes", "file")):
        raise CorpusError(f"acquired asset differs from its recorded acquisition identity: {ref}")
    records[ref] = identity
    document = {"note": "Checksum-verified acquisitions; existing identities are retained.", "assets": records}
    # A failed/interrupted write must leave the previous identities readable.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache, delete=False) as handle:
        partial = Path(handle.name)
        try:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            partial.replace(cache / "corpus-lock.json")
        finally:
            partial.unlink(missing_ok=True)
    return len(records)


def verify_cache(
    manifest: dict,
    cache: Path,
    fluster_dir: Path | None,
    require: str,
) -> list[str]:
    """Check present assets against pinned hashes; report which requested ones are missing."""
    issues: list[str] = []
    smoke = manifest["smoke"]["entries"]
    lock = load_lock(cache)
    smoke_assets = {ref for ref in smoke if ASSET_REF_RE.match(ref)}
    for suite in manifest["suites"]:
        suite_name = suite["suite_name"]
        for vector, asset in (suite.get("assets") or {}).items():
            ref = f"{suite['id']}#{vector}"
            known_input = None
            if fluster_dir is not None:
                index = SuiteIndex(suite, fluster_dir)
                if vector not in index.by_name:
                    issues.append(f"{ref}: not in the pinned suite file")
                    continue
                known_input = index.by_name[vector]["input_file"]
            vector_dir = cache / suite_name / vector
            if known_input:
                candidates = [vector_dir / known_input]
            else:
                # Fluster input_file may itself contain a directory component, so the
                # fallback has to search the whole vector directory.
                candidates = sorted(p for p in vector_dir.rglob("*") if p.is_file()) if vector_dir.is_dir() else []
                if len(candidates) > 1:
                    issues.append(
                        f"cannot identify the asset for {ref}: {vector_dir} holds "
                        f"{len(candidates)} files\n"
                        "        pass --fluster so the pinned suite's input_file selects the right one"
                    )
                    continue
            present = [path for path in candidates if path.is_file()]
            if not present:
                if require == "all" or (require == "smoke" and ref in smoke_assets):
                    issues.append(
                        f"missing asset {ref}\n"
                        f"        expected at {cache / suite_name / vector}\n"
                        f"        run: python3 tests/corpus.py fetch --fluster DIR --cache {cache} --smoke"
                    )
                continue
            path = present[0]
            record = describe_asset(path)
            if asset.get("asset_sha256"):
                if record["sha256"] != asset["asset_sha256"]:
                    issues.append(
                        f"hash mismatch for {ref}: {path}\n"
                        f"        expected sha256 {asset['asset_sha256']}\n"
                        f"        actual   sha256 {record['sha256']}\n"
                        "        A changed asset invalidates every result recorded against it."
                    )
            # An asset acquired but not yet pinned in the manifest is still checked
            # against the local acquisition lock, so a tampered file is caught before
            # anyone records a hash as an expectation.
            local = lock.get(ref)
            if local and not asset.get("asset_sha256") and local.get("asset_sha256"):
                if record["sha256"] != local["asset_sha256"]:
                    issues.append(
                        f"hash mismatch against the local acquisition lock for {ref}: {path}\n"
                        f"        lock sha256 {local['asset_sha256']}\n"
                        f"        actual      sha256 {record['sha256']}\n"
                        "        Re-acquire the asset (delete it) and re-check before pinning anything."
                    )
            if asset.get("bytes") is not None and record["bytes"] != asset["bytes"]:
                issues.append(
                    f"size mismatch for {ref}: expected {asset['bytes']} bytes, found {record['bytes']}"
                )

    # ``--require all`` means the whole corpus, not the small set of assets with a recorded
    # SHA-256. Without this pass a smoke-only cache would satisfy it, which is exactly the
    # difference between "the smoke subset is present" and "the suite is present".
    if require == "all":
        if fluster_dir is None:
            issues.append(
                "--require all needs --fluster: vector names come from the pinned suite definitions, "
                "which are the only source of the full vector list"
            )
        else:
            for suite in manifest["suites"]:
                index = SuiteIndex(suite, fluster_dir)
                missing = []
                for vector in sorted(index.by_name):
                    path = cache / suite["suite_name"] / vector / index.by_name[vector]["input_file"]
                    if not path.is_file():
                        missing.append(vector)
                if missing:
                    issues.append(
                        f"suite {suite['id']}: {len(missing)} of {len(index.by_name)} vectors are missing "
                        f"from {cache / suite['suite_name']} (first: {', '.join(missing[:3])})\n"
                        f"        run: python3 tests/corpus.py fetch --fluster DIR --cache {cache} "
                        f"--suite {suite['id']}"
                    )

    # Assets acquired but not yet pinned in the manifest still have an identity in the local
    # acquisition lock. Checking them here means a tampered file is caught before anyone records
    # a hash as an expectation, including for suite-wide acquisitions.
    suite_by_id = {suite["id"]: suite for suite in manifest["suites"]}
    pinned = pinned_refs(manifest)
    for suite in manifest["suites"]:
        for vector in _vectors_to_lock(suite, cache / suite["suite_name"]):
            ref = f"{suite['id']}#{vector}"
            directory = cache / suite["suite_name"] / vector
            if ref not in pinned and ref not in lock and any(p.is_file() for p in directory.rglob("*")):
                issues.append(f"missing verified acquisition identity for {ref}; delete and re-fetch the asset")
    for ref, record in sorted(lock.items()):
        if ref in pinned:
            continue
        match = ASSET_REF_RE.match(ref)
        if not match:
            continue
        suite_id, vector = match.group("suite"), match.group("vector")
        suite = suite_by_id.get(suite_id)
        if suite is None:
            issues.append(f"lock entry {ref} refers to a suite that is not in the manifest")
            continue
        target = cache / suite["suite_name"] / vector / str(record.get("file", ""))
        if not target.is_file():
            continue
        actual = describe_asset(target)
        if record.get("asset_sha256") and actual["sha256"] != record["asset_sha256"]:
            issues.append(
                f"hash mismatch against the local acquisition lock for {ref}: {target}\n"
                f"        lock sha256 {record['asset_sha256']}\n"
                f"        actual      sha256 {actual['sha256']}\n"
                "        Re-acquire the asset (delete it) and re-check before pinning anything."
            )
        elif record.get("bytes") is not None and actual["bytes"] != record["bytes"]:
            issues.append(
                f"size mismatch against the local acquisition lock for {ref}: "
                f"expected {record['bytes']} bytes, found {actual['bytes']}"
            )
    return issues


def _vectors_to_lock(suite: dict, suite_dir: Path) -> list[str]:
    """Pinned vectors first, then any other vector directory found in the cache."""
    pinned = list(suite.get("assets") or {})
    extra = []
    if suite_dir.is_dir():
        extra = [p.name for p in sorted(suite_dir.iterdir()) if p.is_dir() and p.name not in pinned]
    return pinned + extra


def write_lock(cache: Path, manifest: dict, fluster_dir: Path | None = None, path: Path | None = None) -> dict:
    """Record SHA-256 for assets present in the cache. Never edits the manifest."""
    records: dict[str, dict] = {}
    indexes: dict[str, SuiteIndex] = {}
    for suite in manifest["suites"]:
        index: SuiteIndex | None = None
        if fluster_dir is not None:
            index = indexes.setdefault(suite["id"], SuiteIndex(suite, fluster_dir))
        for vector in _vectors_to_lock(suite, cache / suite["suite_name"]):
            ref = f"{suite['id']}#{vector}"
            if ref in records:
                continue
            vector_dir = cache / suite["suite_name"] / vector
            if not vector_dir.is_dir():
                continue
            if index is not None and vector in index.by_name:
                candidates = [vector_dir / index.by_name[vector]["input_file"]]
            else:
                candidates = sorted(p for p in vector_dir.rglob("*") if p.is_file())
                if len(candidates) != 1:
                    print(
                        f"warning: skipping {ref}: {vector_dir} holds {len(candidates)} files and no pinned "
                        "suite input_file could select one; pass --fluster",
                        file=sys.stderr,
                    )
                    continue
            target = candidates[0]
            if not target.is_file():
                continue
            records[ref] = {
                "asset_sha256": sha256_file(target),
                "md5": md5_file(target),
                "bytes": target.stat().st_size,
                "file": target.relative_to(vector_dir).as_posix(),
                "pinned": bool((suite.get("assets") or {}).get(vector, {}).get("asset_sha256")),
            }
    document = {
        "note": (
            "Locally computed identities for acquired corpus assets. This file is evidence, not a "
            "manifest update: recording a hash in tests/corpus/manifest.json stays a deliberate, "
            "reviewed change."
        ),
        "cache": str(cache),
        "assets": records,
    }
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return document


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

def parse_asset_ref(ref: str) -> tuple[str, str]:
    match = ASSET_REF_RE.match(ref)
    if not match:
        raise CorpusError(f"asset reference must be '<suite id>#<vector name>': {ref!r}")
    return match.group("suite"), match.group("vector")


def select_assets(
    manifest: dict,
    args,
    fluster_dir: Path | None = None,
) -> list[tuple[dict, str]]:
    """Resolve the requested selection to (suite, vector) pairs.

    Selections that name a suite enumerate the **pinned suite definition**, not the small
    set of assets the manifest happens to pin hashes for. Pinning a vector's SHA-256 is a
    separate, deliberate step (see ``lock``); acquiring it is not gated on that.
    """
    suites = {suite["id"]: suite for suite in manifest["suites"]}
    selected: list[tuple[dict, str]] = []
    needs_definition = bool(args.all or args.suite or args.vector)
    if needs_definition and fluster_dir is None:
        raise CorpusError(
            "--fluster is required to enumerate suite vectors: vector names come from the pinned "
            "suite definition, not from the manifest"
        )
    indexes: dict[str, SuiteIndex] = {}

    def vectors_of(suite: dict) -> list[str]:
        index = indexes.setdefault(suite["id"], SuiteIndex(suite, fluster_dir))
        return sorted(index.by_name)

    if args.vector:
        for ref in args.vector:
            suite_id, vector = parse_asset_ref(ref)
            if suite_id not in suites:
                raise CorpusError(f"unknown suite {suite_id!r}; known suites: {sorted(suites)}")
            suite = suites[suite_id]
            if vector not in vectors_of(suite):
                raise CorpusError(
                    f"{ref} is not in the pinned suite definition; check the vector name against "
                    f"{suite['suite_file']} in the pinned Fluster commit"
                )
            selected.append((suite, vector))
    elif args.suite:
        for suite_id in args.suite:
            if suite_id not in suites:
                raise CorpusError(f"unknown suite {suite_id!r}; known suites: {sorted(suites)}")
            suite = suites[suite_id]
            selected.extend((suite, vector) for vector in vectors_of(suite))
    elif args.smoke:
        for ref in manifest["smoke"]["entries"]:
            match = ASSET_REF_RE.match(ref)
            if not match:
                # Generated smoke entries are produced by their own scripts, not fetched.
                continue
            suite_id, vector = parse_asset_ref(ref)
            if suite_id in suites:
                selected.append((suites[suite_id], vector))
    elif args.all:
        if not args.confirm_large_corpus and not args.dry_run:
            raise CorpusError(
                "--all downloads the entire corpus (multiple gigabytes) and will not run without "
                "--confirm-large-corpus. Smoke-covered work should use --smoke; use --dry-run to "
                "see the selection first."
            )
        for suite in manifest["suites"]:
            selected.extend((suite, vector) for vector in vectors_of(suite))
    else:
        raise CorpusError("select assets with --smoke, --suite, --vector or --all")
    return selected


def pinned_refs(manifest: dict) -> set[str]:
    """Asset references with a recorded SHA-256.

    An asset block without a hash is a placeholder for a reviewed pin, not a pin: the
    distinction matters when reporting how much of a selection is hash-verified.
    """
    return {
        f"{suite['id']}#{vector}"
        for suite in manifest["suites"]
        for vector, asset in (suite.get("assets") or {}).items()
        if asset.get("asset_sha256")
    }


def print_selection_plan(selected: list[tuple[dict, str]], manifest: dict) -> None:
    pinned = pinned_refs(manifest)
    per_suite: dict[str, int] = {}
    for suite, _vector in selected:
        per_suite[suite["id"]] = per_suite.get(suite["id"], 0) + 1
    print(f"selection: {len(selected)} vector(s) across {len(per_suite)} suite(s)")
    for suite_id, count in sorted(per_suite.items()):
        declared = next(s["vector_count"] for s in manifest["suites"] if s["id"] == suite_id)
        note = "" if count == declared else f" (suite declares {declared})"
        print(f"  {suite_id}: {count}{note}")
    unpinned = [ref for ref in (f"{s['id']}#{v}" for s, v in selected) if ref not in pinned]
    print(
        f"  pinned assets with a recorded SHA-256: {len(selected) - len(unpinned)}; "
        f"unpinned (hash reported by lock, upstream MD5 still verified on acquisition): {len(unpinned)}"
    )


def cmd_validate(args) -> int:
    manifest = load_json(args.manifest, "corpus manifest")
    issues = validate_schema(manifest)
    if args.pass_sets:
        pass_sets = load_json(args.pass_sets, "r11 pass sets")
    else:
        pass_sets = {}
    fluster = Path(args.fluster) if args.fluster else None
    if fluster is not None:
        if not fluster.is_dir():
            issues.append(f"--fluster directory does not exist: {fluster}")
            fluster = None
        else:
            try:
                issues.extend(cross_check(manifest, fluster, pass_sets))
            except CorpusError as error:
                issues.append(str(error))
    else:
        print("note: --fluster was not given, so pinned suite digests, failure classifications, "
              "pass/fail completeness and the full-corpus selection were not checked (run with a "
              "pinned Fluster checkout in CI).")
    for issue in issues:
        print(f"FAIL: {issue}", file=sys.stderr)
    if issues:
        print(f"\n{len(issues)} problem(s); the manifest is not usable as written.", file=sys.stderr)
        return 1
    vectors = sum(len(suite.get("assets") or {}) for suite in manifest["suites"])
    smoke_bytes = sum(
        asset.get("bytes") or 0
        for suite in manifest["suites"]
        for vector, asset in (suite.get("assets") or {}).items()
        if f"{suite['id']}#{vector}" in manifest["smoke"]["entries"]
    )
    declared_total = sum(suite["vector_count"] for suite in manifest["suites"])
    if fluster is not None:
        # Regression for a real defect: --all used to enumerate only the handful of assets the
        # manifest pins hashes for. The full-corpus selection must match the declared vectors.
        try:
            plan = select_assets(
                manifest,
                argparse.Namespace(vector=None, suite=None, smoke=False, all=True,
                                   confirm_large_corpus=True, dry_run=True),
                fluster,
            )
        except CorpusError as error:
            plan = []
            issues.append(f"full-corpus selection failed: {error}")
        if len(plan) != declared_total:
            issues.append(
                f"full-corpus selection resolves {len(plan)} vectors but the suites declare "
                f"{declared_total}; `fetch --all` would not acquire the documented corpus"
            )
        if issues:
            for issue in issues:
                print(f"FAIL: {issue}", file=sys.stderr)
            print(f"\n{len(issues)} problem(s); the manifest is not usable as written.", file=sys.stderr)
            return 1
        print_selection_plan(plan, manifest)
    print(
        f"manifest OK: {len(manifest['suites'])} suites, "
        f"{declared_total} upstream vectors, "
        f"{len(manifest['known_issues'])} documented failure classes, "
        f"{vectors} pinned assets ({smoke_bytes} smoke bytes of "
        f"{manifest['smoke']['max_bytes']} allowed), "
        f"{len(manifest['generated'])} generated matrices"
    )
    return 0


def cmd_verify(args) -> int:
    manifest = load_json(args.manifest, "corpus manifest")
    issues = validate_schema(manifest)
    pass_sets = load_json(args.pass_sets, "r11 pass sets") if args.pass_sets else {}
    fluster = Path(args.fluster) if args.fluster else None
    if fluster is not None:
        if not fluster.is_dir():
            issues.append(f"--fluster directory does not exist: {fluster}")
        else:
            try:
                issues.extend(cross_check(manifest, fluster, pass_sets))
            except CorpusError as error:
                issues.append(str(error))
    issues.extend(verify_cache(manifest, Path(args.cache), fluster, args.require))
    for issue in issues:
        print(f"FAIL: {issue}", file=sys.stderr)
    if issues:
        print(f"\n{len(issues)} problem(s) in the corpus cache or manifest.", file=sys.stderr)
        return 1
    print(f"cache verified: {args.cache} (require={args.require})")
    return 0


def cmd_fetch(args) -> int:
    manifest = load_json(args.manifest, "corpus manifest")
    issues = validate_schema(manifest)
    if issues:
        for issue in issues:
            print(f"FAIL: {issue}", file=sys.stderr)
        return 1
    if not args.fluster:
        print("FAIL: --fluster is required to fetch: suite definitions are pinned by digest and "
              "vector checksums come from them.", file=sys.stderr)
        return 1
    pass_sets = load_json(args.pass_sets, "r11 pass sets") if args.pass_sets else {}
    fluster = Path(args.fluster)
    try:
        issues.extend(cross_check(manifest, fluster, pass_sets))
    except CorpusError as error:
        issues.append(str(error))
    if issues:
        for issue in issues:
            print(f"FAIL: {issue}", file=sys.stderr)
        return 1
    cache = Path(args.cache)
    selected = select_assets(manifest, args, fluster)
    if args.dry_run:
        print_selection_plan(selected, manifest)
        print("dry run: nothing acquired, no cache or network access.")
        return 0
    # Refuse malformed evidence before accepting any new downloads.
    load_lock(cache)
    indexes: dict[str, SuiteIndex] = {}
    fetched = 0
    cached = 0
    downloaded_bytes = 0
    skipped: list[str] = []
    for suite, vector in selected:
        index = indexes.setdefault(suite["id"], SuiteIndex(suite, fluster))
        try:
            record = acquire(cache, index, vector, mirror=args.mirror, offline=args.offline)
            if record["action"] == "fetched":
                record_acquisition(cache, index, vector, record)
        except CorpusError as error:
            # A single unreachable distributor must not hide the assets that were acquired;
            # failures are reported, capped so a large selection stays readable, and the exit
            # status stays non-zero.
            if len(skipped) < 5:
                print(f"FAIL: {error}", file=sys.stderr)
            elif len(skipped) == 5:
                print("FAIL: further failures suppressed; each is reported when its vector is "
                      "selected individually with --vector", file=sys.stderr)
            skipped.append(f"{suite['id']}#{vector}")
            continue
        pinned = (suite.get("assets") or {}).get(vector, {}).get("asset_sha256")
        state = "pinned" if pinned else "unpinned (hash reported, not recorded in the manifest)"
        print(f"{record['action']:8} {suite['id']}#{vector} sha256={record['sha256'][:16]}… {state}")
        fetched += record["action"] == "fetched"
        cached += record["action"] == "cached"
        downloaded_bytes += record.get("download_bytes", 0)
    print(
        f"\n{fetched} fetched, {cached} already cached, {len(skipped)} failed under {cache}; "
        f"downloaded {downloaded_bytes} bytes of upstream files (excluding the pinned Fluster checkout)"
    )
    if fetched:
        print(
            f"recorded {len(load_lock(cache))} asset identit(ies) in {cache / 'corpus-lock.json'} "
            "(evidence for verify; the manifest is never edited)"
        )
    print("The cache keeps the Fluster layout, so it can be passed to tests/conformance.py as its positional resources argument.")
    if any(not (suite.get("assets") or {}).get(vector, {}).get("asset_sha256") for suite, vector in selected):
        print("Some selected vectors are unpinned: run `corpus.py lock` and have a reviewer record the "
              "hashes in tests/corpus/manifest.json (never automatically).")
    return 1 if skipped else 0


def cmd_lock(args) -> int:
    manifest = load_json(args.manifest, "corpus manifest")
    document = write_lock(Path(args.cache), manifest, Path(args.fluster) if args.fluster else None, Path(args.write) if args.write else None)
    if args.write:
        print(f"wrote {len(document['assets'])} asset identities to {args.write}")
    else:
        print(json.dumps(document, indent=2, sort_keys=True))
    return 0


# --------------------------------------------------------------------------------------
# Hermetic self-test
# --------------------------------------------------------------------------------------

def _mutate(manifest: dict, path: list, value: object) -> dict:
    """Deep-copy the manifest and replace one node.

    ``path`` is a sequence of keys and list indices; vector names contain dots, so a
    dotted string path would be ambiguous.
    """
    clone = json.loads(json.dumps(manifest))
    node = clone
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return clone


def _expect_failure(name: str, expected_substring: str, runner, failures: list[str]) -> None:
    try:
        problem = runner()
    except CorpusError as error:
        problem = str(error)
    if not problem:
        failures.append(f"{name}: expected a failure but validation passed")
        print(f"FAIL: {name}")
        return
    text = " | ".join(problem) if isinstance(problem, list) else str(problem)
    if expected_substring not in text:
        failures.append(f"{name}: failed for the wrong reason: {text[:400]}")
        print(f"FAIL: {name}")
        return
    print(f"PASS: {name}")


def self_test(fixtures: Path) -> int:
    failures: list[str] = []
    manifest_path = fixtures / "manifest-tiny.json"
    fluster = fixtures / "fluster"
    pass_sets_path = fixtures / "pass-sets-tiny.json"
    mirror = fixtures / "mirror"
    manifest = load_json(manifest_path, "tiny corpus manifest")
    pass_sets = load_json(pass_sets_path, "tiny pass sets")

    # The documented JSON Schema must not drift from the vocabularies the tool enforces.
    schema_path = HERE / "corpus-manifest.schema.json"
    if not schema_path.is_file():
        failures.append(f"schema file missing: {schema_path}")
        print("FAIL: schema file missing")
    else:
        schema = load_json(schema_path, "corpus manifest schema")
        defs = schema.get("$defs", {})
        expected = {
            "redistribution": REDISTRIBUTION_VALUES,
            "classes": ASSET_CLASSES,
            "coverage": COVERAGE_VALUES,
        }
        mismatched = [
            name
            for name, values in expected.items()
            if defs.get(name, {}).get("enum") != values
        ]
        if mismatched:
            failures.append(f"schema vocabularies drifted from the tool: {mismatched}")
            print("FAIL: schema vocabularies drifted from the tool")
        elif defs.get("smoke", {}).get("properties", {}).get("max_bytes", {}).get("minimum") != 1:
            failures.append("schema smoke.max_bytes definition drifted")
            print("FAIL: schema smoke definition drifted")
        else:
            print("PASS: documented schema matches the tool's vocabularies")

    issues = validate_schema(manifest)
    if issues:
        failures.append("valid fixture rejected: " + " | ".join(issues))
        print("FAIL: valid fixture rejected")
    else:
        print("PASS: valid fixture accepted")

    issues = cross_check(manifest, fluster, pass_sets)
    if issues:
        failures.append("valid fixture cross-check rejected: " + " | ".join(issues))
        print("FAIL: valid fixture cross-check rejected")
    else:
        print("PASS: classifications and pass sets agree with the pinned suite")

    # Selection regression: a suite-wide command must enumerate the pinned suite definition, not
    # the handful of assets the manifest happens to pin hashes for (one asset in this fixture).
    def stub_args(**kwargs):
        base = {"vector": None, "suite": None, "smoke": False, "all": False,
                "confirm_large_corpus": False, "dry_run": True}
        base.update(kwargs)
        return argparse.Namespace(**base)

    vector_count = len(json.loads((fluster / manifest["suites"][0]["suite_file"]).read_text())["test_vectors"])
    pinned_count = len(pinned_refs(manifest))
    if pinned_count >= vector_count:
        failures.append("fixture is not exercising the selection bug: every vector has a recorded hash")
        print("FAIL: fixture selection preconditions")
        pinned_count = vector_count
    try:
        all_selection = select_assets(manifest, stub_args(all=True, confirm_large_corpus=True), fluster)
        suite_selection = select_assets(manifest, stub_args(suite=[manifest["suites"][0]["id"]]), fluster)
        suite_id = manifest["suites"][0]["id"]
        hash_pinned = {
            ref.split("#", 1)[1] for ref in pinned_refs(manifest) if ref.startswith(suite_id + "#")
        }
        unpinned_vector = sorted({vector for _suite, vector in all_selection} - hash_pinned)[0]
        single = select_assets(
            manifest,
            stub_args(vector=[f"{manifest['suites'][0]['id']}#{unpinned_vector}"]),
            fluster,
        )
    except CorpusError as error:
        failures.append(f"selection failed: {error}")
        print("FAIL: selection")
        all_selection = suite_selection = single = []
    if len(all_selection) != vector_count or len(suite_selection) != vector_count:
        failures.append(
            f"suite-wide selection resolves {len(all_selection)}/{len(suite_selection)} vectors, "
            f"the pinned definition declares {vector_count}"
        )
        print("FAIL: suite-wide selection enumerates pinned assets instead of the suite")
    else:
        print(f"PASS: --suite/--all enumerate the pinned definition ({vector_count} vectors, {pinned_count} pinned)")
    if len(single) != 1:
        failures.append("an unpinned vector cannot be selected explicitly")
        print("FAIL: unpinned --vector selection")
    else:
        print("PASS: an unpinned vector can be selected explicitly and is reported, not silently pinned")

    _expect_failure(
        "suite digest drift is detected",
        "suite definition changed",
        lambda: cross_check(_mutate(manifest, ["suites", 0, "suite_file_sha256"], "0" * 64), fluster, pass_sets),
        failures,
    )
    _expect_failure(
        "unlicensed redistribution is rejected",
        "license_name",
        lambda: validate_schema(_mutate(manifest, ["suites", 0, "license", "redistribution"], "redistributable")),
        failures,
    )
    _expect_failure(
        "missing provenance is rejected",
        "suite_file_sha256",
        lambda: validate_schema(_mutate(manifest, ["suites", 0, "suite_file_sha256"], None)),
        failures,
    )
    _expect_failure(
        "an unverified hash cannot be presented as an expectation",
        "hash_status 'pending'",
        lambda: validate_schema(_mutate(manifest, ["suites", 0, "assets", "tiny-pass.webm", "asset_sha256"], None)),
        failures,
    )
    _expect_failure(
        "classification drift is detected",
        "no longer matches its selection",
        lambda: cross_check(_mutate(manifest, ["known_issues", 1, "vectors"], ["tiny-size-08x08.webm"]), fluster, pass_sets),
        failures,
    )
    _expect_failure(
        "an undocumented r11 failure is detected",
        "no documented class",
        lambda: cross_check(_mutate(manifest, ["known_issues", 0, "vectors"], ["tiny-pass.webm"]), fluster, pass_sets),
        failures,
    )
    _expect_failure(
        "passing a vector while calling it a failure is detected",
        "r11 passes these vectors",
        lambda: cross_check(
            _mutate(
                manifest,
                ["known_issues", 1, "vectors"],
                sorted(set(manifest["known_issues"][1]["vectors"]) | {"tiny-pass.webm"}),
            ),
            fluster,
            pass_sets,
        ),
        failures,
    )
    _expect_failure(
        "the smoke budget is enforced",
        "above the",
        lambda: validate_schema(_mutate(manifest, ["smoke", "max_bytes"], 1)),
        failures,
    )
    _expect_failure(
        "missing required coverage is detected",
        "required coverage",
        lambda: validate_schema(
            _mutate(
                manifest,
                ["smoke", "required_coverage"],
                manifest["smoke"]["required_coverage"] + ["444-format"],
            )
        ),
        failures,
    )
    _expect_failure(
        "personal paths never enter the manifest",
        "personal filesystem path",
        lambda: validate_schema(_mutate(manifest, ["suites", 0, "acquisition", "tool"], "sh /Users/someone/run.sh")),
        failures,
    )
    _expect_failure(
        "declaring a licence without naming it is rejected",
        "needs a named licence",
        lambda: validate_schema(
            _mutate(
                manifest,
                ["suites", 0, "license"],
                {**manifest["suites"][0]["license"], "terms_status": "identified", "license_name": None},
            )
        ),
        failures,
    )
    _expect_failure(
        "redistributing on unidentified terms is rejected",
        "not identified",
        lambda: validate_schema(
            _mutate(
                manifest,
                ["suites", 0, "license"],
                {**manifest["suites"][0]["license"], "redistribution": "redistributable"},
            )
        ),
        failures,
    )
    _expect_failure(
        "a licence field that is only a title is no longer enough",
        "describe what was checked",
        lambda: validate_schema(
            _mutate(
                manifest,
                ["suites", 0, "license"],
                {**manifest["suites"][0]["license"], "terms_note": ""},
            )
        ),
        failures,
    )
    _expect_failure(
        "a reproduction path must point at a checked-in producer script",
        "is not a file in this repository",
        lambda: validate_schema(
            _mutate(
                manifest,
                ["generated", 0, "reproduction"],
                {**manifest["generated"][0]["reproduction"], "producer": "tests/does-not-exist.sh"},
            )
        ),
        failures,
    )
    _expect_failure(
        "an invocation that does not run the producer is rejected",
        "does not reference it",
        lambda: validate_schema(
            _mutate(
                manifest,
                ["generated", 0, "reproduction"],
                {**manifest["generated"][0]["reproduction"], "invocation": "make all"},
            )
        ),
        failures,
    )

    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp)
        index = SuiteIndex(manifest["suites"][0], fluster)
        vector = "tiny-pass.webm"
        record = acquire(cache, index, vector, mirror=str(mirror))
        if record["action"] != "fetched" or not asset_path(cache, index, vector).is_file():
            failures.append("smoke acquisition did not place the asset in the Fluster layout")
            print("FAIL: smoke acquisition")
        else:
            print("PASS: smoke acquisition writes the Fluster <suite>/<vector>/<input_file> layout")

        again = acquire(cache, index, vector, mirror=str(mirror))
        if again["action"] != "cached":
            failures.append("an already acquired asset was not recognized as cached")
            print("FAIL: cache reuse")
        else:
            print("PASS: an already acquired asset is reused instead of re-downloaded")

        if verify_cache(manifest, cache, fluster, "none"):
            failures.append("a cache holding only part of the corpus reported a hard error under require=none")
            print("FAIL: partial cache")
        else:
            print("PASS: a partial cache is accepted under require=none")

        _expect_failure(
            "a missing smoke asset is reported",
            "missing asset",
            lambda: verify_cache(manifest, cache, fluster, "smoke"),
            failures,
        )

        target = asset_path(cache, index, vector)
        original = target.read_bytes()
        target.write_bytes(original + b"tampered")
        _expect_failure(
            "a tampered asset is rejected",
            "hash mismatch",
            lambda: verify_cache(manifest, cache, fluster, "none"),
            failures,
        )
        _expect_failure(
            "a cached asset is never silently overwritten",
            "never overwrites a pinned asset",
            lambda: acquire(cache, index, vector, mirror=str(mirror)),
            failures,
        )
        target.write_bytes(original)

        offline = Path(tmp) / "empty"
        _expect_failure(
            "offline acquisition fails with an actionable command",
            "--offline was requested",
            lambda: acquire(offline, index, "tiny-size-08x08.webm", offline=True),
            failures,
        )
        _expect_failure(
            "a mismatched upstream checksum is refused before the asset is accepted",
            "upstream file changed",
            lambda: acquire(
                Path(tmp) / "md5",
                index,
                "tiny-pass.webm",
                mirror=str(fixtures / "mirror-bad-md5"),
            ),
            failures,
        )

        unpinned = next(
            vector
            for vector in sorted(index.by_name)
            if not (manifest["suites"][0]["assets"] or {}).get(vector, {}).get("asset_sha256")
        )
        acquire(cache, index, unpinned, mirror=str(mirror))
        lock = write_lock(cache, manifest, fluster, cache / "corpus-lock.json")
        lock_ref = f"{manifest['suites'][0]['id']}#{unpinned}"
        if lock_ref not in lock["assets"]:
            failures.append(f"an unpinned acquired asset is missing from the lock: {lock_ref}")
            print("FAIL: lock coverage for unpinned assets")
        else:
            print("PASS: the lock records an unpinned asset that the manifest does not pin")
        unpinned_path = asset_path(cache, index, unpinned)
        unpinned_original = unpinned_path.read_bytes()
        unpinned_path.write_bytes(unpinned_original + b"tampered")
        _expect_failure(
            "a tampered unpinned asset is caught by the acquisition lock",
            "hash mismatch against the local acquisition lock",
            lambda: verify_cache(manifest, cache, fluster, "none"),
            failures,
        )
        unpinned_path.write_bytes(unpinned_original)

        ref = f"{manifest['suites'][0]['id']}#{vector}"
        if lock["assets"].get(ref, {}).get("asset_sha256") != manifest["suites"][0]["assets"][vector]["asset_sha256"]:
            failures.append("lock output does not match the independently computed hash")
            print("FAIL: lock output")
        else:
            print("PASS: lock output matches an independently computed hash and does not edit the manifest")

    if failures:
        print(f"\n{len(failures)} self-test failure(s):", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("\ncorpus manifest self-test passed: negative cases, acquisition, verification and locking.")
    return 0


def cmd_self_test(args) -> int:
    return self_test(Path(args.fixtures))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="corpus manifest (default: tests/corpus/manifest.json)")
    parser.add_argument("--pass-sets", type=Path, default=DEFAULT_PASS_SETS, help="r11 pass sets used for classification checks")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate the manifest, policies and pins (offline)")
    validate.add_argument("--fluster", type=Path, help="pinned Fluster checkout; enables suite digest and classification checks")
    validate.set_defaults(func=cmd_validate)

    verify = sub.add_parser("verify", help="validate, then check the local cache against pinned hashes")
    verify.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    verify.add_argument("--fluster", type=Path)
    verify.add_argument("--require", choices=["none", "smoke", "all"], default="none",
                        help="none: only check assets that are present; smoke/all: missing requested assets fail")
    verify.set_defaults(func=cmd_verify)

    fetch = sub.add_parser("fetch", help="download selected assets on demand into the Fluster layout")
    fetch.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    fetch.add_argument("--fluster", type=Path, required=True)
    fetch.add_argument("--mirror", help="base URL or path mirroring upstream hosts (Fluster-compatible rewriting)")
    fetch.add_argument("--smoke", action="store_true", help="fetch only the bounded smoke subset")
    fetch.add_argument("--suite", action="append", help="fetch every pinned asset of this suite id")
    fetch.add_argument("--vector", action="append", help="fetch one '<suite id>#<vector>' asset")
    fetch.add_argument("--all", action="store_true", help="fetch every vector of every suite (requires --confirm-large-corpus)")
    fetch.add_argument("--confirm-large-corpus", action="store_true", help="acknowledge the full-corpus download")
    fetch.add_argument("--dry-run", action="store_true", help="print the selection plan without acquiring anything")
    fetch.add_argument("--offline", action="store_true", help="refuse network access; only accept cached assets")
    fetch.set_defaults(func=cmd_fetch)

    lock = sub.add_parser("lock", help="report SHA-256 for acquired assets (never edits the manifest)")
    lock.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    lock.add_argument("--fluster", type=Path)
    lock.add_argument("--write", type=Path, help="write the lock document here instead of printing it")
    lock.set_defaults(func=cmd_lock)

    selftest = sub.add_parser("self-test", help="hermetic negative-case and acquisition checks (no network)")
    selftest.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    selftest.set_defaults(func=cmd_self_test)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except CorpusError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
