#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Validate the JSON diagnostic records against the schema fixture.

Runs the `diagnostics emit-samples` test binary, which writes one record of
every category plus the driver summary and a rate-limit summary, and checks
field types, patterns, size limits, sequence numbers and redaction. Also
checks that the category list in the C enum, the fixture and
docs/DIAGNOSTICS.md agree, so the documented schema cannot drift.
"""
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "diagnostics" / "schema.json"
DOC = ROOT / "docs" / "DIAGNOSTICS.md"

TYPES = {"string": str, "integer": int, "array": list}


class SchemaError(Exception):
    pass


def fail(message):
    raise SchemaError(message)


def check_record(schema, line, index):
    limits = schema["limits"]
    try:
        record = json.loads(line)
    except json.JSONDecodeError as error:
        fail(f"record {index} is not JSON ({error}): {line!r}")
    summary = record.get("op") == "driver-init"
    max_bytes = limits["summary_record_bytes"] if summary else limits["record_bytes"]
    if len(line.encode()) + 1 > max_bytes:
        fail(f"record {index} exceeds {max_bytes} bytes")
    for key, kind in schema["required"].items():
        if not isinstance(record.get(key), TYPES[kind]) or isinstance(record.get(key), bool):
            fail(f"record {index} field {key!r} missing or not {kind}: {line}")
    for key, value in record.items():
        kind = schema["required"].get(key) or schema["optional"].get(key)
        if kind is None:
            fail(f"record {index} has undocumented field {key!r}")
        if not isinstance(value, TYPES[kind]) or isinstance(value, bool):
            fail(f"record {index} field {key!r} is not {kind}")
        pattern = schema["patterns"].get(key)
        if pattern and not re.fullmatch(pattern, value):
            fail(f"record {index} field {key!r}={value!r} does not match {pattern}")
    if record["schema"] != schema["schema"]:
        fail(f"record {index} has schema {record['schema']!r}")
    if record["level"] not in schema["levels"]:
        fail(f"record {index} has unknown level {record['level']!r}")
    if record["category"] not in schema["categories"]:
        fail(f"record {index} has unknown category {record['category']!r}")
    if len(record["msg"].encode()) > limits["msg_bytes"]:
        fail(f"record {index} message exceeds {limits['msg_bytes']} bytes")
    if "/home/" in line or "private.mkv" in line:
        fail(f"record {index} leaks a private path: {line}")
    return record


def doc_categories():
    text = DOC.read_text()
    section = text.split("## Categories", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"^\| `([a-z]+)` \|", section, re.MULTILINE)


GOOD = ('{"schema":"libva-v4l2request.diag/1","run":"0123456789abcdef","seq":1,'
        '"t_ms":0,"level":"error","category":"timeout","op":"request-wait",'
        '"errno":"ETIMEDOUT","ctx":1,"va_context":"0x02000000","msg":"failed"}')

BAD = {
    "not json": GOOD[:-1],
    "missing msg": GOOD.replace(',"msg":"failed"', ""),
    "unknown category": GOOD.replace('"timeout"', '"oops"'),
    "unknown level": GOOD.replace('"error"', '"fatal"'),
    "undocumented field": GOOD.replace('"ctx":1', '"ctx":1,"extra":1'),
    "boolean as integer": GOOD.replace('"seq":1', '"seq":true'),
    "bad run id": GOOD.replace("0123456789abcdef", "xyz"),
    "bad errno": GOOD.replace("ETIMEDOUT", "timed out"),
    "private path": GOOD.replace('"failed"', '"open /home/user/private.mkv"'),
    "oversized message": GOOD.replace('"failed"', '"' + "A" * 300 + '"'),
    "oversized record": GOOD.replace('"op":"request-wait"', '"op":"' + "a" * 1100 + '"'),
}


def self_test(schema):
    check_record(schema, GOOD, 0)
    for name, line in BAD.items():
        try:
            check_record(schema, line, 0)
        except SchemaError:
            continue
        fail(f"self-test: {name} record was accepted")


def main():
    if len(sys.argv) != 2:
        fail("usage: diag-schema.py <diagnostics test binary>")
    schema = json.loads(FIXTURE.read_text())
    self_test(schema)
    result = subprocess.run([sys.argv[1], "emit-samples"], capture_output=True,
                            text=True, timeout=30, check=False)
    if result.returncode:
        fail(f"emit-samples failed ({result.returncode}): {result.stderr}")

    enum_categories = re.findall(r"^category (\S+)$", result.stderr, re.MULTILINE)
    if enum_categories != schema["categories"]:
        fail(f"C categories {enum_categories} differ from fixture {schema['categories']}")
    if doc_categories() != schema["categories"]:
        fail(f"documented categories {doc_categories()} differ from fixture")

    lines = result.stdout.splitlines()
    records = [check_record(schema, line, i) for i, line in enumerate(lines)]
    if [r["seq"] for r in records] != list(range(1, len(records) + 1)):
        fail("sequence numbers are not consecutive from 1")
    if len({r["run"] for r in records}) != 1:
        fail("run identifier changed within one run")
    seen = {r["category"] for r in records}
    if seen != set(schema["categories"]):
        fail(f"samples cover {sorted(seen)}, expected every category")
    summary = [r for r in records if r.get("op") == "driver-init"]
    if len(summary) != 1 or summary[0]["decoders"][0]["card"] != 'avd "quoted"':
        fail("driver-init summary missing or card not preserved")
    if summary[0]["decoders"][0]["formats"] != ["S264", "????"]:
        fail(f"decoder formats not sanitized: {summary[0]['decoders'][0]['formats']}")
    if not any(r.get("op") == "suppressed" and r.get("suppressed") == 5 for r in records):
        fail("rate-limit summary record missing")
    print(f"diag-schema: {len(records)} records valid, "
          f"{len(schema['categories'])} categories match docs and enum")


if __name__ == "__main__":
    try:
        main()
    except SchemaError as error:
        print(f"diag-schema: {error}", file=sys.stderr)
        sys.exit(1)
