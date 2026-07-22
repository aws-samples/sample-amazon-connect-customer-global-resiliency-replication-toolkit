# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

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

### Removed

- Dead `healthCheck()` client function and `HealthResponse` type in the
  frontend, left over after the `/api/health` endpoint was removed.
- Unused, incorrectly-paired `_REGION_REWRITE` map in `dry_run.py`.

### Changed

- Instance-picker and Contact Flow Analysis region dropdowns now list only the
  five ACGR-supported source regions, preventing dead-end selections.

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
