# Security reporting

Do not put suspected vulnerabilities, exploit details or private media in a public issue
or PR. Routine playback, installation and support-gap reports belong in this fork's
[issue chooser](https://github.com/iconidentify/libva-v4l2_request/issues/new/choose).

## Private reporting availability

On 16 September 2026, GitHub's authenticated
`GET /repos/iconidentify/libva-v4l2_request/private-vulnerability-reporting` endpoint returned
`{"enabled": false}`. No alternative private channel was documented in the repository
instructions at that revision. There is currently no verified private reporting route
documented for this fork; do not assume that an email address or account is monitored.

Check the repository's [security advisories](https://github.com/iconidentify/libva-v4l2_request/security/advisories) for a
**Report a vulnerability** button, because the setting may change. If it is available,
use GitHub's private reporting form. An advisory draft is private; an ordinary issue is not.
The [GitHub reporting instructions](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately)
explain this distinction.

If private reporting is still unavailable, open a blank issue containing only:

> Please provide a private security-reporting contact for this repository.

Use a neutral title such as `Private reporting contact request`. Do not include the
affected component, version, impact, reproducer, logs, attachments or exploit details
in that public request. Wait for a maintainer to establish a private channel before
sharing the report. The blank-issue option remains available for this purpose. Do not
send a speculative report to an invented address or to an upstream project.

## What to share after a private channel is established

Give the affected repository and source/package versions, expected and actual behaviour,
impact and uncertainty, and the smallest safe reproducer you are permitted to share.
Distinguish a malformed-API test from evidence of a media-triggered exploit. Include
only relevant, redacted diagnostics. Remove credentials, personal paths/URLs, unrelated
process arguments and personal media. Describe a synthetic reproducer if private media
cannot be shared. Do not reproduce a known decoder wedge merely to strengthen a report.

Agree with the maintainer on secure sample transfer, review and disclosure timing before
publishing sensitive details. This project makes no guaranteed response-time or fix-date
commitment. If there is no reply, keep the sensitive material private while seeking a
verified contact; a public bug form is not a private fallback.

## Fixes and disclosure

Maintainers should verify the affected layer and versions privately, choose a reviewer,
and agree a bounded regression and coordinated disclosure plan with the reporter.
Confirm a fix with the relevant offline checks and any separately authorised hardware
work. Do not publish a reproducer, revealing test, draft PR or advisory before that
coordination. Record imported authorship and credit reporters with their agreement.

The [maintenance procedure](docs/MAINTENANCE.md) covers target branches and backports.
A version string alone does not establish that a particular package contains a fix.
Use the actual source commit and package provenance. Security triage does not change
codec support tiers or certify boot stability.

Repository administrators can [enable private vulnerability reporting](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository).
If they do, update this policy and the issue chooser after checking the live route.
Enabling that setting is separate from merging documentation; no report has been sent
to test delivery.
