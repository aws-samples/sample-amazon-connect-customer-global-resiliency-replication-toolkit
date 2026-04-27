"""Property test for P4: exception responses never leak raw exception content.

Validates that when an internal dependency raises an unexpected exception,
the HTTP response body does NOT contain:
  * the raw exception message (sentinel string)
  * the exception class name ("RuntimeError")
  * any traceback ("Traceback")

Rather than enumerating every single FastAPI route (brittle because every
route has different request shapes), we take a representative-route approach
that covers the distinct exception paths in both ``api/routes.py`` and
``api/instance_routes.py``:

  1. ``POST /api/validate-instance`` — ``create_source_client`` path
  2. ``POST /api/discover``          — ``run_discovery`` path
  3. ``GET  /api/inventory/{id}``    — ``_session_store.get_session`` path
  4. ``POST /api/replicate``         — ``_session_store.get_session`` path
  5. ``GET  /api/list-instances``    — ``create_client`` path (instance_routes)

Each row patches the real dependency to raise ``RuntimeError(SENTINEL)``
and asserts the response body is sanitized.

Validates: Requirements 1.8
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from main import app


client = TestClient(app, raise_server_exceptions=False)

# A valid Connect instance ARN that passes Pydantic's ``_ARN_PATTERN`` regex
# so the request reaches the try/except block containing the dependency call.
VALID_ARN = (
    "arn:aws:connect:us-east-1:123456789012:"
    "instance/f4d29ac8-fcfc-4cc1-be06-545ac29aefe9"
)

# Deliberate, high-entropy sentinel. The CAPITAL suffix guards against a
# naive lower-cased log leak being miscounted as safe.
SENTINEL = "sentinel-leak-value-xyz-CAPITAL"


def _sentinel_side_effect(*args, **kwargs):
    """Patch side-effect: raise a RuntimeError carrying the sentinel string."""
    raise RuntimeError(SENTINEL)


# Parametrized matrix covering the five representative exception paths.
# Each tuple is (patch_target, method, path, request_kwargs).
_CASES = [
    (
        "api.routes.create_source_client",
        "post",
        "/api/validate-instance",
        {"json": {"instanceArn": VALID_ARN}},
    ),
    (
        "api.routes.run_discovery",
        "post",
        "/api/discover",
        {"json": {"instanceArn": VALID_ARN}},
    ),
    (
        "api.routes._session_store.get_session",
        "get",
        "/api/inventory/session-id-x",
        {},
    ),
    (
        "api.routes._session_store.get_session",
        "post",
        "/api/replicate",
        {"json": {"sessionId": "sid-x", "resourceIds": ["r1"]}},
    ),
    (
        "api.instance_routes.create_client",
        "get",
        "/api/list-instances",
        {"params": {"region": "us-east-1"}},
    ),
]


@pytest.mark.parametrize(
    "patch_target, method, path, request_kwargs",
    _CASES,
    ids=[c[2] for c in _CASES],
)
def test_route_does_not_leak_exception_content(
    patch_target: str,
    method: str,
    path: str,
    request_kwargs: dict,
):
    """For each representative route, a dependency raising RuntimeError(SENTINEL)
    must not surface the sentinel, the class name, or a traceback in the
    response body."""
    with patch(patch_target, side_effect=_sentinel_side_effect):
        resp = getattr(client, method)(path, **request_kwargs)

    body_text = resp.text

    # The request should reach the exception path and be converted to an
    # error response. We accept any 4xx/5xx status; the core invariant is
    # on the body content.
    assert resp.status_code >= 400, (
        f"Expected error status for {path}, got {resp.status_code}: {body_text!r}"
    )

    assert SENTINEL not in body_text, (
        f"Route {path} leaked sentinel exception message in response body: "
        f"{body_text!r}"
    )
    assert "Traceback" not in body_text, (
        f"Route {path} leaked traceback in response body: {body_text!r}"
    )
    assert "RuntimeError" not in body_text, (
        f"Route {path} leaked exception class name in response body: {body_text!r}"
    )
