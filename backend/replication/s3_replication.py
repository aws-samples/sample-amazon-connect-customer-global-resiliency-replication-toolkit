"""S3 bucket replication for Connect ACGR Resource Replicator.

Creates S3 buckets in the target region with matching configuration
(encryption, versioning, bucket policy, CORS, lifecycle rules) and a
hardcoded '-dr' suffix (S3 bucket names must be globally unique).
"""

from __future__ import annotations

import json
import logging

from aws.client_factory import create_source_client, create_target_client
from models.resources import ResourceBase

logger = logging.getLogger(__name__)


def replicate_s3_bucket(
    resource: ResourceBase,
    target_region: str,
    resource_tags: dict[str, str] | None = None,
) -> str:
    """Create an S3 bucket in the target region matching the source bucket config.

    The new bucket name is: {original_name}-dr
    (hardcoded suffix since S3 bucket names must be globally unique)

    Args:
        resource: The S3BucketResource to replicate.
        target_region: The ACGR target region.
        resource_tags: Tags to apply to the replicated bucket.

    Returns:
        The ARN of the created bucket.
    """
    source_name = resource.name
    target_name = f"{source_name}-dr"

    s3_client = create_target_client("s3", target_region)

    # Check if bucket already exists
    try:
        s3_client.head_bucket(Bucket=target_name)
        logger.info("S3 bucket '%s' already exists, reusing", target_name)
        return f"arn:aws:s3:::{target_name}"
    except s3_client.exceptions.ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "404" or error_code == "NoSuchBucket":
            pass  # Bucket doesn't exist, create it
        elif error_code == "403":
            # Bucket exists but we don't have access — likely owned by someone else
            raise ValueError(
                f"S3 bucket '{target_name}' exists but access is denied. "
                "Choose a different suffix."
            ) from e
        else:
            raise
    except Exception:
        pass  # Proceed to create

    # Create the bucket
    create_params: dict = {"Bucket": target_name}

    # LocationConstraint is required for all regions except us-east-1
    if target_region != "us-east-1":
        create_params["CreateBucketConfiguration"] = {
            "LocationConstraint": target_region
        }

    try:
        s3_client.create_bucket(**create_params)
        logger.info("Created S3 bucket '%s' in %s", target_name, target_region)
    except s3_client.exceptions.BucketAlreadyOwnedByYou:
        logger.info("S3 bucket '%s' already owned by us, reusing", target_name)
    except s3_client.exceptions.BucketAlreadyExists:
        logger.info("S3 bucket '%s' already exists globally, reusing", target_name)

    # Apply encryption if source had it
    encryption_type = getattr(resource, "encryption_type", None)
    if encryption_type:
        try:
            s3_client.put_bucket_encryption(
                Bucket=target_name,
                ServerSideEncryptionConfiguration={
                    "Rules": [
                        {
                            "ApplyServerSideEncryptionByDefault": {
                                "SSEAlgorithm": encryption_type,
                            }
                        }
                    ]
                },
            )
            logger.info("Applied %s encryption to bucket '%s'", encryption_type, target_name)
        except Exception:
            logger.warning("Failed to apply encryption to bucket '%s'", target_name, exc_info=True)

    # Apply versioning if source had it
    versioning_enabled = getattr(resource, "versioning_enabled", False)
    if versioning_enabled:
        try:
            s3_client.put_bucket_versioning(
                Bucket=target_name,
                VersioningConfiguration={"Status": "Enabled"},
            )
            logger.info("Enabled versioning on bucket '%s'", target_name)
        except Exception:
            logger.warning("Failed to enable versioning on bucket '%s'", target_name, exc_info=True)

    # Block public access (security best practice, but allow policy-based access for Connect)
    try:
        s3_client.put_public_access_block(
            Bucket=target_name,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": False,
                "RestrictPublicBuckets": False,
            },
        )
    except Exception:
        logger.debug("Failed to set public access block on '%s'", target_name)

    # Ensure bucket ownership controls are set (required for Connect ownership verification)
    try:
        s3_client.put_bucket_ownership_controls(
            Bucket=target_name,
            OwnershipControls={
                "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
            },
        )
    except Exception:
        logger.debug("Failed to set ownership controls on '%s'", target_name)

    # Replicate bucket policy (with ARN/name rewriting)
    _replicate_bucket_policy(source_name, target_name, target_region)

    # Ensure Connect can access the bucket (required for AssociateInstanceStorageConfig)
    _ensure_connect_bucket_policy(target_name, target_region)

    # Replicate CORS configuration
    _replicate_cors_configuration(source_name, target_name, target_region)

    # Replicate lifecycle rules
    _replicate_lifecycle_rules(source_name, target_name, target_region)

    # Apply user-provided tags
    if resource_tags:
        try:
            tag_set = [{"Key": k, "Value": v} for k, v in resource_tags.items()]
            s3_client.put_bucket_tagging(
                Bucket=target_name,
                Tagging={"TagSet": tag_set},
            )
            logger.info("Applied %d tag(s) to bucket '%s'", len(tag_set), target_name)
        except Exception:
            logger.warning("Failed to apply tags to bucket '%s'", target_name, exc_info=True)

    return f"arn:aws:s3:::{target_name}"


