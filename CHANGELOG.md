# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- Frontend build and test tooling updated to clear all open Dependabot alerts (`npm audit`:
  0 vulnerabilities): `vite` 6.4.1 → 6.4.6, `vitest` 2.1.9 → 5.1.0, and the indirect
  dependencies refreshed within their allowed ranges (`npm audit fix`, no `--force`). These
  packages are used only to build and test the UI; none of them is part of the deployed
  stack. The `AuthProvider` test mocks `oidc-client-ts` with `function` implementations,
  which Vitest 3 and later require for anything called with `new`.

### Documentation

- README Region pairs table shows whether Lex bots can be replicated per pair: ALGR supports
  only the US and Europe pairs, so for ap-northeast-1 → ap-northeast-3 Lex bots are skipped
  (Lex V2 is not offered in ap-northeast-3).
- README "What ACGR already does" table and the User Guide Associate, Lex and Quota sections
  now link the AWS page behind each statement (mirrored resources, ACGR requirements, Lex
  Global Resiliency, IdP integration for approved origins). Approved origins have their own
  row.
- New list of what neither ACGR nor the tool copies: prompts stored in S3, saved-report
  schedules, views in draft state, and the storage configurations `CONTACT_EVALUATIONS`,
  `EMAIL_MESSAGES`, `REAL_TIME_CONTACT_ANALYSIS_CHAT_SEGMENTS` and
  `REAL_TIME_CONTACT_ANALYSIS_VOICE_SEGMENTS`.

### Added

- Amazon Cognito sign-in for the UI and the API. A user pool (admin-created users, no self
  sign-up, 14+ character passwords, optional TOTP MFA, retained on destroy) and a
  Cognito authorizer on every API method except the CORS preflight; unauthenticated calls
  get `401`. The UI signs in with the authorization-code flow and PKCE (`oidc-client-ts`),
  keeps tokens in `sessionStorage`, and shows the signed-in user with a Sign out button. The
  audit log records the authenticated user. New outputs `UserPoolId`, `UserPoolClientId`,
  `CognitoDomain`; new `scripts/create-user.sh` and `scripts/write-frontend-config.sh`;
  `deploy.sh` and `redeploy-frontend.sh` write `config.json` and `deploy.sh` checks that an
  anonymous API call is rejected before uploading the UI.

### Fixed

- Contact flow analysis no longer drops flows silently when `DescribeContactFlow` is
  throttled: throttled reads are retried with backoff, and flows that still cannot be read
  are reported as skipped (`skipped_flows`, shown as a warning in the UI).

### Changed

- Lex bot handling no longer repeats what Amazon Lex Global Resiliency (ALGR) already does.
  ALGR replicates the bot, every version and every alias (same IDs) and the replica is
  read-only, so the tool no longer:
  - creates an alias on the replica bot, or, as a fallback, builds the draft, creates a
    version and an alias on the **source** bot;
  - picks "the first non-test alias" on the replica (this could select the wrong alias);
  - rewrites Lambda ARNs inside the replica bot's intents and rebuilds its locales.
  Association now uses the alias or aliases the **source** Connect instance has associated
  (same alias ID, target Region) and waits until `ListBotAliasReplicas` reports each as
  `Available`; until then the resource is `pending` and retryable. The Discover & Associate
  page lists only the aliases the source instance uses, once they exist in the target Region.
- The backend role gains `lex:ListBotAliasReplicas` and `iam:CreateServiceLinkedRole`
  (limited to `AWSServiceRoleForLexV2Replication` / `replication.lexv2.amazonaws.com`),
  which Lex needs the first time ALGR is used in an account.
- The backend role no longer has Lex write actions the tool never calls (create/update/
  delete of bots, locales, intents, slots, slot types, versions and aliases, BuildBotLocale,
  DeleteBotChannel, TagResource, ListBotVersions, ListSlots, DescribeBotLocale,
  DescribeBotVersion). It keeps only the Lex actions the backend calls.
- Discover & Associate: a Lex alias is associated only once ALGR reports it `Available`;
  while it is still replicating it is reported as pending (new `totalPending` count), not
  as an error. Lambda functions there use the same code as the main Associate step.
