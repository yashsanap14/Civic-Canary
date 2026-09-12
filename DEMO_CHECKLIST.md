# Demo and submission verification

## Before recording
- Deploy this revision; activate the worker and verify SES sender/recipient and IAM.
- Check CloudFront disables API caching and forwards the reviewer header and queries.
- Verify the app, worker and browser all use the same S3 bucket, tables and model settings.
- Confirm one timer/worker only, using systemctl and journalctl.
- Run Python tests, frontend tests/build, and offline evaluation. Keep results labeled offline.
- Run the optional real-model corpus evaluation and report its actual results if time permits.
- Use two public test pages under your control, with different layouts. Label them synthetic.
- Capture the real CloudFront URL and grant judges working access without publishing secrets.

## End-to-end walkthrough
1. Add a website with an objective and optional nonprofit guidance.
2. Wait for a worker inspection; show the baseline, screenshot and Strands node trace.
3. Confirm or edit suggested monitored sections.
4. Close the dashboard. Show the timer scheduling a no-change run.
5. Verify SUCCEEDED with no outbox event and no email for that run.
6. Change the controlled source (document, deadline, contact or availability text).
7. Let the next due scan execute; show matching run ID in browser logs, graph and finding.
8. Verify the proposed patch in the UI matches the persisted Strands packet.
9. Verify precisely one application send claim and SES message ID for the new finding.
10. Open the email decision link, enter the reviewer token and inspect before/after evidence.
11. Approve with notes; download the approved artifact.
12. Show the atomic review row with reviewer identity, reviewedAt, runId and source evidence.
13. Run unchanged again; verify no duplicate finding or notification.
14. Show a failed capture/model run and that no unsupported finding was created.

## Evidence log to fill from real execution
| Item | Observed value |
| --- | --- |
| Deployed revision | Not recorded yet |
| CloudFront endpoint | Not recorded yet |
| Initial inspection run ID | Not recorded yet |
| No-change scheduled run ID | Not recorded yet |
| Actionable scheduled run ID | Not recorded yet |
| Browser session ID | Not recorded yet |
| Bedrock model/profile | Not recorded yet |
| Finding ID | Not recorded yet |
| SES MessageId | Not recorded yet |
| Review ID and artifact | Not recorded yet |
| Measured human review time | Not measured |

## Submission materials
Use the current official challenge rules to verify final eligibility and submission fields.
Include the public repository/license, corrected architecture, working judge instructions,
AWS Builder ID, and a public video within the allowed length covering problem, audience,
impact and a working demonstration. Do not present mocked tests as a live AWS run, or
claim a 90+ judging score from implementation alone.
