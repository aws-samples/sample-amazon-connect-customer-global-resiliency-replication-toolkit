"""Replica resource naming — single source of truth for the replica suffix.

S3 bucket names (and, for consistency, Kinesis/Firehose stream names) must be
globally/region unique, so replicated resources get a suffix. The suffix is
configurable via the ACGR_REPLICA_SUFFIX environment variable and defaults to
"-dr". Everything that constructs or resolves a replica name MUST use these
helpers so replication, association, cleanup, and the status live-check all
agree on the same name.
"""

from __future__ import annotations

import os
import re

_DEFAULT_SUFFIX = "-dr"
# S3 bucket naming: 3-63 chars, lowercase letters/numbers/hyphens/dots, and the
# suffix we append must keep the name valid. Restrict to a safe charset.
_SAFE_SUFFIX = re.compile(r"^[a-z0-9][a-z0-9-]{0,20}$")


def replica_suffix() -> str:
    """Return the configured replica suffix (default '-dr').

    Falls back to the default if the env var is unset, empty, or fails basic
    validation (so a bad value can't produce invalid bucket names).
    """
    raw = os.environ.get("ACGR_REPLICA_SUFFIX", "").strip()
    if not raw:
        return _DEFAULT_SUFFIX
    # Normalise: allow the operator to omit the leading hyphen.
    candidate = raw if raw.startswith("-") else f"-{raw}"
    body = candidate.lstrip("-")
    if _SAFE_SUFFIX.match(body):
        return candidate
    return _DEFAULT_SUFFIX


def target_replica_name(source_name: str) -> str:
    """Return the replica name for a source resource name."""
    return f"{source_name}{replica_suffix()}"