def _ensure_connect_bucket_policy(bucket_name: str, target_region: str) -> None:
    """Ensure the bucket has a policy allowing Amazon Connect to read/write.

    Connect's AssociateInstanceStorageConfig requires the bucket to be
    accessible by the Connect service. If the bucket has no policy (e.g.
    because we couldn't copy the source policy), we add a minimal policy
    granting Connect the required access.

    If the bucket already has a policy, we merge the Connect statement
    into it (avoiding duplicates).
    """
    s3_client = create_target_client("s3", target_region)

    # Get the account ID for the policy condition
    try:
        import boto3
        sts = boto3.client("sts")
        account_id = sts.get_caller_identity()["Account"]
    except Exception:
        logger.warning("Could not determine account ID for Connect bucket policy")
        return

    connect_statement = {
        "Sid": "AllowConnectAccess",
        "Effect": "Allow",
        "Principal": {"Service": "connect.amazonaws.com"},
        "Action": [
            "s3:GetObject",
            "s3:PutObject",
            "s3:GetBucketLocation",
            "s3:ListBucket",
            "s3:GetBucketAcl",
            "s3:PutBucketAcl",
            "s3:GetObjectAcl",
            "s3:DeleteObject",
        ],
        "Resource": [
            f"arn:aws:s3:::{bucket_name}",
            f"arn:aws:s3:::{bucket_name}/*",
        ],
    }

    try:
        # Check if bucket already has a policy
        try:
            resp = s3_client.get_bucket_policy(Bucket=bucket_name)
            existing_policy = json.loads(resp.get("Policy", "{}"))
            statements = existing_policy.get("Statement", [])
            # Check if Connect statement already exists
            for stmt in statements:
                if stmt.get("Sid") == "AllowConnectAccess":
                    logger.debug("Connect bucket policy already exists on '%s'", bucket_name)
                    return
            # Add the Connect statement
            statements.append(connect_statement)
            existing_policy["Statement"] = statements
            policy_str = json.dumps(existing_policy)
        except s3_client.exceptions.ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("NoSuchBucketPolicy", "AccessDenied"):
                # No existing policy — create a new one
                policy_str = json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [connect_statement],
                })
            else:
                raise

        s3_client.put_bucket_policy(Bucket=bucket_name, Policy=policy_str)
        logger.info("Applied Connect access policy to bucket '%s'", bucket_name)
    except Exception:
        logger.warning(
            "Failed to apply Connect bucket policy to '%s'", bucket_name, exc_info=True
        )


