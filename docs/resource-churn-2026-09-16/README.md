# M1 resource campaign evidence — 2026-09-16

**Status: incomplete qualification after two interrupted attempts.** The normal and early-export 1,000-cycle
campaigns passed. The required one-hour soak was aborted after approximately
10.3 minutes because another application opened the decoder. It remains a failed,
incomplete run; issue #41 stays open. No new kernel fault was reported.

The retry started at 21:07 UTC after the user paused videos, but another decoder
client interrupted it at 21:31:09 UTC after 1,426 measured seconds and 6,063
cycles. Its resource measurements remained flat; the guard reported no new
kernel fault. The foreign process exited before it could be identified, and a
later read-only preflight was healthy and idle. Both attempts remain failed
and cannot be combined into an uninterrupted hour. A fresh exclusive window is
pending; the user has been asked to close video applications completely.

The retry used the same selected release driver (SHA-256
`696273b355e0dbd045fa80af6cd787118511424c79960d8c1c0e65db8becb769`)
and the merged harness at `0758361796905981d8855988f71f4c2ca5a66ff4`.

The [machine-readable report](../resource-churn-validation-2026-09-16.json)
contains source/build/device identities, input and independent software frame
hashes, declared limits, raw summary records, 100/500/1,000-cycle checkpoints and
guard outcomes. Both complete campaigns retain one initialized VA display until
all measurements finish. Their FD, dma-buf, mapping and RSS values remain flat;
allocator-accounted allocation/cache growth stays below 10 KiB.

`raw-evidence.tar.xz` includes all completed and aborted resource logs, synthetic
media, reference hashes, guard logs, calibration failures and the report builder.
No private media or unrelated process arguments are included. Public text replaces
the owner's absolute workspace prefix with `<workspace>`; the report records
both original and public member hashes. Binary media is unchanged. The archive
is evidence to inspect, not an installer or a script to run automatically.

Verify the archive and every public member without extracting or executing it,
from the repository root:

```sh
python3 - <<'PY'
import hashlib, json, tarfile
from pathlib import Path
report = json.loads(Path('docs/resource-churn-validation-2026-09-16.json').read_text())
archive = Path('docs') / report['archive']['path']
assert hashlib.sha256(archive.read_bytes()).hexdigest() == report['archive']['sha256']
expected = report['archive']['members']
with tarfile.open(archive, 'r:xz') as bundle:
    members = bundle.getmembers()
    assert len(members) == len(expected)
    assert {m.name for m in members} == set(expected)
    for member in members:
        assert member.isfile()
        data = bundle.extractfile(member).read()
        assert len(data) == expected[member.name]['size']
        assert hashlib.sha256(data).hexdigest() == expected[member.name]['public_sha256']
print('All public evidence members match the report')
PY
```

Run new campaigns using [RESOURCE_CHURN.md](../RESOURCE_CHURN.md), fresh output
paths and an exclusive hardware guard. The original report builder's absolute
source/artifact roots are placeholders in the public copy; adapt them to the
reviewed checkout and extracted evidence when replaying `make-evidence.py
--partial`. It rechecks every completed-run frame record against the independent
reference, sample identity/sequence, resource bounds and final guard state before
producing a report. The prior failed calibration and interrupted soak cannot
be substituted for the missing hour.

Remaining: a fresh uninterrupted 60-minute guarded soak, codec comparisons on the
exact selected O3 release binary, the updated interruption-wrapper hardware check,
and merge. The production source tree equals the earlier #35 qualification, but
its O2 binary is different. No browser rendering, concurrent API use, other
resolutions, kernel-module identity, boot behavior or installation is certified.
