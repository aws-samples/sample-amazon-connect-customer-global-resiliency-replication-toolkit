"""FastAPI application entry point with CORS and audit logging middleware."""

import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router
from api.instance_routes import instance_router

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Connect ACGR Resource Replicator",
    description="Discovers and replicates AWS resources associated with an Amazon Connect instance to its ACGR-paired DR region.",
    version="0.1.0",
)

# CORS: the authoritative, scoped origin allow-list is enforced at API Gateway
# (restricted to the CloudFront distribution domain). This app-level middleware
# is a permissive fallback for local development. Credentials are disabled
# because the API is stateless and uses no cookies/authorization — pairing
# `allow_origins=["*"]` with `allow_credentials=True` is invalid per the CORS
# spec (browsers reject a wildcard origin on credentialed requests).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def audit_logging_middleware(request: Request, call_next):
    """Log each API request with timestamp, method, path, and response status.

    Captures user identity (from headers if available), the HTTP method,
    the request path, and the response status code for audit purposes.
    """
    start_time = time.time()
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Extract user identity from common headers (API Gateway sets these)
    user_identity = (
        request.headers.get("x-amzn-oidc-identity")
        or request.headers.get("x-forwarded-user")
        or (request.client.host if request.client else "unknown")
    )

    response = await call_next(request)

    duration_ms = (time.time() - start_time) * 1000

    logger.info(
        "AUDIT | timestamp=%s | user=%s | method=%s | path=%s | status=%d | duration_ms=%.1f",
        timestamp,
        user_identity,
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )

    return response


app.include_router(router)
app.include_router(instance_router)