- Instance attributes: the tool no longer writes `CONTACT_LENS`, `EARLY_MEDIA` or
  `MESSAGE_STREAMING` to the DR instance. ACGR replicates them once the account is onboarded
  to attribute replication (AWS Support), so the tool only reads both instances and reports a
  difference. `CONTACTFLOW_LOGS` is still always enabled (ACGR does not replicate it).
- Lambda functions are associated with the DR instance only in the Associate step (they were
  also associated during replication, without the Connect invoke permission). The Associate
  step now adds that permission even when a function is already associated.
- Approved origins are added to the DR instance only by the replication step, for the
  origins you selected. The Associate step no longer copies every source origin again.
- Documentation: README gains a "What ACGR already does, and what this toolkit adds" section,
  the missing API endpoints and the current test counts; USER-GUIDE documents the Associate
  step, the Lex `pending` behaviour, Lex V1 and ALGR limits, the quota badge and the flow
  analyzer action name, and adds Lex troubleshooting rows.
- Quota comparison marks the Amazon Connect quotas that ACGR matches automatically for
  mirrored resources ("Auto-matched by ACGR").
- Lex V1 (classic) bots are reported as skipped with a clear reason: ALGR supports Lex V2
  only. Previously replication called ALGR with the bot name and could fall back to an
  unrelated Lex V2 bot with the same name in the target Region.

- IAM roles are excluded from the replication selection UI (they are a global,
  region-less service — the same role ARN is valid in the target region, so
  there is nothing to copy). An informational alert explains this on the Select
  Resources step; dependent resources keep referencing their existing role.
  (Backend replication of IAM roles remains available if explicitly needed.)
- Added an Amazon Lex Global Resiliency (ALGR) prerequisite banner to the
  wizard's region-confirmation step, calling out that ALGR must be
  enabled/allow-listed for the account for Lex bot replication to succeed.
  (There is no AWS API to query ALGR allow-list status, so this is
  informational and cannot be auto-detected.)

- Lambda replication no longer fails when a source function references a
  cross-account layer it cannot republish (e.g. the AWS-managed
  `aws-fis-extension` layer owned by an AWS service account). Such layers are
  now dropped from the replicated function with a warning instead of being
  region-rewritten and attached, which previously caused `CreateFunction` to
  fail with a `lambda:GetLayerVersion` AccessDenied and cascade-blocked any
  dependent resources (e.g. a Lex bot whose codehook is that function).
  Same-account layers are unaffected.

- Instance-listing region allow-list now derives from the authoritative
  `ACGR_REGION_PAIRS` map instead of a separate hard-coded list of eight
  regions. Previously the `/api/list-instances` dropdown advertised
  `ap-northeast-2`, `ap-southeast-1`, and `ap-southeast-2` — regions that
  Amazon Connect Global Resiliency does not support — so selecting an instance
  there failed at `validate-instance`/`discover` with a 400.
- Quota comparison target-region resolution used an incorrect ACGR pairing
  (`ap-southeast-1 ↔ ap-northeast-1`, `ca-central-1 ↔ ca-west-1`, etc.). It now
  uses the authoritative `ACGR_REGION_PAIRS` map.
- `ApprovedOriginResource` is now deserialized to its correct subclass from
  DynamoDB (added to `_RESOURCE_TYPE_MAP`); previously it round-tripped as a
  plain `ResourceBase` and silently dropped `origin_url` in the Lambda
  deployment.
- ARN format validation now accepts S3 bucket ARNs (`arn:aws:s3:::bucket`,
  which have empty region and account fields) while still rejecting
  non-numeric account IDs. Manually adding an S3 resource no longer 400s.
- App-level CORS middleware no longer pairs `allow_origins=["*"]` with
  `allow_credentials=True` (an invalid combination per the CORS spec).
  Credentials are disabled; the scoped, authoritative origin allow-list
  remains enforced at API Gateway.

- Instance-picker and Contact Flow Analysis region dropdowns now list only the
  five ACGR-supported source regions, preventing dead-end selections.

