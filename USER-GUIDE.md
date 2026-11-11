# Amazon Connect ACGR Resource Replicator — User Guide

This guide covers detailed feature walkthroughs and troubleshooting. For setup, deployment, architecture, and API reference, see [README.md](README.md).

---

## Signing in and out

Ask an administrator to run `scripts/create-user.sh` for you. You receive a temporary password by email. Open the toolkit URL, sign in on the Cognito page, and choose a new password (14+ characters) when asked. The top bar shows who you are signed in as; **Sign out** ends the session. If you see the sign-in page again, your session expired; sign in again. Your work is stored in the backend, so nothing is lost.

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
- Lex bots V1 and V2 (via `connect:ListBots`). Only Lex V2 bots can be replicated (ALGR is V2 only); a Lex V1 bot is shown as skipped
- IAM roles (from AWS Lambda execution roles)
- Kinesis/Firehose/KVS streams (from Connect instance storage config)

### Step 4: Select Resources

Review the discovered inventory organized by category. For each resource you can see the name, ARN, type, status, and dependencies. Select which resources to replicate. Optionally enter a name prefix (e.g., `dr-`) to prepend to all replicated resource names.

**Sync vs Async Mode:** For ≤ 5 resources, replication runs synchronously in a single AWS Lambda execution. For > 5 resources, the tool automatically uses Step Functions for async replication with parallel execution per dependency level. You can opt into async mode for smaller jobs if you prefer.

### Step 5: Replicate

Click "Replicate" to start. Resources are replicated in dependency order:

