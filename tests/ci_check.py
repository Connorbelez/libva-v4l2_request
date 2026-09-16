#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""CI aggregate evaluation, offline fixtures and workflow-wiring audit.

Three modes, one source of truth:

  check    Evaluate the GitHub Actions ``needs`` context of the aggregate
           ``userspace`` job.  The required job set is the ``needs:`` list of
           the ``userspace`` job in the workflow file itself, so the evaluator
           can never disagree with the wiring it judges.  Exits 0 only when
           every required job actually ran and succeeded; a missing result,
           failure, cancellation or skip is a hard error.

  fixtures Offline negative fixtures for the evaluation and audit logic.  They
           exercise the same functions the aggregate job uses: all-success
           passes, and failure, cancellation, skip, missing, malformed and
           wiring violations must each be reported.

  audit    Verify the wiring and safety invariants of
           ``.github/workflows/checks.yml``: every non-aggregate job must be
           listed in ``userspace.needs``; the aggregate must evaluate even
           after a dependency failure (``if: !cancelled()``); external actions
           and container images must be pinned by full commit SHA/digest;
           only GitHub-hosted runner labels are allowed; matrix jobs must not
           be fail-fast; the workflow must only request ``contents: read``;
           and no ``pull_request_target``, ``continue-on-error``, self-hosted
           runner or hardware-guard lease may appear, so public PR CI never
           opens the decoder or handles secrets.

The parser is intentionally strict: it only accepts the YAML subset used by
the workflow file and fails loudly on anything it cannot verify, so a future
edit that falls outside the audited invariants fails this check instead of
silently bypassing it.  Standard library only.
"""

import argparse
import json
import re
import sys

AGGREGATE_JOB = "userspace"
AGGREGATE_IF = re.compile(r"if:\s*\$\{\{\s*!cancelled\(\)\s*\}\}")
HOSTED_RUNNER = re.compile(r"^ubuntu-(latest|[0-9]{2}\.[0-9]{2}(-arm)?)$")
ACTION_PIN = re.compile(r"^[\w.-]+/[\w.-]+@(?:[0-9a-f]{40}|sha256-[0-9a-f]{12,64})$")
CONTAINER_PIN = re.compile(r"^[A-Za-z0-9._/-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")
JOB_ID = re.compile(r"^  ([a-zA-Z0-9][a-zA-Z0-9_-]*):[ \t]*$")
PERMISSION = re.compile(r"^  ([a-z-]+):\s*([a-z]+)\s*$")
REVIEW_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

# Strings that must never appear in the offline CI workflow.
FORBIDDEN = ("pull_request_target", "continue-on-error", "self-hosted",
             "LIBVA_HW_GUARD_LEASE", "secrets.")

# Runtimes GitHub-hosted runners execute without a forced-runtime warning.
# Update this set (with a fresh allowlist review, below) when Actions moves
# the goalposts again -- this is what issue #63 was missing.
SUPPORTED_RUNTIMES = frozenset(["node24"])

# A full-SHA pin says nothing about the runtime its own action.yml declares.
# This is the reviewed record of that fact: one entry per commit that has
# ever appeared in a 'uses:' line, each read directly from that commit's
# action.yml (offline -- no network access from this script) and dated. A
# pin that is not here, or whose recorded runtime has fallen out of
# SUPPORTED_RUNTIMES, fails the audit; that is the whole point.
ACTION_RUNTIME_ALLOWLIST = {
    # actions/checkout v4.4.0 -- node20, superseded by v5.1.0 below.
    "actions/checkout@11d5960a326750d5838078e36cf38b85af677262": {
        "runtime": "node20", "reviewed": "2026-09-16",
    },
    # actions/checkout v5.1.0 -- node24, current pin per PR #64.
    "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09": {
        "runtime": "node24", "reviewed": "2026-09-16",
    },
    # actions/cache v4.3.0 -- node20, superseded by v5.1.0 below.
    "actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830": {
        "runtime": "node20", "reviewed": "2026-09-16",
    },
    # actions/cache v5.1.0 -- node24, current pin per PR #64.
    "actions/cache@caa296126883cff596d87d8935842f9db880ef25": {
        "runtime": "node24", "reviewed": "2026-09-16",
    },
    # Fixture-only pin exercised by run_fixtures(); not a real dependency.
    "actions/checkout@1111111111111111111111111111111111111111": {
        "runtime": "node24", "reviewed": "2026-09-16",
    },
}


def validate_allowlist(allowlist):
    """Fail loudly on a malformed entry: no allowlist entry may lack a
    runtime or a parseable review date -- an unreviewed pin must read as
    unreviewed, never silently pass because a field is missing."""
    problems = []
    for pin, entry in allowlist.items():
        if not isinstance(entry, dict):
            problems.append("allowlist entry for '%s' is not a mapping" % pin)
            continue
        runtime = entry.get("runtime")
        if not runtime or not isinstance(runtime, str):
            problems.append("allowlist entry for '%s' has no runtime" % pin)
        reviewed = entry.get("reviewed")
        if not isinstance(reviewed, str) or not REVIEW_DATE.match(reviewed):
            problems.append("allowlist entry for '%s' has no valid review "
                            "date (expected YYYY-MM-DD)" % pin)
    return problems


def parse_jobs(text):
    """Split the ``jobs:`` section into job id -> raw block text (strict)."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == "jobs:":
            start = i
            break
    if start is None:
        raise ValueError("workflow has no 'jobs:' section")
    jobs = {}
    current = None
    for line in lines[start + 1:]:
        match = JOB_ID.match(line)
        if match:
            current = match.group(1)
            jobs[current] = []
            continue
        if current is not None:
            if line.strip() == "" or line[0] in " \t-":
                jobs[current].append(line)
            else:
                current = None  # unindented line: the jobs section ended
    if not jobs:
        raise ValueError("workflow has no jobs")
    return {job: "\n".join(block) for job, block in jobs.items() if block}


