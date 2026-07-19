"""ARN parsing, region extraction, ACGR region pair mapping, and ARN rewriting utilities."""


# ACGR-supported region pairs: source_region -> target_region
ACGR_REGION_PAIRS: dict[str, str] = {
    "us-west-2": "us-east-1",
    "us-east-1": "us-west-2",
    "eu-west-2": "eu-central-1",
    "eu-central-1": "eu-west-2",
    "ap-northeast-1": "ap-northeast-3",
}

# Supported region pairs formatted for error messages
_SUPPORTED_PAIRS_DISPLAY = [
    "us-west-2 <-> us-east-1",
    "eu-west-2 <-> eu-central-1",
    "ap-northeast-1 -> ap-northeast-3",
]


def supported_source_regions() -> list[str]:
    """Return the sorted list of AWS Regions that can act as an ACGR *source*.

    These are the Regions in which an Amazon Connect instance can be selected
    for replication. Osaka (ap-northeast-3) is intentionally excluded because
    Amazon Connect Global Resiliency supports it as a replica target only.

    This is the single source of truth for the set of regions the API accepts;
    callers (e.g. instance listing) should derive their allow-lists from here
    rather than hard-coding a separate list.
    """
    return sorted(ACGR_REGION_PAIRS.keys())


def parse_arn(arn: str) -> dict:
    """Parse an ARN string into its component parts.

    ARN format: arn:<partition>:<service>:<region>:<account>:<resource>

    Args:
        arn: A full Amazon Resource Name string.

    Returns:
        A dict with keys: partition, service, region, account, resource.

    Raises:
        ValueError: If the ARN does not have the expected format (at least 6 colon-separated parts).
    """
    parts = arn.split(":", 5)
    if len(parts) < 6 or parts[0] != "arn":
        raise ValueError(
            f"Invalid ARN format: '{arn}'. Expected format: arn:<partition>:<service>:<region>:<account>:<resource>"
        )
    return {
        "partition": parts[1],
        "service": parts[2],
        "region": parts[3],
        "account": parts[4],
        "resource": parts[5],
    }


def extract_region(arn: str) -> str:
    """Extract the region from an ARN.

    Args:
        arn: A full Amazon Resource Name string.

    Returns:
        The region component of the ARN.

    Raises:
        ValueError: If the ARN is invalid or the region component is empty.
    """
    parsed = parse_arn(arn)
    region = parsed["region"]
    if not region:
        raise ValueError(f"ARN has no region component: '{arn}'")
    return region


def resolve_target_region(source_region: str) -> str:
    """Look up the ACGR target region for a given source region.

    Args:
        source_region: The AWS region to resolve the ACGR pair for.

    Returns:
        The ACGR-paired target region.

    Raises:
        ValueError: If the source region is not in a supported ACGR pair.
    """
    target = ACGR_REGION_PAIRS.get(source_region)
    if target is None:
        supported = ", ".join(_SUPPORTED_PAIRS_DISPLAY)
        raise ValueError(
            f"Unsupported ACGR region: '{source_region}'. Supported region pairs: {supported}"
        )
    return target


def rewrite_arn(arn: str, target_region: str) -> str:
    """Replace the region component of an ARN with the target region.

    All other ARN parts (partition, service, account, resource) are preserved.

    Args:
        arn: The original ARN string.
        target_region: The region to substitute into the ARN.

    Returns:
        A new ARN string with the region replaced.

    Raises:
        ValueError: If the ARN is invalid.
    """
    parsed = parse_arn(arn)
    return ":".join([
        "arn",
        parsed["partition"],
        parsed["service"],
        target_region,
        parsed["account"],
        parsed["resource"],
    ])
