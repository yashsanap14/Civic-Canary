# Activate the monitoring upgrade on the existing EC2 deployment

## Deployment status
The owner reports EC2/FastAPI/React/Nginx/CloudFront, AgentCore Browser, Bedrock,
DynamoDB and S3 are deployed. This change adds code and deployment templates locally.
The new timer, notifications and graph path have NOT been activated or verified in
that account by this implementation. No real email was sent during development.

## Configuration
The API and worker must read the SAME environment file and run the SAME checkout.

```dotenv
AWS_REGION=us-east-1
CIVIC_CANARY_MODE=aws
BROWSER_MODE=agentcore
AGENTCORE_BROWSER_ID=YOUR_EXISTING_BROWSER_ID
BEDROCK_MODEL_ID=YOUR_ACCESSIBLE_MODEL_OR_INFERENCE_PROFILE_ID
CIVIC_CANARY_SITES_TABLE=CivicCanarySites
CIVIC_CANARY_FINDINGS_TABLE=CivicCanaryFindings
CIVIC_CANARY_REVIEWS_TABLE=CivicCanaryReviews
CIVIC_CANARY_S3_BUCKET=civic-canary
CIVIC_CANARY_PUBLIC_URL=https://YOUR_CLOUDFRONT_HOST
CIVIC_CANARY_DEMO_BASE_URL=https://YOUR_CLOUDFRONT_HOST
CIVIC_CANARY_EMAIL_FROM=YOUR_VERIFIED_SES_SENDER
CIVIC_CANARY_EMAIL_TO=YOUR_REVIEWER_EMAIL
REVIEW_TOKEN_SHA256=YOUR_64_CHARACTER_SHA256_DIGEST
```

Keep other working browser/model settings. AWS credentials come from the instance role.
The old AWS_STORAGE_LAYOUT=existing option remains compatible; remove it to use the
canonical CIVIC_CANARY_* configuration above. There is no Runs DynamoDB table.
Set a strong reviewer token and keep its digest in the environment. Both variants now
use the new atomic commit_review API for approval/rejection.

## AWS activation
1. Adapt `deploy/ec2-role-policy.template.json` with the actual account, browser,
   model/profile/destination-region model ARNs, verified SES identity and recipient.
   Attach the resulting policy to the existing EC2 role. DynamoDB transaction writes
   are authorized by the underlying PutItem permissions on both tables.
2. Verify the sender in SES in us-east-1. In SES sandbox, verify the recipient too,
   or request production access. Leave email variables blank until ready: events remain
   pending and the worker reports NOT_CONFIGURED; it does not claim delivery.
3. Configure CloudFront `/api/*` with caching disabled, all query strings and the
   X-Review-Token header forwarded, and POST/OPTIONS permitted. Keep static asset caching.
   Keep Nginx proxying `/api/` to FastAPI. Restrict direct origin access according to
   the existing deployment's security controls. The API sends Cache-Control: no-store.
4. No new database table, GSI, bucket, API Gateway or Lambda is needed. Existing
   recordings and evidence remain in their existing bucket. Scope S3 DeleteObject
   only to processed `jobs/` and `outbox/` queue entries, not evidence or audit history.

## EC2 redeployment
Commit/push or copy this revision first. Replace paths, service names and service user
below with the existing deployment values. Do not start a second independent worker.
The checked-in unit uses `/opt/civic-canary` and the `civiccanary` user; edit those values
if the existing installation differs. Its ProtectHome=true assumes a checkout outside
/home. Do not use the broad CDK deployment script for this EC2 code upgrade.

```bash
cd /opt/civic-canary
git pull --ff-only
uv sync --extra dev
npm --prefix web ci
npm --prefix web run build
uv run pytest -q
# Edit the protected environment file with the values above.
sudoedit /etc/civic-canary.env
# Ensure the API systemd unit also loads /etc/civic-canary.env.
sudo systemctl restart civic-canary.service
# Backfill the bounded Recent Runs index once, with the same environment loaded.
set -a
source /etc/civic-canary.env
set +a
uv run python -m scripts.index_run_history
sudo cp deploy/civic-canary-monitor.service /etc/systemd/system/
sudo cp deploy/civic-canary-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now civic-canary-monitor.timer
sudo systemctl start civic-canary-monitor.service
sudo systemctl list-timers civic-canary-monitor.timer
sudo journalctl -u civic-canary-monitor.service -n 100 --no-pager
curl --fail http://127.0.0.1:8000/api/health
```

Read protected env files only as their authorized owner. The service account needs
read/execute access to the checkout and virtual environment; systemd loads EnvironmentFile.
If manually invoking the worker, use the SAME `/var/lib/civic-canary/worker.lock` via
flock so it cannot overlap the timer. OnUnitInactiveSec avoids concurrent timer jobs.
Four queued jobs are processed per invocation; the next invocation continues the queue.

## Data migration and bounded access
Domain fields are additive with defaults; existing sites/findings remain readable.
The API uses transactional review writes in either AWS layout. Historical review rows
are retained; new ones use reviewId/findingId/action/reviewedAt/reviewer/notes plus
runId, originalRecommendation, evidence and evidenceKeys.

Runs remain under `runs/`; a reverse-timestamp `recent-runs/` S3 index supports fetching
50 recent records without loading every historical run. Run-history backfill reads old
records once and writes index objects. It does not delete or alter original run data.
Findings/targets lack secondary indexes in the existing tables, so bounded Scan pages
are used rather than pretending a Query can work without an index. `/api/findings-page`
exposes a cursor; legacy list APIs return at most 100 records. The scheduler paginates
site configuration. Large multi-tenant deployments need an explicit indexing design.

## Notifications and recovery
One immutable event and one conditional send claim per finding prevent repeat alerts.
Queue records live under `outbox/`, immutable events under `notification-events/`, and
send receipts under `notification-claims/`. All include the originating run ID.
A timeout/crash after SES submission cannot safely be distinguished from delivered mail.
Such events are UNKNOWN_REQUIRES_RECONCILIATION, never blindly resent. Inspect SES logs
and the claim before a deliberate operational retry. This is not a claim of exactly-once
email delivery. Configure SES delivery/bounce events for operational delivery visibility.
Existing findings suppress repeated alerts; no-change runs create no outbox entry.

A crashed RUNNING scan is recorded FAILED on the next worker pass. Failed setup can be
retried from the UI. Other failures remain in Recent Runs; the next scheduled slot is a
fresh attempt. If AWS storage itself is unavailable, logging and a later retry are the
fallback: no system can persist a failure to an unavailable store.

## Security and scope
Production reads require the reviewer token, including evidence URL issuance. Email
links contain only a finding ID and require authentication; never include the token.
Approval downloads also require authentication. S3 evidence URLs expire after 5 minutes.
This is a single-team reviewer model, not tenant isolation or individual user accounts.

Only public HTTPS pages are accepted, with exact-host navigation, DNS/private-address
checks, read-only requests, blocked login/upload paths, bounded links/timeouts, and a
fresh managed browser context. DNS validation is defense in depth, not a network-level
DNS-pinning guarantee. Use restricted browser egress for higher-assurance deployments.
Strict same-host requests may reduce fidelity for sites that depend on external assets;
review capture evidence before trusting a new site. The add-site flow monitors the
submitted page, not an unlimited crawl. Add separate pages as separate targets.

The Strands graph captures evidence, classifies relevance, drafts, and independently
checks grounding. Exact citations/current-guidance quotes are validated in code before
persistence. This reduces unsupported recommendations; semantic truth is not guaranteed,
which is why uncertain claims and all guidance changes retain human review.