def required_jobs(jobs):
    """The required job set = userspace.needs (fail loudly on surprises)."""
    problems = []
    if AGGREGATE_JOB not in jobs:
        return None, ["workflow is missing the '%s' aggregate job"
                      % AGGREGATE_JOB]
    block = jobs[AGGREGATE_JOB]
    needs = re.search(r"^\s*needs:\s*\[([^\]]*)\]\s*$", block, re.M)
    if not needs:
        return None, ["the '%s' job must declare its required children as a "
                      "single flow list, e.g. needs: [build-test, ...]"
                      % AGGREGATE_JOB]
    required = [item.strip() for item in needs.group(1).split(",") if item.strip()]
    if not required:
        return None, ["'%s'.needs must not be empty" % AGGREGATE_JOB]
    if not AGGREGATE_IF.search(block):
        problems.append("the '%s' job must use 'if: !cancelled()' so it is "
                         "evaluated even after a dependency failure"
                         % AGGREGATE_JOB)
    return required, problems


def evaluate(needs_results, required):
    """Return the list of problems for a needs {job: result} mapping."""
    problems = []
    for job in required:
        if job not in needs_results:
            problems.append("required job '%s' has no result: it did not run "
                            "(skipped, filtered or removed)" % job)
        elif needs_results[job] != "success":
            problems.append("required job '%s' finished with result '%s', "
                            "expected 'success'" % (job, needs_results[job]))
    extra = sorted(set(needs_results) - set(required))
    if extra:
        problems.append("needs context contains jobs outside the required "
                        "set %s: %s" % (required, extra))
    return problems


def parse_needs_json(raw):
    """Parse the toJSON(needs) context into {job: result} (fail on garbage)."""
    try:
        context = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        return None, ["needs context is not valid JSON: %s" % exc]
    if not isinstance(context, dict) or not context:
        return None, ["needs context must be a non-empty JSON object"]
    results = {}
    for job, info in context.items():
        if not isinstance(info, dict) or "result" not in info:
            return None, ["needs entry '%s' has no 'result' field" % job]
        results[job] = info["result"]
    return results, []