- The temporary S3 bucket used to stage Lambda code during replication is now named
  `connect-acgr-lambda-staging-<account-id>-<region>` (the prefix changed).
  A deployment that already created a bucket under the previous prefix simply creates a new
  one; the old bucket is not used or deleted.

### Fixed

- Contact flow analysis now finds Lex bots in the documented `ConnectParticipantWithLexBot`
  action (`LexV2Bot.AliasArn`, or `LexBot` name/Region/alias for V1), and marks a flow's Lex
  alias as "in inventory" when its bot is in the inventory.
- Lex bots are no longer reported `REPLICATED` before the ALGR replica actually
  exists in the target region. Previously `_try_algr_replication` returned
  success immediately after `CreateBotReplica` (and treated an `Enabling`
  replica as done), so the UI showed the bot as replicated while it was still
  provisioning — and a subsequent Associate step failed with "bot does not
  exist". Replication no longer blocks: while the replica is `Enabling` the
  resource is reported as in progress (retryable), and each session-status poll
  re-checks the live replica status and marks it replicated once it is `Enabled`.

- `CONTRIBUTING.md`, `deploy.sh` and `README.md` used paths that only worked from a differently
  named folder; they are now relative to the repository root.
- The `TestAssociateFirehose` unit test called the real Firehose API and failed without AWS
  credentials; it no longer does, so the whole backend suite passes offline.

### Removed

- `BOT_MANAGEMENT` and `ENABLE_BOT_ANALYTICS_AND_TRANSCRIPTS` instance-attribute calls; they
  are not valid `AttributeType` values in the Amazon Connect API.
- Unused code: `replication/storage_config_replication.py` and the orchestrator function that
  was its only caller (never called); `_associate_lex_bot_with_connect`,
  `_wait_for_bot_available` and `_normalize_locale_id` in `lex_replication.py`.

- Dead `healthCheck()` client function and `HealthResponse` type in the
  frontend, left over after the `/api/health` endpoint was removed.
- Unused, incorrectly-paired `_REGION_REWRITE` map in `dry_run.py`.

- The draw.io source that was embedded inside `docs/architecture.png` (the image itself is unchanged).

## [1.0.0] - 2026-04-27

### Added

- `SECURITY.md` with vulnerability disclosure policy, supported-versions statement, and scope.
- `CONTRIBUTING.md` with issue-filing and PR guidance plus test-run instructions.
- `CHANGELOG.md` in Keep a Changelog format.
- API Gateway throttling on the production stage (1000 rps / 2000 burst).
- Wizard Step 4 name-prefix validation against `^[a-zA-Z0-9-]{1,32}$`.
- Sessions page loading spinner and empty state with CTA to the Wizard.
- Auto-populate instance ARN on the Contact Flow Analysis page from the wizard session.
- Inventory table pagination (25 per page, client-side).
- Cloudscape `SideNavigation` as primary chrome (replacing top-nav buttons).
- Vitest + React Testing Library test harness with smoke and property tests.
- Backend invariant tests: no `/api/health` route; exception responses never leak raw content.
- Infra property tests: no wildcard CORS; DynamoDB `RETAIN`; all Lambdas have `LOG_LEVEL=INFO`.
- `scripts/check_docs.sh` documentation-invariant script.

### Changed

- API Gateway CORS `allow_origins` scoped to the CloudFront distribution domain (was `*`).
- DynamoDB tables now use `RemovalPolicy.RETAIN` (was `DESTROY`) so `cdk destroy` does not delete session data.
- Lambda functions now run with `LOG_LEVEL=INFO`.
- Backend API responses no longer include raw Python exception content, class names, or tracebacks. Full exceptions are still captured server-side via `logger.exception`.
- README includes a proof-of-concept callout, a dedicated Security section, and updated Security Notes reflecting the hardened defaults.

### Removed

- `GET /api/health` endpoint (unused by the frontend, redundant post-deployment check).

### Security

- Error responses sanitized to prevent exception content leakage.
- CORS scoped to the CloudFront origin.
- API Gateway throttling limits abuse of public endpoints.
- DynamoDB RETAIN prevents accidental data loss from `cdk destroy`.
