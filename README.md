# Amazon Connect - ACGR Replication Toolkit

Serverless web application that discovers and replicates AWS resources associated with an Amazon Connect instance to its ACGR (Global Resiliency) paired disaster recovery region.

> **Important:** This toolkit is intended as a starting point for evaluation and learning.
> Before deploying in any environment, review the [Security considerations](#security) section
> and ensure the deployment meets your organisation's requirements. See [SECURITY.md](SECURITY.md)
> for known gaps and hardening guidance.

## Getting started

Clone the repo and follow the [Prerequisites](#prerequisites) and [Deployment](#deployment) sections below.

```bash
git clone https://github.com/aws-samples/amazon-connect-customer-acgr-replication-toolkit.git
cd amazon-connect-customer-acgr-replication-toolkit
```

## Architecture

```
CloudFront
├── S3 (React + Cloudscape frontend)
└── API Gateway /api/* → Lambda (FastAPI + Mangum, 512 MB / 900s)
    ├── DynamoDB: ReplicatorSessions (session state + TTL)
    ├── DynamoDB: FlowAnalysisJobs (ephemeral analysis jobs + 1hr TTL)
    ├── Step Functions: ReplicationStateMachine (async orchestration)
    ├── Source Region AWS APIs (discovery)
    └── Target Region AWS APIs (replication)
```

## Features

- Guided 5-step wizard for resource discovery and replication
- Instance picker: browse Connect instances by region or enter ARN manually
- Supported resource types: IAM Roles, Lambda Functions, Lex Bots (V1 + V2 via ALGR), Amazon Kinesis Data Streams, Amazon Data Firehose, Amazon Kinesis Video Streams, S3 Buckets
- Dependency-ordered replication (IAM → Lambda → Lex → everything else)
- Step Functions async replication for large inventories (>5 resources), with parallel execution per dependency level
- Contact Flow Analysis: async scan of contact flows for Amazon Lex bot and Lambda function references, with inventory cross-referencing, progress tracking, and support for 1000+ flows
- Error classification with actionable guidance (permission, quota, not-found, conflict, timeout)
- KMS handling visibility: see what KMS key action was taken per resource (reused, bootstrapped, skipped)
- Session persistence with live progress tracking
- Retry failed resources individually or in bulk
- Quota comparison between source and target regions
- Selective cleanup of replicated resources
- Resource diff between source and target
- Idempotent: safe to re-run (handles "already exists" gracefully)

## Supported ACGR Region Pairs

| Source | Target |
|--------|--------|
| us-east-1 | us-west-2 |
| us-west-2 | us-east-1 |
| eu-west-2 | eu-central-1 |
| eu-central-1 | eu-west-2 |
| ap-northeast-1 | ap-northeast-3 |

## Prerequisites

Install these on your development machine before deploying:

| Tool | Minimum version | Install |
|------|-----------------|---------|
| AWS CLI | v2 | [docs.aws.amazon.com/cli](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |
| Node.js | 18+ | [nodejs.org](https://nodejs.org/) — Node 18 or Node 20 both work |
| npm | bundled with Node.js | comes with Node install |
| Python | 3.12+ | [python.org](https://www.python.org/downloads/) or Homebrew: `brew install python@3.12` |
| AWS CDK CLI | v2 | `npm install -g aws-cdk` |
| pip3 | bundled with Python | comes with Python install |

Additionally:

- **AWS credentials** configured with permission to deploy CloudFormation, create IAM roles, and manage the AWS resources listed below. Run `aws sts get-caller-identity` to confirm your credentials resolve.
- **CDK bootstrap** in the target account/region (only required the first time you deploy CDK apps in that account/region):
  ```bash
  cdk bootstrap aws://<ACCOUNT-ID>/<REGION>
  ```

## Deployment

From the repository root:

```bash
chmod +x deploy.sh
./deploy.sh              # Full deploy (build frontend, bundle backend, CDK deploy, upload assets)
./deploy.sh synth        # Synthesize CloudFormation only (no deploy)
./deploy.sh destroy      # Tear down the stack and clean bundled deps
```

`deploy.sh` handles the full pipeline:

1. **Preflight** — verifies AWS CLI, CDK, Node, and Python are installed and AWS credentials work
2. **Frontend build** — `npm ci` and `npm run build` in `frontend/`
3. **Backend bundling** — installs Python dependencies (FastAPI, Mangum, boto3, Pydantic with Linux x86_64 binaries) into `backend/` for the Lambda package
4. **CDK synth & deploy** — provisions or updates the CloudFormation stack
5. **Frontend upload** — syncs `dist/` to the frontend S3 bucket with correct cache-control headers (1-year immutable for hashed assets, no-cache for `index.html`)
6. **CloudFront invalidation** — `/*` invalidation so users see the new build immediately

After a successful deploy, the CDK outputs include:
- `CloudFrontUrl` — the URL to open in your browser
- `ApiGatewayEndpoint` — the underlying API Gateway URL (mainly for debugging; the UI uses CloudFront)
- `StateMachineArn` — the AWS Step Functions state machine for async replication

You can also restrict CORS to a custom origin (default is the Amazon CloudFront distribution domain):

```bash
cdk deploy --context allowedOrigin=https://your-custom-domain.example.com
```

## AWS Resources Created

| Resource | Logical name | Purpose |
|----------|--------------|---------|
| Lambda | `ReplicatorBackend` | FastAPI backend via Mangum (512 MB, 900s timeout, LOG_LEVEL=INFO) |
| Lambda | `ResourceReplicatorLambda` | Single-resource replication handler invoked by Step Functions |
| API Gateway | `ConnectAcgrReplicatorApi` | REST API (CORS scoped to CloudFront origin, 1000 rps / 2000 burst throttling) |
| S3 Bucket | `FrontendBucket` | Static frontend assets (BlockPublicAccess + OAI) |
| CloudFront | `ReplicatorDistribution` | CDN for the UI, with SPA URI rewrite and `/api/*` proxy to API Gateway |
| DynamoDB | `ReplicatorSessions` | Session persistence with TTL (RemovalPolicy: RETAIN) |
| DynamoDB | `FlowAnalysisJobs` | Ephemeral flow-analysis job state with 1-hour TTL (RemovalPolicy: RETAIN) |
| Step Functions | `ReplicationStateMachine` | Dependency-level parallel replication orchestration |
| IAM Role | `ReplicatorLambdaRole` | Broad cross-service role (Connect, Lambda, Lex, Kinesis, IAM, S3, KMS, Wisdom, etc.). Scope down for production. |

## Project Structure

```
amazon-connect-acgr-replication-starter-pack/
├── frontend/src/components/
│   ├── Wizard/ReplicatorWizard.tsx    # 5-step replication wizard
│   ├── InstancePicker.tsx             # Region → instance dropdown selector
│   ├── Session/SessionsListPage.tsx   # Recent sessions list
│   ├── Session/SessionStatusPage.tsx  # Live replication progress + SFN polling
│   ├── ContactFlows/ContactFlowAnalysis.tsx
│   ├── Quota/QuotaComparison.tsx
│   ├── Discover/DiscoverAssociate.tsx
│   └── Inventory/ResourceTable.tsx    # Paginated inventory (25/page)
├── backend/
│   ├── api/routes.py                  # FastAPI routes (25+ endpoints)
│   ├── discovery/                     # Per-resource-type discovery modules
│   ├── replication/                   # Per-resource-type replication modules
│   ├── association/                   # Connect ↔ replicated resource association
│   ├── cleanup/                       # Selective cleanup
│   ├── audit/                         # Target-region audit
│   ├── aws/                           # Cross-region boto3 clients + ARN utils
│   ├── lambda_handler.py              # Mangum entry point for Lambda
│   └── tests/                         # pytest suite (693 tests)
├── infra/
│   ├── app.py
│   ├── stack.py                       # CDK stack definition
│   └── tests/                         # CDK synth property tests
├── scripts/
│   └── check_docs.sh                  # Documentation invariant checker
├── deploy.sh
├── redeploy-frontend.sh
├── run-tests.sh
├── USER-GUIDE.md                      # Feature walkthrough and troubleshooting
├── SECURITY.md                        # Vulnerability disclosure + scope
├── CONTRIBUTING.md                    # How to file issues and PRs
├── CHANGELOG.md
└── LICENSE
```

## API Endpoints

| Method | Endpoint | Description |
|--------|---------|-------------|
| POST | `/api/validate-instance` | Validate a Connect instance ARN and resolve regions |
| GET | `/api/list-instances?region=X` | List Connect instances in an ACGR-supported region |
| POST | `/api/discover` | Run full resource discovery |
| GET | `/api/inventory/{session_id}` | Retrieve discovered inventory |
| POST | `/api/replicate` | Start sync replication for selected resources |
| POST | `/api/sessions/{id}/replicate-async` | Start async replication via Step Functions |
| GET | `/api/sessions/{id}/execution-status` | Poll AWS Step Functions execution progress |
| GET | `/api/replicate/{job_id}/status?session_id=X` | Poll sync replication progress |
| POST | `/api/replicate/{job_id}/retry/{resource_id}` | Retry a failed resource |
| POST | `/api/contact-flows/analyze` | Start async contact flow analysis (returns job_id) |
| GET | `/api/contact-flows/analyze/{job_id}/status` | Poll flow analysis progress and results |
| POST | `/api/inventory/{session_id}/resources` | Manually add a resource ARN to inventory |

## Running Tests

### Backend (Python)

```bash
cd backend
python3 -m pytest tests/
```

Expected: **693 passing**.

### Infrastructure (CDK property tests)

```bash
cd infra
python3 -m pytest tests/
```

Expected: **4 passing** (no wildcard CORS, DynamoDB RETAIN, LOG_LEVEL=INFO on all Lambdas, UsagePlan present).

### Frontend (Vitest + React Testing Library + fast-check)

```bash
cd frontend
npm install
npm run test
```

Expected: **30 passing** across 7 test files.

### Documentation invariants

```bash
bash scripts/check_docs.sh
```

Expected: exit 0, ending with `[check_docs] PASS`.

## Local Development

### Backend

```bash
cd backend
pip install fastapi uvicorn boto3 pydantic requests
uvicorn api.routes:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

The Vite dev server proxies `/api/*` to `http://localhost:8000`, so the frontend can talk to a locally running backend.

## Security

See [SECURITY.md](SECURITY.md) for:
- Vulnerability disclosure policy
- Supported versions
- Scope — hardened defaults (CORS scoping, throttling, RETAIN, LOG_LEVEL, error sanitization)
- Production-hardening follow-ups (authentication, IAM role scoping, cross-account support)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to file issues, submit pull requests, and run the test suites locally.

## License

Licensed under the MIT License. See [LICENSE](LICENSE) for the full text.
