"""Invariant test: the `/api/health` route must not be registered.

Validates Requirement 1.1 (Property P1.5 — route-registration invariant)
from the acgr-idr-public-release-readiness spec. The `/api/health` endpoint
was removed as part of public-release hardening and must not be
re-introduced.
"""

from main import app


def test_health_endpoint_not_registered():
    """Assert no route on the FastAPI app has path == '/api/health'."""
    registered_paths = [getattr(route, "path", None) for route in app.routes]

    assert "/api/health" not in registered_paths, (
        "`/api/health` must not be registered; it was removed as part of "
        "public-release hardening."
    )
