"""Error classification for replication failures.

Classifies exceptions into actionable categories with guidance text,
so operators can diagnose and resolve issues without inspecting backend logs.
"""

import re
from typing import Literal

from pydantic import BaseModel


class ErrorClassification(BaseModel):
    """Classified replication error with actionable guidance."""

    error_type: Literal[
        "permission", "not-found", "timeout", "conflict", "quota", "service-error", "unknown"
    ]
    raw_message: str
    guidance: str
    iam_action: str | None = None
    quota_name: str | None = None


def _extract_iam_action(message: str) -> str | None:
    """Try to extract the denied IAM action from an error message."""
    # Common patterns: "User: ... is not authorized to perform: iam:CreateRole"
    # or "... when calling the CreateRole operation"
    match = re.search(r"to perform:\s*(\S+)", message)
    if match:
        return match.group(1)
    match = re.search(r"calling the (\w+) operation", message)
    if match:
        return match.group(1)
    return None


def _extract_quota_name(message: str) -> str | None:
    """Try to extract the quota/limit name from an error message."""
    # Common patterns: "Rate exceeded", "Limit exceeded for resource: ..."
    match = re.search(r"[Ll]imit exceeded(?:\s+for\s+resource)?:?\s*(.+?)(?:\.|$)", message)
    if match:
        return match.group(1).strip()
    match = re.search(r"[Qq]uota.*?:\s*(.+?)(?:\.|$)", message)
    if match:
        return match.group(1).strip()
    return None


def classify_error(exc: Exception) -> ErrorClassification:
    """Classify an exception into an actionable error type.

    Matches on:
    - AccessDeniedException, UnauthorizedOperation → permission
    - ResourceNotFoundException, 404 → not-found
    - Lambda timeout, 504 → timeout
    - ResourceConflictException → conflict
    - LimitExceededException, TooManyRequestsException → quota
    - Other ClientError → service-error
    - Everything else → unknown
    """
    message = str(exc)
    error_code = ""

    # Extract error code from botocore ClientError
    if hasattr(exc, "response"):
        response = getattr(exc, "response", {})
        error_info = response.get("Error", {})
        error_code = error_info.get("Code", "")
        if not message or message == str(type(exc)):
            message = error_info.get("Message", message)

    # Permission errors
    if error_code in ("AccessDeniedException", "UnauthorizedOperation", "AccessDenied"):
        iam_action = _extract_iam_action(message)
        action_text = f" `{iam_action}`" if iam_action else ""
        return ErrorClassification(
            error_type="permission",
            raw_message=message,
            guidance=f"Add{action_text} permission to the execution role.",
            iam_action=iam_action,
        )

    # Not-found errors
    if error_code in ("ResourceNotFoundException", "NotFoundException", "NoSuchEntity"):
        return ErrorClassification(
            error_type="not-found",
            raw_message=message,
            guidance="Verify the resource ARN and confirm the resource exists in the expected region.",
        )
    if error_code and "404" in error_code:
        return ErrorClassification(
            error_type="not-found",
            raw_message=message,
            guidance="Verify the resource ARN and confirm the resource exists in the expected region.",
        )

    # Timeout errors
    if error_code in ("RequestTimeout", "RequestTimeoutException"):
        return ErrorClassification(
            error_type="timeout",
            raw_message=message,
            guidance="The operation may still be running. Check the session status in a few minutes.",
        )
    if "timed out" in message.lower() or "timeout" in message.lower() or "504" in message:
        return ErrorClassification(
            error_type="timeout",
            raw_message=message,
            guidance="The operation may still be running. Check the session status in a few minutes.",
        )

    # Quota / rate limit errors
    if error_code in ("LimitExceededException", "TooManyRequestsException", "Throttling", "ThrottlingException"):
        quota_name = _extract_quota_name(message)
        quota_text = f" `{quota_name}`" if quota_name else ""
        return ErrorClassification(
            error_type="quota",
            raw_message=message,
            guidance=f"Service quota{quota_text} exceeded. Request a quota increase via the Service Quotas console.",
            quota_name=quota_name,
        )

    # Conflict errors
    if error_code in ("ResourceConflictException", "ConflictException", "ResourceAlreadyExistsException"):
        return ErrorClassification(
            error_type="conflict",
            raw_message=message,
            guidance="Resource already exists in the target region. Use cleanup before retrying.",
        )

    # Other AWS service errors (ClientError with a code we didn't match above)
    if error_code:
        return ErrorClassification(
            error_type="service-error",
            raw_message=message,
            guidance=f"AWS service error: {error_code}. Check CloudWatch logs for details.",
        )

    # Unknown / non-AWS errors
    return ErrorClassification(
        error_type="unknown",
        raw_message=message,
        guidance="Unexpected error. Check CloudWatch logs for the full stack trace.",
    )
