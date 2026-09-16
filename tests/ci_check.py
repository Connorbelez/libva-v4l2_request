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

# Strings that must never appear in the offline CI workflow.
FORBIDDEN = ("pull_request_target", "continue-on-error", "self-hosted",
             "LIBVA_HW_GUARD_LEASE", "secrets.")


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
    for match in re.finditer(r"^\s*-?\s*uses:\s*(\S+)\s*$", text, re.M):
        if not ACTION_PIN.match(match.group(1)):
            problems.append("action '%s' is not pinned by full commit SHA "
                            "(expected owner/repo@<40-hex or sha256-hex>)"
                            % match.group(1))
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
