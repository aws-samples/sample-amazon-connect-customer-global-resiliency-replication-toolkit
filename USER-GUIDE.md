# Amazon Connect ACGR Resource Replicator — User Guide

This guide covers detailed feature walkthroughs and troubleshooting. For setup, deployment, architecture, and API reference, see [README.md](README.md).

---

## Replication Wizard (5 Steps)

### Step 1: Enter Connect Instance ARN

You have two options to specify your Connect instance:

**Browse Instances (recommended):** Select the "Browse Instances" tab, choose an ACGR-supported region from the dropdown, and the tool will list all Connect instances in that region. Select an instance and click "Validate & Select" to proceed.

**Manual ARN Entry:** Select the "Manual ARN Entry" tab and paste your Amazon Connect instance ARN (e.g., `arn:aws:connect:us-east-1:123456789012:instance/abc-def-123`).

The tool validates that the instance exists and is ACTIVE, detects ACGR replica status, and resolves the source/target region pair automatically.

### Step 2: Confirm Regions

Review the detected source and target regions. The tool shows whether the instance already has a replica in the target region.

### Step 3: Discover Resources

Click "Discover" to scan all associated resources. Discovery typically takes 10–30 seconds and finds:
- Lambda functions (via `connect:ListLambdaFunctions`)
- Lex bots V1 and V2 (via `connect:ListBots`)
- IAM roles (from AWS Lambda execution roles)
- Kinesis/Firehose/KVS streams (from Connect instance storage config)

### Step 4: Select Resources

Review the discovered inventory organized by category. For each resource you can see the name, ARN, type, status, and dependencies. Select which resources to replicate. Optionally enter a name prefix (e.g., `dr-`) to prepend to all replicated resource names.

**Sync vs Async Mode:** For ≤ 5 resources, replication runs synchronously in a single AWS Lambda execution. For > 5 resources, the tool automatically uses Step Functions for async replication with parallel execution per dependency level. You can opt into async mode for smaller jobs if you prefer.

### Step 5: Replicate

Click "Replicate" to start. Resources are replicated in dependency order:

1. IAM Roles (no dependencies)
2. Lambda Functions (depend on IAM roles)
3. Lex Bots (depend on Lambda for codehook ARN rewriting)
4. Kinesis / Firehose / KVS streams

Each resource shows live status: pending → in progress → completed / failed / blocked. If a dependency fails, dependent resources are marked as BLOCKED.

---

## Replication Behavior

### Lex Replication via ALGR

Amazon Lex bots are replicated using ALGR (Amazon Lex Global Resiliency) for real-time sync between source and target regions. Fulfillment Lambda ARNs are rewritten to the target region, and bot alias and locale configuration are preserved.

### Lambda Runtime Upgrades

Deprecated runtimes are automatically upgraded during replication:

| Deprecated Runtime | Upgraded To |
|-------------------|-------------|
| `python2.7` – `python3.8` | `python3.12` |
| `nodejs10.x` – `nodejs16.x` | `nodejs20.x` |
| `dotnetcore2.1` / `3.1` | `dotnet8` |
| `ruby2.5` / `2.7` | `ruby3.3` |
| `java8` | `java21` |

### ARN Rewriting

Lambda environment variables containing source-region ARNs are automatically rewritten to target-region equivalents. This covers Amazon DynamoDB table ARNs, Kinesis stream ARNs, and other regional resources.

### Idempotency

Safe to re-run. If a resource already exists in the target region, the tool detects the conflict, logs it, looks up the existing resource, and continues without failing.

---

## Sessions Management

Every replication run creates a session stored in DynamoDB. From the Sessions page you can:
- View recent sessions with their status (in progress, completed, partially failed)
- Click a session to see detailed per-resource status
- Retry individual failed resources or retry all failed resources at once

Sessions have a TTL and are automatically cleaned up after expiration.

---

## Quota Comparison

Compare service quotas between source and target regions to identify potential issues before replication:
- Lambda concurrent executions
- Amazon Lex bot limits
- Kinesis shard limits

Highlights quotas where the target region has lower limits than the source.

---

## Discover & Associate

For cases where resources already exist in the target region (e.g., manually created or from a previous replication):

1. Enter the target Connect instance ARN
2. Click "Discover Target" to scan existing resources in the target region
3. Review discovered resources
4. Click "Associate" to link them with the target Connect instance

Useful for associating AWS Lambda functions, linking Amazon Lex bots, or configuring storage (Kinesis, S3) on the DR instance.

---

## Cleanup

### Full Session Cleanup

From the session detail page, click "Cleanup" to delete all replicated resources in the target region for that session.

### Selective Cleanup

Choose specific resources to delete while keeping others. Useful when some replicated resources need to be recreated with different settings.

---

## Resource Diff

Compare a resource between source and target regions to see configuration differences. Available from the session detail page for each replicated resource.

---

## Contact Flow Analysis

The Contact Flows page lets you scan your Connect instance's contact flows to identify which Amazon Lex bots and AWS Lambda functions they reference. This is useful for planning replication — you can see exactly which resources are used by your flows before starting.

### How to Use

1. Navigate to the **Contact Flows** tab in the top navigation bar
2. Select a region and instance from the dropdown, or enter an ARN manually
3. Click **Analyze**
4. Watch the progress bar as flows are analyzed ("Analyzed 375 of 1200 flows")
5. When complete, review the results table

