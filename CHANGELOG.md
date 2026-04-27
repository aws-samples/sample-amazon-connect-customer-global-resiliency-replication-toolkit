# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
