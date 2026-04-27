"""Unit tests for the `_safe_error_detail` helper in `api.routes`.

Validates Requirements 1.8 and 1.9 from the
acgr-idr-public-release-readiness spec:

- 1.8: Safe error responses must not leak raw exception content, class
  name, or tracebacks to clients.
- 1.9: The full exception must still be logged server-side at ERROR level
  via `logger.exception(...)` so operators can debug from CloudWatch.
"""

import logging

import pytest

from api.routes import _safe_error_detail


def test_returns_user_message_exactly():
    """The helper must return the user_message argument verbatim."""
    exc = RuntimeError("secret-internal-detail")

    result = _safe_error_detail(exc, "Friendly public message")

    assert result == "Friendly public message"


def test_logs_full_exception_at_error_level(caplog):
    """The helper must log the full exception (type + message + traceback)
    at ERROR level so it is captured in CloudWatch Logs (Requirement 1.9)."""
    # Raise and catch so the exception has a real traceback attached.
    try:
        raise RuntimeError("secret-internal-detail")
    except RuntimeError as exc:
        with caplog.at_level(logging.ERROR, logger="api.routes"):
            _safe_error_detail(exc, "Friendly public message")

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "Expected at least one ERROR-level log record"

    # `logger.exception(...)` attaches exc_info, which the default formatter
    # renders into `caplog.text`. That rendered output is what operators see
    # in CloudWatch and must contain the raw exception type + message.
    assert "RuntimeError" in caplog.text
    assert "secret-internal-detail" in caplog.text


def test_return_value_omits_exception_content():
    """The returned string must not contain the raw exception message or
    class name (Requirement 1.8 — no raw exception leakage to clients)."""
    result = _safe_error_detail(
        RuntimeError("xyz-sentinel-789"), "Static message"
    )

    assert "xyz-sentinel-789" not in result
    assert "RuntimeError" not in result