### How It Works

When you click Analyze, the tool:
1. Creates an analysis job and returns immediately
2. Kicks off a background Lambda invocation (900s timeout) that:
   - Lists all contact flows via `ListContactFlows`
   - Calls `DescribeContactFlow` concurrently (5 at a time) for each flow
   - Parses JSON definitions to extract Amazon Lex bot and Lambda ARNs
   - Updates progress in DynamoDB every 25 flows
3. The frontend polls every 3 seconds for progress updates
4. Results are stored in DynamoDB with a 1-hour TTL (auto-cleanup)

If the Lambda approaches its timeout (850s), analysis stops gracefully and returns partial results with a truncation warning.

### Results Table

Each row shows:
- **Flow Name** and **Flow Type** (e.g., CONTACT_FLOW, CUSTOMER_QUEUE)
- **Lex Bot References** — count of Amazon Lex bots referenced via `InvokeLexBot` / `InvokeLexV2Bot` actions
- **Lambda References** — count of AWS Lambda functions referenced via `InvokeLambdaFunction` actions

Expand a row to see the specific ARNs. Each ARN shows an inventory match indicator — green if the resource is already in your replication inventory, gray if not.

### Filtering

Use the filter controls to narrow results:
- **All** — show all contact flows
- **Lex Only** — show only flows that reference Amazon Lex bots
- **Lambda Only** — show only flows that reference AWS Lambda functions

A summary banner at the top shows total flows analyzed, flows with Lex references, and flows with Lambda references.

### Scalability

| Instance Size | Approximate Time |
|---------------|-----------------|
| 50 flows | ~5 seconds |
| 500 flows | ~30 seconds |
| 1200 flows | ~1–2 minutes |
| 2000+ flows | ~3–5 minutes |

---

## Enhanced Error Display

When a resource fails during replication, the Session Status page shows detailed error information.

### Error Badges

Each failed resource displays a colored badge indicating the error type:
- **Permission** — an IAM permission was denied (shows the specific action, e.g., `lambda:CreateFunction`)
- **Quota** — a service quota was exceeded (shows the quota name)
- **Not Found** — the source resource was not found
- **Conflict** — the resource already exists in the target region
- **Timeout** — the operation timed out
- **Service Error** — an unexpected AWS service error

### Guidance Text

Below each error badge, actionable guidance text explains what to do:
- Permission errors: "Add `lambda:CreateFunction` to the Lambda execution role"
- Quota errors: "Request a quota increase for Lambda concurrent executions in the target region"
- Timeout errors: "Retry the resource — use async mode (Step Functions) for large inventories"

### Retry

Click the **Retry** button next to any failed resource to retry it individually. Use **Retry All Failed** to retry all failed resources in the session at once.

---

## KMS Handling Details

Resources that involve encryption (Kinesis streams, S3 buckets, Firehose) show a KMS info panel on the Session Status page.

Click the expandable **KMS Details** section on any resource to see:
- **Key Type** — AWS-managed key, customer-managed key (CMK), or none
- **Key Alias / ARN** — the key identifier used
- **Action Taken** — what the tool did:
  - **Reused** — an existing key in the target region was reused
  - **Bootstrapped** — a new AWS-managed key was created in the target region
  - **Skipped** — no encryption key was needed or the CMK is not available cross-region
- **Message** — a human-readable explanation of the KMS decision

---

## Step Functions Progress

For large replication jobs (> 5 resources), the tool uses AWS Step Functions for async replication with parallel execution per dependency level.

### How It Works

1. Resources are grouped into dependency levels (e.g., IAM roles first, then Lambda, then Lex)
2. Each level runs in parallel (up to 10 resources at once)
3. Levels execute sequentially — level 2 starts only after level 1 completes
4. If a resource fails, all its transitive dependents are marked as **BLOCKED**

### Progress Polling

The Session Status page automatically polls for progress while a AWS Step Functions execution is running. You'll see:
- Current dependency level being executed
- Per-resource status: pending → in progress → completed / failed / blocked
- A final summary with succeeded, failed, and blocked counts

---

## Throttle Handling

The resource Lambda handler (`resource_lambda_handler.py`) uses exponential backoff with jitter when AWS API calls are throttled. This prevents cascading failures during parallel AWS Step Functions execution when multiple resources are being replicated simultaneously.

---

## Limitations

- Kinesis/KVS/Firehose: streams are created but no data flows until the DR Connect instance is configured to use them
- Single AWS account only (no cross-account support)
- Does not replicate Connect-native resources (contact flows, queues, routing profiles)
- Lambda code is copied as-is; hardcoded region-specific logic may need manual adjustment

---

## Troubleshooting

| Issue | Resolution |
|-------|------------|
| "Instance not found" | Verify the ARN is correct and the Lambda role has `connect:DescribeInstance` in that region |
| Discovery finds no resources | Ensure the Connect instance has AWS Lambda functions, Amazon Lex bots, or storage configs associated |
| Lambda replication fails with "Role not found" | IAM roles must be replicated first — check if the IAM role replication succeeded |
| Amazon Lex bot replication fails | Check that ALGR is supported for the region pair; verify the bot exists in the source region |
| Replication timeout (900s) | Use async mode (Step Functions) for large inventories — the wizard auto-selects async for >5 resources |
| Permission denied error | Check the error badge on the Session Status page — it shows the specific IAM action that was denied |