def _replicate_bucket_policy(
    source_bucket: str, target_bucket: str, target_region: str
) -> None:
    """Replicate the bucket policy from source to target, rewriting bucket references.

    Rewrites the source bucket name to the target bucket name in the policy
    document. This is best-effort — failures are logged but don't fail replication.
    """
    source_region = _get_source_region_from_bucket(source_bucket)
    s3_source = create_source_client("s3", source_region) if source_region else create_source_client("s3", target_region)
    s3_target = create_target_client("s3", target_region)

    try:
        resp = s3_source.get_bucket_policy(Bucket=source_bucket)
        policy_str = resp.get("Policy", "")
        if not policy_str:
            return

        # Rewrite bucket name references in the policy
        rewritten_policy = policy_str.replace(source_bucket, target_bucket)

        s3_target.put_bucket_policy(Bucket=target_bucket, Policy=rewritten_policy)
        logger.info("Replicated bucket policy from '%s' to '%s'", source_bucket, target_bucket)
    except s3_source.exceptions.ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "NoSuchBucketPolicy":
            logger.debug("No bucket policy on source bucket '%s'", source_bucket)
        else:
            logger.warning(
                "Failed to replicate bucket policy from '%s': %s", source_bucket, e
            )
    except Exception:
        logger.warning(
            "Failed to replicate bucket policy from '%s'", source_bucket, exc_info=True
        )


def _replicate_cors_configuration(
    source_bucket: str, target_bucket: str, target_region: str
) -> None:
    """Replicate CORS configuration from source to target bucket.

    Best-effort — failures are logged but don't fail replication.
    """
    source_region = _get_source_region_from_bucket(source_bucket)
    s3_source = create_source_client("s3", source_region) if source_region else create_source_client("s3", target_region)
    s3_target = create_target_client("s3", target_region)

    try:
        resp = s3_source.get_bucket_cors(Bucket=source_bucket)
        cors_rules = resp.get("CORSRules", [])
        if not cors_rules:
            return

        s3_target.put_bucket_cors(
            Bucket=target_bucket,
            CORSConfiguration={"CORSRules": cors_rules},
        )
        logger.info(
            "Replicated %d CORS rule(s) from '%s' to '%s'",
            len(cors_rules), source_bucket, target_bucket,
        )
    except s3_source.exceptions.ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "NoSuchCORSConfiguration":
            logger.debug("No CORS configuration on source bucket '%s'", source_bucket)
        else:
            logger.warning(
                "Failed to replicate CORS from '%s': %s", source_bucket, e
            )
    except Exception:
        logger.warning(
            "Failed to replicate CORS from '%s'", source_bucket, exc_info=True
        )


def _replicate_lifecycle_rules(
    source_bucket: str, target_bucket: str, target_region: str
) -> None:
    """Replicate lifecycle rules from source to target bucket.

    Best-effort — failures are logged but don't fail replication.
    """
    source_region = _get_source_region_from_bucket(source_bucket)
    s3_source = create_source_client("s3", source_region) if source_region else create_source_client("s3", target_region)
    s3_target = create_target_client("s3", target_region)

    try:
        resp = s3_source.get_bucket_lifecycle_configuration(Bucket=source_bucket)
        rules = resp.get("Rules", [])
        if not rules:
            return

        s3_target.put_bucket_lifecycle_configuration(
            Bucket=target_bucket,
            LifecycleConfiguration={"Rules": rules},
        )
        logger.info(
            "Replicated %d lifecycle rule(s) from '%s' to '%s'",
            len(rules), source_bucket, target_bucket,
        )
    except s3_source.exceptions.ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "NoSuchLifecycleConfiguration":
            logger.debug("No lifecycle rules on source bucket '%s'", source_bucket)
        else:
            logger.warning(
                "Failed to replicate lifecycle rules from '%s': %s", source_bucket, e
            )
    except Exception:
        logger.warning(
            "Failed to replicate lifecycle rules from '%s'", source_bucket, exc_info=True
        )


def _get_source_region_from_bucket(bucket_name: str) -> str | None:
    """Try to determine the source region for a bucket.

    Returns None if it can't be determined (caller should use a fallback).
    """
    try:
        # Use a regionless S3 client to get bucket location
        import boto3
        s3 = boto3.client("s3")
        resp = s3.get_bucket_location(Bucket=bucket_name)
        location = resp.get("LocationConstraint")
        # None means us-east-1
        return location or "us-east-1"
    except Exception:
        return None
