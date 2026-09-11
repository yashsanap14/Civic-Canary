# Civic Canary

Civic Canary is a reviewer-controlled AI agent that monitors public-benefit portals for meaningful
content and accessibility regressions. It captures a safe, read-only snapshot, compares it with an
approved baseline, identifies nonprofit guidance affected by the change, and drafts a correction
for a human reviewer.

The repository contains a working local MVP and deployment-ready AWS infrastructure. No AWS
environment is currently deployed from this repository.

## Demo scenario

The included River County benefits portal has two deterministic versions:

- **V1** is the trusted baseline.
- **V2** adds a required benefit-award letter, breaks the Spanish guidance link, and removes a form
  label.
- Unrelated styling changes are intentionally ignored.

A reviewer can switch the demo version, start a scan, inspect evidence and graph timings, and
approve or reject a proposed guidance patch. Approval creates a separate Markdown artifact; Civic
Canary never edits the source portal.

## Features

- Bounded Strands workflow with typed Pydantic contracts
- Deterministic semantic and structural comparison before model review
- Accessibility checks for broken links, missing labels, language metadata, and axe-core findings
- Evidence-backed mapping from portal changes to nonprofit playbook sections
- Reviewer-token protection for scans, demo controls, and decisions
- Run idempotency and finding deduplication
- Local fixture browser and managed AgentCore Browser adapters
- S3 evidence, DynamoDB records, CloudWatch tracing, and scheduled scans on AWS
- Read-only URL allow-listing with cross-host navigation and write-request blocking

## Safety boundaries

Civic Canary does not:

- log in to portals or bypass CAPTCHA challenges;
- enter or collect personal information;
- upload files, submit applications, or make eligibility decisions;
- navigate outside a target's exact host allow-list;
- publish corrections without human approval.

## Architecture

```mermaid
flowchart LR
    UI[React reviewer UI] --> API[FastAPI control API]
    API -->|queue scan| WORKER[Scan Lambda]
    SCHEDULE[EventBridge Scheduler] --> WORKER
    WORKER --> RUNTIME[AgentCore Runtime]
    RUNTIME --> BROWSER[AgentCore Browser]
    RUNTIME --> GRAPH[Strands graph]
    GRAPH --> DIFF[Semantic diff]
    GRAPH --> A11Y[Accessibility audit]
    DIFF --> IMPACT[Playbook impact mapping]
    A11Y --> IMPACT
    IMPACT --> DRAFT[Safe repair draft]
    RUNTIME --> DB[(DynamoDB)]
    RUNTIME --> EVIDENCE[(S3 evidence)]
    API --> DB
    API --> EVIDENCE
```

Critical facts are extracted and compared deterministically. The model validates meaning and drafts
the correction, but malformed model output is never silently accepted. A failed scan is recorded as
a failed run and does not create an actionable finding.

## Repository layout

| Path | Purpose |
| --- | --- |
| `agent/` | Strands graph, comparison engine, browser adapters, schemas, and fixtures |
| `services/` | FastAPI control plane, scan worker, persistence, security, and observability |
| `web/` | React reviewer dashboard and deterministic portal fixtures |
| `infra/` | AWS CDK stack and AgentCore runtime policy |
| `agentcore/` | AgentCore project configuration |
| `scripts/` | Build, seed, deploy, and runtime-connection helpers |
| `tests/` | API, workflow, safety, and resilience tests |
| `AWS_DEPLOYMENT.md` | Complete deployment-owner handoff |

## Local quick start

Requirements:

