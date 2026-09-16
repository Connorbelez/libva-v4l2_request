# Maintenance and handoff

This procedure assigns decisions to repository maintainers and a named reviewer for
each change. It does not invent a review rota, response-time commitment or release
cadence. The triage and backport defaults below are proposals for maintainer approval
with this document. The [agent workflow](AGENT_WORKFLOW.md) remains the authority for
claims, status meanings and completion.

## Triage a report

1. Send security-sensitive reports through [SECURITY.md](../SECURITY.md). Keep the public
   contact request free of vulnerability details. Do not copy a private report into an issue.
2. Identify the owning layer using [CONTRIBUTING.md](../CONTRIBUTING.md), check duplicates,
   existing support gaps and native blocked-by links, and record missing evidence.
3. Apply the existing priority definitions: P0 for release-blocking faults or essential
   correctness evidence; P1 for important compatibility, reliability or delivery work;
   P2 for extended coverage, optimisation or hardware-dependent expansion. Record why.
4. Select a reviewer familiar with the layer and record their agreement in the issue or
   PR. Repository ownership does not mean a particular person has accepted review work.
5. Mark a scoped leaf ready only when its prerequisites, resources and permissions are
   satisfied. Use blocked with a named dependency otherwise. New forms deliberately
   do not auto-assign readiness, severity or a reviewer.

A reproducible report identifies source/package versions, device/kernel/client,
decoder selection, expected/actual output and a minimal redacted reproducer. Accept
`unknown` where the reporter cannot establish a version or the loaded-module identity.
Ask for the missing fact rather than a full system journal or private recording.
Research identifies an uncertainty and decision; its closure does not deliver a feature.

## Review ownership and evidence

The contributor owns the implementation and its evidence. A maintainer owns priority,
reviewer selection, acceptance and merge. Record independent review when it occurs;
a contributor's self-check and a passing CI job do not count as independent review.
Security, shared lifetime/reference and kernel work warrant a reviewer with relevant
expertise. Pending expertise or hardware evidence remains visible at handoff.

Use the PR template to tie every acceptance criterion to a change or result. Preserve
raw suite denominators and exact vector sets, including failed and aborted runs.
Keep hardware output, software fallback, expected rejection, wrong output and untested
cases separate. For this repository, the existing CI job is `userspace`; driver changes
must satisfy the up-to-date required `userspace` check in the driver repository.
Do not bypass branch rules or turn missing hardware evidence into a green support claim.

## Dependency updates and backports

For an update, record old/new source revisions and dependency versions, upstream origin,
licence/credits, compatibility impact and the relevant before/after checks. Review build
dependencies, workflow actions and package pins as code. Keep updates scoped and avoid
unrelated automated version churn or new host/secrets access for public PRs.

Fix the owning default branch first: driver `avd-fixes`, companion `main`. A maintainer
may approve another sequence for a coordinated security fix and record the reason privately.
Backport only to an explicitly named target the maintainer agrees to maintain; this
procedure does not create an LTS branch or promise support for historical packages.
Link the original fix, preserve authorship, use `git cherry-pick -x` when appropriate,
and record conflict resolutions. Run the target branch's checks and qualify its actual
dependencies. Never mark a backport delivered before that target is merged and available.

## Release decision

Release evidence should identify the source commit, package/driver hashes, dependency
versions, affected support rows, commands/results, known failures and rollback procedure.
Review the exact files being shipped. In the companion repository, validate the package
source pin, version and vendor marker together; build/check/inspect before any separately
authorised installation. Keep imported credits intact.

Userspace checks do not certify display behaviour, firmware, installation or boot safety.
The driver [support contract](https://github.com/iconidentify/libva-v4l2_request/blob/avd-fixes/docs/SUPPORT.md)
controls codec/device claims. The companion [release qualification ticket](https://github.com/iconidentify/omarchy-m1-video/issues/27)
owns the boot/platform release decision. Hardware and kernel authorisation gates remain
in force. A maintainer records the decision, its scope and outstanding risks; a research
result or green CI job alone cannot promote a platform to stable.

## Handoff or stop

Record the ticket/session, base and final commits, PRs in both repositories, each acceptance
criterion and its evidence, tests not run and why, the reviewer and whether review was
independent, remaining blockers, and the next exact step. Include active processes and
the hardware lease/final idle state, or explicitly say no hardware was used. Renew or
release the implementation claim as required by the live workflow.

Request `status:review` when the concrete result is ready; contributors without permission
can request the label change in the issue. Keep cross-repository work open until both
owning default branches contain the required changes and all criteria have evidence.
After acceptance, update directly unblocked dependencies and close only the completed
leaf. Do not close an epic or feature claim because a research document was merged.
