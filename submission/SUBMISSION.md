# Civic Canary — submission packet

## Project name and tagline

**Civic Canary** — Catch public-service changes before outdated guidance reaches the people who need it.

Suggested track: **Good Neighbor Agents**.

## Copy-ready project description

### The problem and who it is for

Nonprofit staff, food-bank coordinators, library teams, and community navigators repeatedly check public websites to keep their guidance current. A changed document requirement, a broken language link, or an inaccessible form can leave someone with outdated instructions. Comparing pages manually is repetitive work; deciding how to update community guidance deserves human attention.

### What Civic Canary does

Civic Canary monitors selected public pages against a trusted baseline and turns relevant changes into evidence-backed review packets. A coordinator provides a website, a monitoring objective, and optional existing guidance. The setup workflow discovers candidate pages and lets the coordinator confirm the monitoring scope.

The agent collects page content and accessibility evidence, analyzes changes against the objective, drafts proposed guidance, and verifies that its quotations are grounded in captured sources. Findings explain what changed, why it matters, who may be affected, and what a reviewer could change. Reviewers inspect before-and-after evidence and approve or reject a proposed correction. Approval creates a separate guidance artifact; it never edits the source website.

The repository also includes repeatable synthetic scenarios. The River County benefits example introduces a benefit-award-letter requirement, a broken Spanish guidance link, and an unlabeled form field. These are controlled test data, not claims about an actual benefits program.

### How we built it

The production agent uses the Strands Agents SDK and a bounded GraphBuilder workflow: collect, classify, draft, and verify. Amazon Bedrock provides model reasoning, while Amazon Bedrock AgentCore Browser supports isolated browser capture. Pydantic contracts and quotation checks validate the result before a finding is persisted.

The EC2 deployment design serves a React interface and FastAPI backend through Nginx and CloudFront. DynamoDB holds sites, findings, and reviews; S3 holds jobs, scan history, baselines, screenshots, and approved artifacts. A background worker and systemd timer support recurring checks. The notification outbox and SES integration support surfacing new decisions. Scheduler and email activation must be verified in the deployed environment before claiming a live autonomous demonstration.

### Why it matters

The goal is to reduce repeated checking while keeping people responsible for policy and guidance decisions. Civic Canary is not an eligibility engine or a general-purpose website editor. It is a monitoring assistant that connects a source change to a specific reviewable correction. Benefits such as hours saved and fewer missed updates have not yet been measured in a user study.

### Safety and limitations

Monitoring is read-only and scope-limited. The agent does not log in, submit applications, collect applicant information, or automatically publish changes. Ambiguous changes require human judgment. Model confidence is not calibrated accuracy. Offline fixture tests demonstrate reproducibility, not live model accuracy or production uptime.

## Submission links

- Live application: https://d2g9z69nuvwc3l.cloudfront.net/
- Public source: https://github.com/yashsanap14/Civic-Canary
- License: [MIT](../LICENSE), already detected by GitHub.
- Architecture image: [PNG for upload](architecture.png), [editable SVG](architecture.svg)
- Setup: [README](../README.md), [EC2 upgrade](../DEPLOYMENT_UPGRADE.md), [existing AWS storage](../EXISTING_AWS_STORAGE.md)
- Video: **PENDING — public YouTube or Vimeo URL after recording and upload**.
- AWS Builder ID: **PENDING — owner must provide the requested Builder identity/profile, not an AWS account number or access key**.

## Judge instructions

1. Open the HTTPS application. AWS-mode reads and actions require a reviewer token; an empty dashboard before authentication does not prove there are no monitored sites.
2. Obtain access from the team through the organizer-approved private channel. Never put the deployment token in this repository, video, or public description. If private access cannot be arranged, use the local fixture workflow below.
3. Enter the token and select **Connect / refresh**. The verified live target is named `usaa` and monitors USA.gov food assistance. Inspect its existing successful no-change run and Strands trace; see [observed live evidence](LIVE_EVIDENCE.md).
4. No actionable findings were present in that target during verification. To test approval, first arrange a separate synthetic target with the team, or use local fixture mode. Only approve a disposable synthetic finding prepared for judging; approval creates a separate draft.
5. For credential-free testing, follow README's local quick start with `REVIEW_TOKEN=review-demo`, then use the V1/V2 fixture workflow. This local mode does not demonstrate live AWS inference.

## Completion checklist

- [x] Source repository is public; README and MIT license are present.
- [x] Project description and standalone architecture diagram prepared.
- [x] Narration source and recording shot list prepared.
- [ ] Authenticate live demo and verify a successful production run, finding, and review artifact.
- [ ] Verify timer/no-change behavior and SES delivery before showing those as live features.
- [x] Record the working app; align narration to the actual footage; export a video under five minutes. Export: `build/submission/final/Civic-Canary-Hackathon-Demo.mp4` (3:10, 1080p). Shows a no-change manual run, not approval or scheduled delivery.
- [ ] Watch the entire exported video and check for credentials or unrelated personal content.
- [ ] Upload to YouTube or Vimeo as **Public** and test the link while signed out.
- [ ] Enter the owner's AWS Builder ID in the submission form.
- [x] Non-video submission packet included in the repository; video/audio assets remain local.

Rules checked September 13, 2026: [official challenge](https://agentsforhumans.devpost.com/). Final eligibility and form completion remain the team's responsibility.
