# Security Findings Disposition — ACGR Replication Starter Pack

**Scan:** Probe `ProbeScanExport-776eb515-2207-4502-87c1-0d03a01600ec-main-20260809`
**Findings:** 200 total — 8 ERROR, 181 WARNING, 11 INFO
**Scanners:** semgrep (187), bandit (11), grype (2)
**Repository:** `amazon-connect-acgr-replication-starter-pack`, branch `main`
**Context:** Proof-of-concept tool. Not production-deployed. Processes no customer data.

> Note on severity tiers: this export contains only `ERROR`, `WARNING`, and `INFO`.
> There is no `CRITICAL` or `HIGH` tier in the data, so `ERROR` is the highest
> severity present.

---

## Executive summary

All 200 findings have been triaged against the source. **No exploitable vulnerability was
identified.** 193 of 200 are false positives or non-security code-quality / localization
lint. 7 are genuine but low-risk hygiene items (dependency version pinning) with a clear
remediation path. Both dependency CVEs are provably build-time-only and cannot reach the
deployed artifact.

| Outcome | Count |
| --- | --- |
| Not applicable (localization lint) | 164 |
| False positive (verified against source) | 27 |
| Not exploitable (build-time-only dependency) | 2 |
| Accepted — valid hygiene, remediation planned | 7 |
| **Exploitable vulnerabilities** | **0** |

---

## Disposition by rule class

### 1. `jsx-not-internationalized` — 164 findings — WARNING
**Disposition: Not applicable.**

Localization lint on React UI strings. No security relevance. The tool is a
single-locale (en-US) internal operator console; internationalization is not a
requirement for this PoC.

### 2. `B110` try/except/pass — 8 findings — INFO
**Disposition: False positive.**

All eight are intentional best-effort paths. Verified examples:

- `replication/orchestrator.py:288` and `api/routes.py:1691` — optional ARN parsing where
  a parse failure correctly leaves an empty default and the caller degrades gracefully.
- `replication/s3_replication.py:61` — an S3 `head_bucket` existence probe that proceeds
  to bucket creation on any error.

No exception handler is masking a security control or an authorization decision.

### 3. `arbitrary-sleep` — 7 findings — **ERROR**
**Disposition: False positive.**

The rule targets debug `time.sleep()` calls left in code. All seven are bounded polling
loops awaiting genuinely asynchronous AWS state transitions:

| Location | Waiting for | Bound |
| --- | --- | --- |
| `association/resource_association.py:618` | Retry after `associate_lambda_function` (IAM propagation) | 1 × 5s |
| `association/resource_association.py:922` | Amazon Lex bot locale build | 12 × 5s |
| `association/resource_association.py:945` | Lex bot version reaching `Available` | 12 × 5s |
| `association/resource_association.py:991` | Bot alias replicating to target Region | 12 × 5s |
| `association/resource_association.py:1242` | Firehose delivery stream reaching `ACTIVE` | `max_wait` guard |
| `replication/lex_replication.py:111` | Bot reaching `Available` after create | 60s |
| `replication/lex_replication.py:391` | Locale build (`_BUILD_MAX_WAIT_SECONDS`) | 300s |

Every loop has an explicit iteration cap or elapsed-time guard; none can spin
indefinitely. No security impact. See *Residual risk 4* for the related (non-security)
Lambda-duration consideration.

### 4. `package-dependencies-check` — 7 findings — WARNING
**Disposition: Accepted — valid hygiene finding. Remediation planned.**

Floating semver ranges in `frontend/package.json`. Mitigated in practice by a committed
`package-lock.json`, which pins the fully resolved dependency tree for reproducible
installs. Pinning exact versions in `package.json` is a planned improvement (item 2 below).

### 5. `raw-html-format` — 2 findings — WARNING
**Disposition: False positive. Already annotated in source.**

`api/routes.py:192` and `api/routes.py:334`. Both sites already carry a `# nosemgrep`
annotation with written justification. These are FastAPI **JSON** `400` `detail` strings,
not HTML. The interpolated value is the `resource` segment of an Amazon Connect ARN that
has already been validated upstream. FastAPI serialises the response as JSON; it is never
rendered as HTML. No XSS sink exists on this path.

### 6. `react-props-spreading` — 2 findings — WARNING
**Disposition: False positive.**

JSX spread onto Cloudscape table cell components (`Inventory/ResourceTable.tsx`). This is
a style/robustness lint about forwarding invalid DOM props — not an injection or
data-exposure vector.

### 7. `is-function-without-parentheses` — 2 findings — WARNING
**Disposition: False positive.**

`replication/lambda_replication.py:684`, `discovery/orchestrator.py:108`. Lint on a
function reference intentionally used without invocation. No security impact.

### 8. `logging-error-without-handling` — 2 findings — WARNING
**Disposition: False positive.**

`replication/step_functions_orchestrator.py:242`, `api/routes.py:2860`. `logger.error` on
code paths that deliberately continue because the operation is non-fatal and best-effort.
Errors are recorded and surfaced to the operator through the session status API.