1. IAM Roles (no dependencies)
2. Lambda Functions (depend on IAM roles)
3. Lex Bots (depend on Lambda, because the bot's code hook calls the Lambda function with the same name in the target region)
4. Kinesis / Firehose / KVS streams

Each resource shows live status: pending → in progress → completed / failed / blocked. If a dependency fails, dependent resources are marked as BLOCKED.

---

## Replication Behavior

### Lex Replication via ALGR

Amazon Lex V2 bots are replicated with ALGR (Amazon Lex Global Resiliency). The tool switches ALGR on for each bot (`CreateBotReplica`). From then on Lex itself keeps the replica in sync: the bot, its versions and its aliases keep the same IDs in the target region, and changes flow one way, from the source bot to the replica. The replica cannot be edited, so change the bot in the source region only.

The tool does not create or edit anything on either bot. The bot's code hook finds the Lambda function with the same name in the target region, which the Lambda replication step creates, so nothing needs rewriting.

**Associating the bot with the DR instance.** The tool associates the same alias or aliases that the source instance uses, with the region changed, once ALGR reports each alias replica as `Available`. While an alias is still replicating, the resource shows `pending`; retry the association after a few minutes. If the source instance has no alias of the bot associated, there is nothing to associate and the tool says so.

**Not replicated by ALGR** (per the Amazon Lex documentation): bots that use LLM-based slots such as CFAQ and utterance generation, and a network of bots as a whole.

### Associating with the DR instance

After replication, the **Associate** step links the replicated resources to the DR Connect instance. Safe to re-run: it skips what is already linked.

| What | What the tool does |
|------|--------------------|
| Instance attributes | Always turns on `CONTACTFLOW_LOGS`. The tool never writes `CONTACT_LENS`, `EARLY_MEDIA` or `MESSAGE_STREAMING`, because ACGR replicates them once your account is onboarded to instance-attribute replication through AWS Support. It only reads both instances and shows a note when they differ, so you can match them or ask AWS Support to onboard you. |
| Lambda functions | Adds the Connect invoke permission to the function and associates it with the DR instance. The permission is added even if the function was already associated. |
| Lex bots | Associates the alias or aliases the source instance uses, once ALGR reports them `Available` (see above). |
| Kinesis, Firehose, video streams, S3 | Sets up the storage configuration on the DR instance. The storage type comes from the source instance's configuration. |
| Amazon Q in Connect | Replicates the assistant and knowledge bases, and links them. |

Approved origins are added during replication, for the origins you selected, not in the Associate step.

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

For an ACGR pair, AWS matches the quotas of the Connect resources it mirrors (users, queues, routing profiles, flows, flow modules, agent status, quick connects, hours of operation, prompts, security profiles, user hierarchy groups) automatically before mirroring them. Those rows carry an **Auto-matched by ACGR** badge; check them again after replication finishes before asking for an increase. Quotas ACGR does not match (for example phone numbers, concurrent calls, and the Lambda, Lex and Kinesis limits) still need a manual request.

---

## Discover & Associate

For cases where resources already exist in the target region (e.g., manually created or from a previous replication):

1. Enter the target Connect instance ARN
2. Click "Discover Target" to scan existing resources in the target region
3. Review discovered resources
4. Click "Associate" to link them with the target Connect instance

Useful for associating AWS Lambda functions, linking Amazon Lex bots, or configuring storage (Kinesis, S3) on the DR instance.

- **Lex bots:** the list shows the alias or aliases the source instance uses, and only once they exist in the target Region. An alias is associated only when ALGR reports it `Available`. If it is still replicating, the result is **pending** (counted separately in the summary, not as an error); select it again and click Associate in a few minutes. A `Failed` replica is reported as an error.
- **Lambda functions:** use the same logic as the Associate step above, including the Connect invoke permission.

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

If a flow cannot be read (for example AWS keeps throttling the request after several retries), a warning "Some flows could not be read" lists them, so the totals are never silently incomplete. Throttled reads are retried with backoff first.

### Results Table

Each row shows:
- **Flow Name** and **Flow Type** (e.g., CONTACT_FLOW, CUSTOMER_QUEUE)
- **Lex Bot References** — count of Amazon Lex bots referenced by `ConnectParticipantWithLexBot` actions (the "Get customer input" block with a Lex bot): a Lex V2 alias ARN, or a Lex V1 bot name, Region and alias
- **Lambda References** — count of AWS Lambda functions referenced via `InvokeLambdaFunction` actions

Expand a row to see the specific ARNs. Each ARN shows an inventory match indicator — green if the resource is already in your replication inventory, gray if not. A Lex alias counts as in the inventory when its bot is in the inventory.

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
- Does not replicate Connect-native resources (contact flows, queues, routing profiles, users): ACGR does that itself
- Lex V1 (classic) bots are discovered but not replicated, because ALGR supports Lex V2 only. Migrate the bot to Lex V2, or maintain it in the target Region yourself
- Lex bots using LLM-based slots, and a network of bots as a whole, are not replicated by ALGR
- The tool does not create or edit anything on a Lex bot; the replica is read-only and kept in sync by ALGR. If the source instance has no alias of a bot associated, there is no alias to associate and the tool reports it
- Lambda code is copied as-is; hardcoded region-specific logic may need manual adjustment

---

## Troubleshooting

| Issue | Resolution |
|-------|------------|
| "Instance not found" | Verify the ARN is correct and the Lambda role has `connect:DescribeInstance` in that region |
| Discovery finds no resources | Ensure the Connect instance has AWS Lambda functions, Amazon Lex bots, or storage configs associated |
| Lambda replication fails with "Role not found" | IAM roles must be replicated first — check if the IAM role replication succeeded |
| Amazon Lex bot replication fails | Check that ALGR is supported for the region pair; verify the bot exists in the source region |
| Lex bot shows **skipped** | It is a Lex V1 (classic) bot. ALGR supports Lex V2 only; migrate it to V2 or create it in the target Region yourself |
| Lex bot association shows **pending** | ALGR is still copying the alias (this can take a while). Retry the association in a few minutes |
| Lex association says no alias is associated with the source instance | The tool associates the aliases the source Connect instance uses. Associate the alias your flows call with the source instance first; ALGR then replicates it |
| Lex alias reported **Failed** | ALGR could not replicate that alias. Check the bot's replication status in the Amazon Lex console (Global Resiliency tab) and `lex:ListBotAliasReplicas` |
| Replication timeout (900s) | Use async mode (Step Functions) for large inventories — the wizard auto-selects async for >5 resources |
| Permission denied error | Check the error badge on the Session Status page — it shows the specific IAM action that was denied |
