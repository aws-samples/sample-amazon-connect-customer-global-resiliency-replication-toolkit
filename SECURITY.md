# Security Policy

## Reporting a Vulnerability

If you discover a potential security issue in this project, we ask that you notify us directly and do not create a public GitHub issue.

Please report security vulnerabilities by emailing the repository maintainers or by using the GitHub Security Advisories feature ("Report a vulnerability" on the Security tab of this repository).

## Scope

This tool deploys AWS resources (Lambda, API Gateway, DynamoDB, S3, CloudFront, Step Functions) into your AWS account.

### Hardened defaults

- CORS is scoped to the CloudFront distribution domain (overridable via the `allowedOrigin` CDK context variable).
- API Gateway throttling is enabled on the stage (1000 rps, 2000 burst).
- DynamoDB tables use `RemovalPolicy.RETAIN`, so `cdk destroy` will not delete session data.
- Lambda functions run with `LOG_LEVEL=INFO` and server-side error messages are sanitized before being returned to the client.

### Production-hardening follow-ups (not included)

- The Lambda execution role has broad IAM permissions for cross-region resource operations. Review and scope down for production use.
- The CloudFront-hosted UI has no authentication by default. Add Cognito or API Gateway authorizers before exposing to untrusted users.
- The tool is single-account — cross-account replication is not supported.

## Supported Versions

Only the latest release is actively maintained.

## Dependencies

This project uses third-party open-source dependencies (FastAPI, Pydantic, boto3, React, Cloudscape Design System). We recommend regularly updating dependencies to incorporate security patches.