### 9. `B112` try/except/continue — 2 findings — INFO
**Disposition: False positive.**

`api/routes.py:1869`, `replication/storage_config_replication.py:169`. Loop-continue on
optional per-item enrichment: a single item's failure must not abort the whole batch.

### 10. `B311` non-cryptographic random — 1 finding — INFO
**Disposition: False positive.**

`replication/resource_lambda_handler.py:233`. `random` is used solely to add **jitter to
exponential backoff** on AWS throttling retries. There is no security or cryptographic
use; a CSPRNG would provide no benefit here.

### 11. `wildcard-cors` — 1 finding — WARNING
**Disposition: False positive — compensating control in place.**

`backend/main.py:28` sets `allow_origins=["*"]` on the FastAPI CORS middleware. Two
compensating controls make this non-exploitable:

1. **The authoritative CORS allow-list is enforced upstream at Amazon API Gateway**,
   scoped to the CloudFront distribution domain (see `infra/stack.py`; overridable via the
   `allowedOrigin` CDK context variable). The app-level middleware is a permissive
   fallback for local development only.
2. **`allow_credentials=False`.** The API is stateless and uses no cookies or
   `Authorization` header, so no credentialed cross-origin request can succeed. Per the
   CORS specification, browsers reject a wildcard origin on credentialed requests.

The rationale is documented in a code comment at the call site.

### 12. `GHSA-mw96-cpmx-2vgc` — rollup arbitrary file write via path traversal — 1 finding — **ERROR**
**Disposition: Not exploitable.**

Evidence:

```
$ npm ls rollup --omit=dev
acgr-replication-starter-pack-frontend@0.1.0
└── (empty)
```

Rollup is **absent from the production dependency tree**. It is present only transitively
through `vite@6.4.1` and `vitest@2.1.9`, both **devDependencies**:

```
├─┬ vite@6.4.1
│ └── rollup@4.57.1
└─┬ vitest@2.1.9
  └─┬ vite@5.4.21
    └── rollup@4.57.1 deduped
```

Rollup executes only at build and test time. The deployed artifact is pre-compiled static
assets served from Amazon S3 via Amazon CloudFront; rollup is never shipped to, or
executed in, the runtime environment. Exploitation would require an attacker to already
control the build environment and its inputs.

### 13. `GHSA-67mh-4wv8-2f99` — esbuild development server — 1 finding — WARNING
**Disposition: Not exploitable.**

The advisory is explicitly scoped to the esbuild **development server**, which is never
run in this deployment. `esbuild` is a transitive devDependency of Vite; the same
build-time-only reasoning as item 12 applies.

---

## Planned remediation (non-blocking)

1. **Upgrade `vite` and `vitest`** — clears both dependency advisories (items 12 and 13)
   and removes them from future scans. Low risk: build tooling only, no runtime change.
2. **Pin exact frontend dependency versions** in `package.json`, complementing the
   existing `package-lock.json` (addresses item 4).
3. **Replace bare `except: pass` with scoped exception types and debug-level logging**
   to improve diagnosability (addresses items 2 and 9 — code quality, not security).

---

## Residual risks — declared proactively

The following are **not** scanner findings; static analysis cannot detect them. We raise
them explicitly as the genuine limitations of this proof of concept. They are documented
in `SECURITY.md` and in a prominent warning at the top of `README.md`.

1. **No authentication** on the CloudFront-hosted UI or the API. Anyone able to reach the
   endpoint can trigger resource replication **and deletion**. This is the highest
   residual risk and the primary reason the tool must not be used in production.
2. **Broad IAM role** on the backend Lambda — create, read, and delete across multiple
   AWS services in two Regions. Intentionally permissive so the PoC can operate; this is
   **not least-privilege** and is unsuitable for production.
3. **Destructive, irreversible cleanup paths.** The tool disassociates and deletes
   resources in the target Region. IAM roles are explicitly excluded from deletion.
4. **Lambda duration exposure.** The bounded waits in item 3 execute inside a 900-second
   Lambda. Associating many Amazon Lex bots in a single invocation could approach that
   ceiling. This is a reliability and cost concern rather than a security one; the
   remediation is to move long waits into AWS Step Functions `Wait` states.

**Control statement.** Use of this tool is restricted to non-production AWS accounts and
non-production Amazon Connect instances. Any wider use requires authentication, IAM
scope-down, and a full security review, as stated in `README.md`.

---

## Caveats for reviewers

1. **Possible export truncation.** This export contains exactly **200 rows**. That round
   number may indicate a UI or export cap. The true total should be confirmed in the Probe
   console before this disposition is treated as covering the complete finding set.
2. **Python dependency scope.** This scan covers first-party code and npm dependencies.
   The vendored Python packages under `backend/` are installed at deploy time from
   `requirements.txt` and are excluded from version control, so they may not be in scope.
   A separate SCA pass over `requirements.txt` is recommended to complete coverage.
