# Contributing

Thanks for your interest in contributing to the Amazon Connect ACGR Resource Replicator. This document covers how to file issues, submit pull requests, and run the test suites locally.

## Reporting Issues

Please open a GitHub issue with a clear title, reproduction steps, expected behavior, actual behavior, and relevant environment detail (AWS region, Python version, Node version). Do NOT file security issues publicly — see [SECURITY.md](SECURITY.md).

## Submitting Pull Requests

1. Fork the repository and create a feature branch off `main`.
2. Make your change. Keep commits focused and the diff small where possible.
3. Run the full test suite locally (see below) and confirm everything passes.
4. Open a pull request with a clear title, a short description of the problem and the solution, and a reference to the related issue if one exists.

## Running Tests

### Backend (Python)

```
cd acgr-replication-starter-pack/backend
python3 -m pytest tests/
```

### Infrastructure (CDK)

```
cd acgr-replication-starter-pack/infra
python3 -m pytest tests/
```

### Frontend (TypeScript / React)

```
cd acgr-replication-starter-pack/frontend
npm install
npm run test
```

### Documentation invariants

```
bash acgr-replication-starter-pack/scripts/check_docs.sh
```

## Development Setup

See [README.md](README.md) for the full development and deployment instructions, including how to deploy to a target AWS account via `./deploy.sh`.

## Code of Conduct

By participating in this project, you agree to abide by standard open-source community guidelines: be respectful, be constructive, and assume good intent. Harassment, discrimination, and personal attacks are not tolerated.

## Security

For vulnerability disclosure, see [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions will be licensed under the MIT License (see [LICENSE](LICENSE)).