def check_permissions(text, problems):
    """The workflow may only request contents: read."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == "permissions:":
            start = i
            break
    if start is None:
        problems.append("workflow must declare a top-level permissions block")
        return
    granted = []
    for line in lines[start + 1:]:
        match = PERMISSION.match(line)
        if match:
            granted.append((match.group(1), match.group(2)))
        elif line.strip() == "":
            continue
        else:
            break
    if granted != [("contents", "read")]:
        problems.append("workflow permissions must be exactly 'contents: read',"
                        " found: %s" % (granted or "nothing"))


def audit(text, path="<workflow>"):
    """Return wiring and safety problems for the workflow text (strict)."""
    problems = []
    for needle in FORBIDDEN:
        if needle in text:
            problems.append("workflow must not contain '%s'" % needle)
    check_permissions(text, problems)
    problems.extend(validate_allowlist(ACTION_RUNTIME_ALLOWLIST))
    for match in re.finditer(r"^\s*-?\s*uses:\s*(\S+)\s*$", text, re.M):
        pin = match.group(1)
        if not ACTION_PIN.match(pin):
            problems.append("action '%s' is not pinned by full commit SHA "
                            "(expected owner/repo@<40-hex or sha256-hex>)"
                            % pin)
            continue
        entry = ACTION_RUNTIME_ALLOWLIST.get(pin)
        if entry is None:
            problems.append("action '%s' is pinned by commit but not in the "
                            "reviewed commit-to-runtime allowlist; read its "
                            "action.yml and add an entry with the runtime "
                            "and a review date" % pin)
        elif entry["runtime"] not in SUPPORTED_RUNTIMES:
            problems.append("action '%s' targets runtime '%s' (reviewed "
                            "%s), which is not in the supported set %s; "
                            "repin to a release with a supported runtime" %
                            (pin, entry["runtime"], entry["reviewed"],
                             sorted(SUPPORTED_RUNTIMES)))
    for match in re.finditer(r"^\s*container:\s*(\S+)\s*$", text, re.M):
        if not CONTAINER_PIN.match(match.group(1)):
            problems.append("container image '%s' is not pinned by a sha256 "
                            "digest" % match.group(1))
    for match in re.finditer(r"^\s*runs-on:\s*(.+?)\s*$", text, re.M):
        label = match.group(1)
        if label == "${{ matrix.os }}":
            continue
        if not HOSTED_RUNNER.match(label):
            problems.append("runs-on '%s' is not a GitHub-hosted runner label"
                            % label)
    for match in re.finditer(r"^\s*-?\s*os:\s*(\S+)\s*$", text, re.M):
        if not HOSTED_RUNNER.match(match.group(1)):
            problems.append("matrix os '%s' is not a GitHub-hosted runner "
                            "label" % match.group(1))
    try:
        jobs = parse_jobs(text)
    except ValueError as exc:
        problems.append("%s: %s" % (path, exc))
        return problems
    required, needs_problems = required_jobs(jobs)
    problems.extend(needs_problems)
    if required is None:
        return problems
    for job in sorted(set(jobs) - {AGGREGATE_JOB} - set(required)):
        problems.append("job '%s' is not listed in %s.needs; the aggregate "
                        "would not gate it" % (job, AGGREGATE_JOB))
    for job in required:
        if job not in jobs:
            problems.append("%s.needs references job '%s' which does not "
                            "exist" % (AGGREGATE_JOB, job))
    for job, block in jobs.items():
        if "matrix:" in block and "fail-fast: false" not in block:
            problems.append("job '%s' has a matrix but no 'fail-fast: false'; "
                            "a failing leg would cancel the other legs' "
                            "evidence" % job)
    return problems


FIXTURE_WORKFLOW = """name: Checks
on: [push, pull_request]
permissions:
  contents: read
jobs:
  build-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@1111111111111111111111111111111111111111
  static-analysis:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@1111111111111111111111111111111111111111
  userspace:
    needs: [build-test, static-analysis]
    if: ${{ !cancelled() }}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@1111111111111111111111111111111111111111
      - run: python3 tests/ci_check.py check --workflow x --needs-json '{}'
