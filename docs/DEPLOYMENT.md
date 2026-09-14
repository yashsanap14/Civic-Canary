# Civic Canary deployment notes

This document indexes the detailed deployment material moved out of the main README so
hackathon judges and operators can find implementation depth without cluttering the overview.

## What is deployed today

The active path is:

**CloudFront → Nginx / React / FastAPI on Amazon EC2 → Strands Agents SDK → Amazon Bedrock +
Bedrock AgentCore Browser → DynamoDB + S3**, with human approve/reject in the dashboard.

The EC2 instance uses an IAM instance role for least-privilege access to Bedrock, AgentCore
Browser, DynamoDB, and S3. A systemd timer runs the background worker for due-site scans.

Architecture diagram: [civic-canary-architecture.png](civic-canary-architecture.png)

## Operator guides

| Guide | Use when |
| --- | --- |
| [../DEPLOYMENT_UPGRADE.md](../DEPLOYMENT_UPGRADE.md) | Activating the EC2 worker, env vars, role permissions, timer, history backfill |
| [../EXISTING_AWS_STORAGE.md](../EXISTING_AWS_STORAGE.md) | Reusing CivicCanarySites / Findings / Reviews tables and the civic-canary S3 bucket |
| [../AWS_DEPLOYMENT.md](../AWS_DEPLOYMENT.md) | Optional CDK path (Lambda, API Gateway, EventBridge, AgentCore Runtime) |
| [../DEMO_CHECKLIST.md](../DEMO_CHECKLIST.md) | Pre-demo and submission checks |

## Optional vs deployed

The repository still contains an alternative CDK / Lambda / API Gateway / AgentCore Runtime
path. That path is **not** the architecture shown in the README diagram. Do not run both
scheduling paths without a shared locking design.

SES notification delivery is implemented in code for the EC2 upgrade path and may require
account/sandbox configuration before production email works.

## Production scan behavior (summary)

- Production AWS scans use the Strands graph with AgentCore Browser capture and Bedrock analysis.
- Findings require grounded evidence quotations before persistence.
- Model or browser failure marks the run failed; there is no silent production bypass.
- Local mode can still use fixture/demo browsers for offline development.

## Security reminders

- Never commit `.env`, reviewer tokens, token digests, AWS credentials, CDK outputs, or local
  AgentCore state (see `.gitignore`).
- Prefer IAM roles over long-lived access keys on EC2.
