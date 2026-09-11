# Use the existing AWS resources

This configuration reuses these resources without CDK deployment or table recreation:

| Resource | Partition key | Data |
| --- | --- | --- |
| CivicCanarySites | siteId (String) | Portal targets |
| CivicCanaryFindings | findingId (String) | Findings and current decision status |
| CivicCanaryReviews | reviewId (String) | Approved/rejected review decisions |
| civic-canary (S3) | Object key | Runs, baselines, snapshots, playbook, approved drafts |

Set these environment variables on the API/worker process before starting it:

```bash
export CIVIC_CANARY_MODE=aws
export AWS_STORAGE_LAYOUT=existing
export AWS_REGION=us-east-1
export SITES_TABLE=CivicCanarySites
export FINDINGS_TABLE=CivicCanaryFindings
export REVIEWS_TABLE=CivicCanaryReviews
export EVIDENCE_BUCKET=civic-canary
```

Configure REVIEW_TOKEN securely in the process environment, or use REVIEW_TOKEN_SECRET_ARN.
Use the EC2 instance IAM role for credentials. No TARGETS_TABLE, RUNS_TABLE, or storage
SSM parameter is needed in this layout.
The application does not automatically load .env; export the values or configure them in
your service manager/container environment. Apply them to every API and worker process.

Initialize an empty deployment once:

```bash
uv run python scripts/seed_aws.py --existing --region us-east-1
```

The seed uses the checked-in synthetic portal and refuses to overwrite an existing target.
Start/restart the API with the environment above. Scan V1, switch to V2, scan, then approve
or reject a finding. Data now persists across API restarts.

This enables AWS persistence for the existing deterministic demo. It does not enable Bedrock
or AgentCore by itself. Leave runtime ARN settings unset for that demo. For managed browser
scans, configure an HTTPS portal target, the runtime, worker, and browser separately, and
give the runtime these same storage settings.

The API and Pydantic models retain snake_case fields. DynamoDB items use siteId/findingId as
the authoritative partition key, with the other model fields unchanged. An existing manually
created item must contain the full model fields, not just the partition key; arbitrary
camelCase documents are not automatically migrated.

Run records are JSON under runs/<URL-encoded-run-id>.json. Conditional S3 writes reserve run
IDs atomically; no fourth table is needed. Keep the runs/ prefix out of lifecycle deletion
rules if you need permanent history. Review IDs are review-<finding_id>. A DynamoDB transaction
commits the final finding state and its review record together. Pending approvals can be retried.
Approved draft objects are under approved/<finding_id>.md.

The API/worker instance role needs:

- DynamoDB GetItem, PutItem, Scan on the three exact table ARNs. The transaction's Put
  operations are authorized by PutItem on both the Findings and Reviews tables.
- S3 ListBucket on arn:aws:s3:::civic-canary.
- S3 GetObject and PutObject on arn:aws:s3:::civic-canary/*.
- Secrets Manager GetSecretValue only if using a review-token secret.
- KMS permissions if the existing resources use customer-managed encryption keys.

These are runtime permissions; no CreateTable/DeleteTable or bucket deletion is needed.
Use [infra/existing-storage-policy.json](infra/existing-storage-policy.json) as the role policy
template, replacing YOUR_ACCOUNT_ID with the table owner's account ID. This template is for
storage access only; retain any separately required model/browser/worker permissions.
Do not run the CDK deploy/destroy instructions for these manually managed resources.
Existing tables and bucket contents are never deleted by the adapter.

Verification: inspect siteId=benefits-demo in Sites, findingId keys in Findings, the
reviewId record after a decision in Reviews, and runs/, baselines/, snapshots/, playbooks/,
approved/ in S3. Confirm a restart preserves runs/findings and a repeated scan is deduplicated.