"""

REQUIRED = ["alpha", "beta"]


def run_fixtures():
    """Offline negative fixtures for evaluate(), parse_needs_json(), audit()."""
    failures = []
    total = [0]

    def expect(name, problems, must_fail=False, contains=None):
        total[0] += 1
        bad = []
        if must_fail:
            if not problems:
                bad.append("expected a problem, none raised")
            elif contains and not any(contains in p for p in problems):
                bad.append("expected a problem mentioning '%s', got: %s"
                           % (contains, problems))
        elif problems:
            bad = ["unexpected problems: %s" % problems]
        status = "PASS" if not bad else "FAIL"
        print("%s  %s" % (status, name))
        for line in bad:
            print("      - " + line)
        if bad:
            failures.append(name)

    expect("all required jobs successful passes",
           evaluate({"alpha": "success", "beta": "success"}, REQUIRED))
    expect("a failing child is reported",
           evaluate({"alpha": "failure", "beta": "success"}, REQUIRED),
           must_fail=True, contains="'failure'")
    expect("a cancelled child is reported",
           evaluate({"alpha": "cancelled", "beta": "success"}, REQUIRED),
           must_fail=True, contains="'cancelled'")
    expect("a skipped child is reported",
           evaluate({"alpha": "success", "beta": "skipped"}, REQUIRED),
           must_fail=True, contains="'skipped'")
    expect("a missing required job is reported",
           evaluate({"alpha": "success"}, REQUIRED),
           must_fail=True, contains="has no result")
    expect("an unexpected extra job is reported",
           evaluate({"alpha": "success", "beta": "success",
                     "gamma": "success"}, REQUIRED),
           must_fail=True, contains="outside the required set")

    def malformed():
        _, problems = parse_needs_json("{not json")
        return problems or ["malformed JSON was accepted"]

    expect("malformed needs JSON is rejected", malformed(),
           must_fail=True, contains="not valid JSON")

    def bad_entry():
        _, problems = parse_needs_json('{"alpha": {"outputs": {}}}')
        return problems or ["entry without a result was accepted"]

    expect("needs entry without result is rejected", bad_entry(),
           must_fail=True, contains="no 'result' field")

    def valid_context():
        results, problems = parse_needs_json(
            '{"alpha": {"result": "success", "outputs": {}},'
            ' "beta": {"result": "success"}}')
        if problems:
            return problems
        if results != {"alpha": "success", "beta": "success"}:
            return ["unexpected parse result: %r" % results]
        return []

    expect("valid needs context parses", valid_context())

    def good_workflow():
        return audit(FIXTURE_WORKFLOW)

    expect("correctly wired workflow passes the audit", good_workflow())

    def forgotten_job():
        text = FIXTURE_WORKFLOW.replace(
            "needs: [build-test, static-analysis]", "needs: [build-test]")
        return [p for p in audit(text) if "static-analysis" in p] or \
            ["audit accepted a job missing from the aggregate needs"]

    expect("job missing from aggregate needs is flagged", forgotten_job(),
           must_fail=True, contains="not listed")

    def dangling_needs():
        text = FIXTURE_WORKFLOW.replace(
            "needs: [build-test, static-analysis]",
            "needs: [build-test, does-not-exist]")
        return [p for p in audit(text) if "does-not-exist" in p] or \
            ["audit accepted a dangling needs reference"]

    expect("dangling aggregate needs reference is flagged", dangling_needs(),
           must_fail=True, contains="does not exist")

    def mutable_action():
        text = FIXTURE_WORKFLOW.replace(
            "actions/checkout@1111111111111111111111111111111111111111",
            "actions/checkout@v4", 1)
        return [p for p in audit(text) if "pinned" in p] or \
            ["audit accepted a mutable action tag"]

    expect("unpinned action reference is flagged", mutable_action(),
           must_fail=True, contains="pinned")

    def fail_fast_matrix():
        text = FIXTURE_WORKFLOW.replace(
            "  build-test:\n    runs-on: ubuntu-latest",
            "  build-test:\n    strategy:\n      matrix:\n        include:\n"
            "          - cc: gcc\n    runs-on: ubuntu-latest")
        return [p for p in audit(text) if "fail-fast" in p] or \
            ["audit accepted a fail-fast matrix job"]

    expect("missing fail-fast is flagged", fail_fast_matrix(),
           must_fail=True, contains="fail-fast")

    def self_hosted():
        text = FIXTURE_WORKFLOW.replace("runs-on: ubuntu-latest",
                                        "runs-on: self-hosted", 1)
        return [p for p in audit(text) if "hosted" in p] or \
            ["audit accepted a self-hosted runner label"]

    expect("self-hosted runner label is flagged", self_hosted(),
           must_fail=True, contains="hosted")

    def write_permissions():
        text = FIXTURE_WORKFLOW.replace("  contents: read",
                                        "  contents: write")
        return [p for p in audit(text) if "permissions" in p] or \
            ["audit accepted write permissions"]

    expect("write permissions are flagged", write_permissions(),
           must_fail=True, contains="contents: read")

    def unreviewed_pin():
        text = FIXTURE_WORKFLOW.replace(
            "actions/checkout@1111111111111111111111111111111111111111",
            "actions/checkout@2222222222222222222222222222222222222222", 1)
        return [p for p in audit(text) if "allowlist" in p] or \
            ["audit accepted a pin absent from the reviewed allowlist"]

    expect("action pinned to a commit outside the reviewed allowlist is "
           "flagged", unreviewed_pin(), must_fail=True, contains="allowlist")

    def unsupported_runtime_pin():
        # A real reviewed pin (actions/checkout v4.4.0): recorded as node20,
        # which issue #63 is about -- it must fail even though it is fully
        # reviewed and fully SHA-pinned.
        text = FIXTURE_WORKFLOW.replace(
            "actions/checkout@1111111111111111111111111111111111111111",
            "actions/checkout@11d5960a326750d5838078e36cf38b85af677262", 1)
        return [p for p in audit(text) if "node20" in p] or \
            ["audit accepted a reviewed pin with an unsupported runtime"]

    expect("action pinned to a reviewed but unsupported runtime is "
           "flagged", unsupported_runtime_pin(), must_fail=True,
           contains="node20")

    def allowlist_entry_without_review_date():
        bad = {"actions/checkout@" + "3" * 40: {"runtime": "node24"}}
        return validate_allowlist(bad) or \
            ["allowlist entry without a review date was accepted"]

    expect("allowlist entry without a review date is rejected",
           allowlist_entry_without_review_date(), must_fail=True,
           contains="review date")

    def allowlist_entry_without_runtime():
        bad = {"actions/checkout@" + "4" * 40: {"reviewed": "2026-09-16"}}
        return validate_allowlist(bad) or \
            ["allowlist entry without a runtime was accepted"]

    expect("allowlist entry without a runtime is rejected",
           allowlist_entry_without_runtime(), must_fail=True,
           contains="no runtime")

    if failures:
        print("%d/%d fixtures failed" % (len(failures), total[0]))
        sys.exit(1)
    print("%d/%d fixtures passed" % (total[0], total[0]))


def main():
    parser = argparse.ArgumentParser(
        description="CI aggregate evaluation, fixtures and workflow audit")
    sub = parser.add_subparsers(dest="mode", required=True)

    check = sub.add_parser("check",
                           help="evaluate the aggregate needs context")
    check.add_argument("--workflow", required=True,
                       help="workflow file (source of the required job set)")
    check.add_argument("--needs-json", required=True,
                       help="the toJSON(needs) context of the aggregate job")

    sub.add_parser("fixtures", help="run the offline negative fixtures")

    audit_mode = sub.add_parser("audit", help="audit the workflow wiring")
    audit_mode.add_argument("--workflow", required=True,
                            help="workflow file to audit")

    args = parser.parse_args()

    if args.mode == "fixtures":
        run_fixtures()
        return

    with open(args.workflow, "r", encoding="utf-8") as handle:
        text = handle.read()

    if args.mode == "audit":
        problems = audit(text, args.workflow)
        if problems:
            fail(problems)
        print("workflow wiring audit passed")
        return

    try:
        jobs = parse_jobs(text)
    except ValueError as exc:
        fail([str(exc)])
    required, problems = required_jobs(jobs)
    results, parse_problems = parse_needs_json(args.needs_json)
    problems = list(problems or []) + list(parse_problems or [])
    if results is not None and required is not None:
        problems += evaluate(results, required)
    if problems:
        fail(problems)
    print("aggregate '%s' gate passed: required jobs %s all ran and "
          "succeeded" % (AGGREGATE_JOB, required))


def fail(problems):
    for problem in problems:
        print("ci_check: " + problem, file=sys.stderr)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
