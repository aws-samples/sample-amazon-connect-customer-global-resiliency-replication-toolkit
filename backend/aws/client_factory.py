"""AWS client factory for creating boto3 clients with proper credential handling.

In Lambda mode (DEPLOYMENT_MODE=lambda), clients are created without explicit
credentials — they rely on the Lambda execution role's automatic credential injection.

In local mode (DEPLOYMENT_MODE=local, the default), clients use the default boto3
credential chain (env vars, ~/.aws/credentials, instance profile, etc.).
"""

from __future__ import annotations

import os

import boto3


def get_deployment_mode() -> str:
    """Return the current deployment mode from the DEPLOYMENT_MODE env var.

    Returns ``"lambda"`` or ``"local"`` (default).
    """
    return os.environ.get("DEPLOYMENT_MODE", "local").lower()


def create_client(service_name: str, region_name: str):
    """Create a boto3 client for the given service and region.

    In both Lambda and local modes, the client is created without explicit
    access key or secret key parameters.  In Lambda mode this means the
    execution role credentials are used automatically; in local mode the
    default credential chain applies.

    Args:
        service_name: The AWS service name (e.g. ``"lambda"``, ``"connect"``).
        region_name: The AWS region (e.g. ``"us-west-2"``).

    Returns:
        A boto3 client for the requested service and region.
    """
    return boto3.client(service_name, region_name=region_name)


def create_source_client(service_name: str, source_region: str):
    """Convenience wrapper: create a client targeting the source region.

    Args:
        service_name: The AWS service name.
        source_region: The ACGR source region.

    Returns:
        A boto3 client for the requested service in the source region.
    """
    return create_client(service_name, source_region)


def create_target_client(service_name: str, target_region: str):
    """Convenience wrapper: create a client targeting the target region.

    Args:
        service_name: The AWS service name.
        target_region: The ACGR target region.

    Returns:
        A boto3 client for the requested service in the target region.
    """
    return create_client(service_name, target_region)
