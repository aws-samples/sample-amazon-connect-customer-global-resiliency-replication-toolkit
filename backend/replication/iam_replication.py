"""IAM role replication to the ACGR target region.

Creates an equivalent IAM role in the target region with:
- The same assume-role policy document
- All inline policies (with ARNs rewritten to the target region)
- All managed policy attachments

Requirements: 10.5
"""

from __future__ import annotations

import json
import logging

from botocore.exceptions import ClientError

from aws.arn_utils import rewrite_arn
from aws.client_factory import create_target_client
from models.resources import IAMRoleResource

logger = logging.getLogger(__name__)


def _rewrite_policy_arns(policy_document: dict, target_region: str) -> dict:
    """Recursively rewrite ARN strings in a policy document to the target region.

    Walks the policy document structure and rewrites any string value that
    looks like an ARN (starts with ``arn:``) using :func:`rewrite_arn`.

    Args:
        policy_document: The IAM policy document (parsed JSON dict).
        target_region: The target region to substitute into ARNs.

    Returns:
        A new policy document dict with region-rewritten ARNs.
    """
    if isinstance(policy_document, str):
        if policy_document.startswith("arn:"):
            try:
                return rewrite_arn(policy_document, target_region)
            except ValueError:
                return policy_document
        return policy_document
    if isinstance(policy_document, list):
        return [_rewrite_policy_arns(item, target_region) for item in policy_document]
    if isinstance(policy_document, dict):
        return {
            key: _rewrite_policy_arns(value, target_region)
            for key, value in policy_document.items()
        }
    return policy_document


def replicate_iam_role(role: IAMRoleResource, target_region: str, resource_tags: dict[str, str] | None = None) -> str:
    """Replicate an IAM role to the target region.

    Performs the following steps:
    1. CreateRole with the original assume-role policy document.
    2. PutRolePolicy for each inline policy (with ARNs rewritten).
    3. AttachRolePolicy for each managed policy attachment.

    Args:
        role: The discovered IAM role resource to replicate.
        target_region: The ACGR target region.

    Returns:
        The ARN of the role in the target region (newly created or existing).

    Raises:
        PermissionError: If the caller lacks IAM permissions.
        RuntimeError: For any other AWS API error during replication.
    """
    iam_client = create_target_client("iam", target_region)

    # IAM roles use the same name (IAM is global)
    target_name = role.name

    # Step 1: Create the role
    role_arn = _create_role(iam_client, role, target_name)

    # Step 2: Put inline policies with ARN rewriting
    _put_inline_policies(iam_client, role, target_region, target_name=target_name)

    # Step 3: Attach managed policies
    _attach_managed_policies(iam_client, role, target_name)

    # Step 4: Apply tags
    if resource_tags:
        try:
            tag_list = [{"Key": k, "Value": v} for k, v in resource_tags.items()]
            iam_client.tag_role(RoleName=target_name, Tags=tag_list)
            logger.info("Applied %d tag(s) to IAM role '%s'", len(tag_list), target_name)
        except Exception:
            logger.warning("Failed to apply tags to IAM role '%s'", target_name, exc_info=True)

    logger.info("Successfully replicated IAM role '%s' → %s", role.name, role_arn)
    return role_arn


def _create_role(iam_client, role: IAMRoleResource, target_name: str = "") -> str:
    """Create the IAM role and return its ARN.

    If the role already exists in the target region, fetches and returns
    the existing role's ARN instead of raising an error.

    Raises:
        PermissionError: If access is denied.
        RuntimeError: For other AWS errors.
    """
    try:
        name = target_name or role.name
        response = iam_client.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(role.assume_role_policy),
            Description=f"Replicated from {role.arn}",
        )
        return response["Role"]["Arn"]
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        error_msg = exc.response["Error"]["Message"]
        name = target_name or role.name

        if error_code == "EntityAlreadyExists":
            logger.info(
                "IAM role '%s' already exists in target region; reusing", name
            )
            return _get_existing_role_arn(iam_client, name)
        if error_code in ("AccessDenied", "UnauthorizedAccess"):
            raise PermissionError(
                f"Permission denied creating IAM role '{name}': {error_msg}"
            ) from exc
        raise RuntimeError(
            f"Failed to create IAM role '{name}': [{error_code}] {error_msg}"
        ) from exc


def _get_existing_role_arn(iam_client, role_name: str) -> str:
    """Fetch the ARN of an existing IAM role by name.

    Raises:
        RuntimeError: If the role cannot be retrieved.
    """
    try:
        response = iam_client.get_role(RoleName=role_name)
        return response["Role"]["Arn"]
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        error_msg = exc.response["Error"]["Message"]
        raise RuntimeError(
            f"Role '{role_name}' exists but failed to retrieve ARN: "
            f"[{error_code}] {error_msg}"
        ) from exc


def _put_inline_policies(
    iam_client, role: IAMRoleResource, target_region: str, target_name: str = ""
) -> None:
    """Put all inline policies on the role, rewriting ARNs to the target region.

    Raises:
        PermissionError: If access is denied.
        RuntimeError: For other AWS errors.
    """
    for policy in role.inline_policies:
        policy_name = policy.get("PolicyName", "")
        policy_document = policy.get("PolicyDocument", {})

        rewritten_doc = _rewrite_policy_arns(policy_document, target_region)

        try:
            iam_client.put_role_policy(
                RoleName=target_name or role.name,
                PolicyName=policy_name,
                PolicyDocument=json.dumps(rewritten_doc),
            )
            logger.debug(
                "Put inline policy '%s' on role '%s'", policy_name, role.name
            )
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            error_msg = exc.response["Error"]["Message"]

            if error_code in ("AccessDenied", "UnauthorizedAccess"):
                raise PermissionError(
                    f"Permission denied putting policy '{policy_name}' on role "
                    f"'{role.name}': {error_msg}"
                ) from exc
            raise RuntimeError(
                f"Failed to put inline policy '{policy_name}' on role "
                f"'{role.name}': [{error_code}] {error_msg}"
            ) from exc


def _attach_managed_policies(iam_client, role: IAMRoleResource, target_name: str = "") -> None:
    """Attach all managed policies to the role.

    Raises:
        PermissionError: If access is denied.
        RuntimeError: For other AWS errors.
    """
    for policy in role.attached_policies:
        policy_arn = policy.get("PolicyArn", "")

        try:
            iam_client.attach_role_policy(
                RoleName=target_name or role.name,
                PolicyArn=policy_arn,
            )
            logger.debug(
                "Attached managed policy '%s' to role '%s'", policy_arn, role.name
            )
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            error_msg = exc.response["Error"]["Message"]

            if error_code in ("AccessDenied", "UnauthorizedAccess"):
                raise PermissionError(
                    f"Permission denied attaching policy '{policy_arn}' to role "
                    f"'{role.name}': {error_msg}"
                ) from exc
            raise RuntimeError(
                f"Failed to attach policy '{policy_arn}' to role "
                f"'{role.name}': [{error_code}] {error_msg}"
            ) from exc