- Python 3.13 or 3.14
- [`uv`](https://docs.astral.sh/uv/)
- Node.js 20, 22, or 24 and npm

Install dependencies:

```bash
uv sync --extra dev --extra infra
npm --prefix web install
cp .env.example .env
```

Start the API:

```bash
export REVIEW_TOKEN=review-demo
uv run uvicorn services.control_api.app:app --reload --port 8000
```

Start the reviewer UI in another terminal:

```bash
npm --prefix web run dev
```

Open `http://localhost:5173`, enter `review-demo`, switch the portal to V2, and choose **Run
scan**. The API is proxied from Vite to `http://localhost:8000`.

For a command-line demonstration:

```bash
uv run civic-canary --version v1
uv run civic-canary --version v2
```

V1 should produce no findings. V2 should detect the document requirement, broken Spanish link,
and unlabeled field.

## Run with Amazon Bedrock AgentCore Browser

To run Civic Canary using the managed Amazon Bedrock AgentCore Browser rather than the local fixture browser, create or update your local `.env` file with:

```bash
AWS_REGION=us-east-1
BROWSER_MODE=agentcore
AGENTCORE_BROWSER_ID=<their-browser-tool-id>
BEDROCK_MODEL_ID=<their-bedrock-model-or-inference-profile-id>
```

AWS credentials should come from the normal AWS credential chain / IAM role (such as `aws sso login` or environment credentials) and must not be committed to GitHub.

Run the scan locally in AgentCore mode:

```bash
uv run civic-canary --version v2 --browser-mode agentcore
```

When `BROWSER_MODE=agentcore` is set in `.env`, `uv run civic-canary --version v2` automatically connects to the configured AgentCore Browser tool.


## Tests and quality checks

```bash
uv run --extra dev pytest
uv run --extra dev ruff check agent services tests scripts infra
npm --prefix web test
npm --prefix web run lint
npm --prefix web run build
```

The tests cover baseline matching, all three demo regressions, allow-list enforcement, write-request
blocking, protected actions, approval rollback, asynchronous dispatch, idempotency, and explicit
failure handling.

## API

Public, sanitized reads:

- `GET /api/health`
- `GET /api/targets`
- `GET /api/runs`
- `GET /api/runs/{run_id}`
- `GET /api/findings`
- `GET /api/findings/{finding_id}`

Requests requiring `X-Review-Token`:

- `POST /api/runs`
- `POST /api/demo/version`
- `POST /api/findings/{finding_id}/decision`

## AWS deployment

For the existing CivicCanarySites, CivicCanaryFindings, CivicCanaryReviews tables and
civic-canary bucket, follow [Existing AWS storage](EXISTING_AWS_STORAGE.md).
That path needs no new tables or CDK deployment.

The production design targets `us-east-1` and uses Bedrock, AgentCore Runtime and Browser, Lambda,
API Gateway, DynamoDB, S3, Secrets Manager, Systems Manager Parameter Store, EventBridge Scheduler,
CloudWatch, IAM, and AWS Budgets.

The deployment owner should follow [AWS_DEPLOYMENT.md](AWS_DEPLOYMENT.md). It includes the required
permissions, model and quota checks, reviewer-token setup, estimated costs, deployment commands,
acceptance tests, troubleshooting, and complete cleanup instructions.

Never commit `.env`, reviewer tokens, token digests, AWS credentials, generated deployment targets,
CDK output, packaged ZIP files, or local AgentCore state. These are excluded by `.gitignore`.

## Contributing

1. Create a branch from `main`.
2. Keep portal access read-only and preserve the safety boundaries above.
3. Add tests for every behavior change.
4. Run all Python and web checks before opening a pull request.

## License

This project is licensed under the [MIT License](LICENSE).

## Existing AWS storage and EC2 redeployment

The AWS store uses existing tables; it does not create tables or buckets. Configure:

```dotenv
CIVIC_CANARY_MODE=aws
AWS_REGION=us-east-1
CIVIC_CANARY_SITES_TABLE=CivicCanarySites
CIVIC_CANARY_FINDINGS_TABLE=CivicCanaryFindings
CIVIC_CANARY_REVIEWS_TABLE=CivicCanaryReviews
CIVIC_CANARY_S3_BUCKET=civic-canary
```

Site and finding partition keys are `siteId` and `findingId`. Domain models and API
responses retain `target_id` and `finding_id`. Reviews use `reviewId`, `findingId`,
`action` (APPROVED/REJECTED), `reviewedAt`, `reviewer`, and `notes`. The API records a
SHA-256 token identity rather than the raw reviewer token.

Runs persist at `s3://civic-canary/runs/{run_id}.json`; Recent Runs lists these objects
across all pages and sorts newest first. Conditional S3 writes prevent duplicate run
creation. Existing `baselines/`, `snapshots/`, `evidence/`, and `approved/` storage
behavior is preserved. Local mode still uses InMemoryStore.

Remove legacy targets/runs table variables. If using `/civic-canary/storage-config`
in SSM, update its keys to `sites_table`, `findings_table`, `reviews_table`, and
`evidence_bucket`; old targets/runs keys are ignored. Explicit environment values take
precedence. The EC2 instance role needs DynamoDB GetItem, PutItem, Scan on the three
tables, S3 GetObject/PutObject on `civic-canary/*`, and S3 ListBucket on `civic-canary`.
See `infra/agentcore-runtime-policy.json` for the storage permissions.

Migration: existing records with the required camelCase keys need no key migration;
they must still contain the domain model's other required fields. Redundant legacy
ID attributes are ignored in favor of the partition key and removed on the next write.
Data in older tables or buckets is not copied automatically. Export old run models to
`runs/{run_id}.json` in the existing bucket if that history must remain visible. Copy
old baseline/evidence objects preserving their paths if moving from another bucket.
No AWS resources or stored data were modified by this code change.

The CDK stack now imports the existing resources. **If upgrading a previously deployed
CDK stack, first retain and back up its old storage resources before removing them
from CloudFormation**: the old definitions used DESTROY and S3 auto-delete. Review
`cdk diff` before deployment. EC2 code redeployment does not require CDK deployment.

On EC2, after committing and pushing this change, use the following commands. Replace
`/path/to/Civic-Canary` and `civic-canary.service` with your checkout and existing
systemd unit (the repository does not define an EC2 unit):

```bash
cd /path/to/Civic-Canary
git pull --ff-only
# Edit .env with the values above; retain your reviewer/browser/model settings.
nano .env
uv sync --extra dev
npm --prefix web ci
npm --prefix web run build
uv run --extra dev pytest -q
# The systemd unit must load this checkout's .env via EnvironmentFile.
sudo systemctl restart civic-canary.service
sudo systemctl status civic-canary.service --no-pager
curl --fail http://127.0.0.1:8000/api/health
```

For a foreground deployment without systemd, start the API with the environment loaded:

```bash
set -a
source .env
set +a
uv run uvicorn services.control_api.app:app --host 0.0.0.0 --port 8000
```
