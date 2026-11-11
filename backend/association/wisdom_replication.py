"""Replicate Amazon Q in Connect (Wisdom) assistants and knowledge bases.

Discovers Wisdom assistants associated with the source Connect instance,
creates matching assistants and knowledge bases in the target region,
and links them to the replica Connect instance.

KMS key handling:
- Source KBs may reference a KMS key in the source region.
- KMS keys are regional and cannot be used cross-region.
- We look for a KMS key with the same alias in the target region.
- If not found, we create a new symmetric KMS key in the target region.
- The mapping is logged so the user can manage keys post-replication.

External integrations (AppIntegrations) are NOT replicated — they require
manual setup in the target region. A message is returned for those.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from botocore.exceptions import ClientError

from aws.client_factory import create_source_client, create_target_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# KMS key cross-region resolution
# ---------------------------------------------------------------------------

def _resolve_or_create_kms_key(
    source_kms_arn: str,
    source_region: str,
    target_region: str,
) -> tuple[str, str]:
    """Resolve or create a KMS key in the target region matching the source key.

    Strategy:
    1. List aliases on the source key to find a friendly alias name.
    2. Look for a key with the same alias in the target region.
    3. If found, return it. If not, create a new symmetric key and alias.

    Returns:
        (target_key_arn, message) — the ARN of the target key and a status message.
    """
    source_kms = create_source_client("kms", source_region)
    target_kms = create_target_client("kms", target_region)

    # Find aliases for the source key
    source_key_id = source_kms_arn.split("/")[-1] if "/" in source_kms_arn else source_kms_arn
    alias_name: str | None = None

    try:
        paginator = source_kms.get_paginator("list_aliases")
        for page in paginator.paginate(KeyId=source_key_id):
            for alias in page.get("Aliases", []):
                name = alias.get("AliasName", "")
                if name and not name.startswith("alias/aws/"):
                    alias_name = name
                    break
            if alias_name:
                break
    except ClientError:
        logger.debug("Could not list aliases for source KMS key %s", source_kms_arn)

    # If no custom alias, generate one from the key ID
    if not alias_name:
        alias_name = f"alias/connect-wisdom-{source_key_id[:8]}"

    # Check if alias exists in target region
    try:
        resp = target_kms.describe_key(KeyId=alias_name)
        target_key_arn = resp["KeyMetadata"]["Arn"]
        return target_key_arn, f"Reused existing KMS key in {target_region} ({alias_name})"
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code != "NotFoundException":
            logger.debug("Error checking target KMS alias %s: %s", alias_name, exc)

    # Create new symmetric key in target region
    try:
        create_resp = target_kms.create_key(
            Description=f"Connect Wisdom replication key (source: {source_kms_arn})",
            KeyUsage="ENCRYPT_DECRYPT",
            KeySpec="SYMMETRIC_DEFAULT",
            Tags=[
                {"TagKey": "CreatedBy", "TagValue": "connect-acgr-replicator"},
                {"TagKey": "SourceKeyArn", "TagValue": source_kms_arn},
            ],
        )
        target_key_arn = create_resp["KeyMetadata"]["Arn"]

        # Create alias
        try:
            target_kms.create_alias(
                AliasName=alias_name,
                TargetKeyId=target_key_arn,
            )
        except ClientError:
            logger.debug("Could not create alias %s (may already exist)", alias_name)

        return target_key_arn, (
            f"Created new KMS key in {target_region}: {target_key_arn} "
            f"(alias: {alias_name})"
        )
    except ClientError as exc:
        logger.warning("Failed to create KMS key in %s: %s", target_region, exc)
        return "", f"Failed to create KMS key in {target_region}: {exc}"


# ---------------------------------------------------------------------------
# Amazon Q in Connect assistant and knowledge base discovery
# ---------------------------------------------------------------------------

def _list_connect_wisdom_assistants(
    source_region: str,
    instance_id: str,
) -> list[dict[str, Any]]:
    """List Wisdom assistants associated with the Connect instance.

    Filters for assistants tagged with AmazonConnectEnabled=True.
    """
    wisdom = create_source_client("wisdom", source_region)
    assistants: list[dict[str, Any]] = []

    try:
        paginator = wisdom.get_paginator("list_assistants")
        for page in paginator.paginate():
            for assistant in page.get("assistantSummaries", []):
                assistant_id = assistant.get("assistantId", "")
                if not assistant_id:
                    continue
                # Check tags for AmazonConnectEnabled
                try:
                    tags_resp = wisdom.list_tags_for_resource(
                        resourceArn=assistant["assistantArn"],
                    )
                    tags = tags_resp.get("tags", {})
                    if tags.get("AmazonConnectEnabled", "").lower() == "true":
                        assistant["tags"] = tags
                        assistants.append(assistant)
                except ClientError:
                    logger.debug(
                        "Could not list tags for assistant %s", assistant_id
                    )
    except ClientError as exc:
        logger.warning("Failed to list Wisdom assistants: %s", exc)

    return assistants


def _list_knowledge_bases_for_assistant(
    source_region: str,
    assistant_id: str,
) -> list[dict[str, Any]]:
    """List knowledge base associations for a Wisdom assistant."""
    wisdom = create_source_client("wisdom", source_region)
    kbs: list[dict[str, Any]] = []

    try:
        paginator = wisdom.get_paginator("list_knowledge_bases")
        for page in paginator.paginate():
            for kb in page.get("knowledgeBaseSummaries", []):
                kbs.append(kb)
    except ClientError as exc:
        logger.warning(
            "Failed to list knowledge bases for assistant %s: %s",
            assistant_id, exc,
        )

    return kbs


# ---------------------------------------------------------------------------
# Create assistant and knowledge bases in target region
# ---------------------------------------------------------------------------

def _create_target_assistant(
    source_assistant: dict[str, Any],
    target_region: str,
) -> tuple[str, str, str]:
    """Create a Amazon Q in Connect assistant in the target region matching the source.

    Returns:
        (target_assistant_id, target_assistant_arn, message)
    """
    wisdom = create_target_client("wisdom", target_region)
    name = source_assistant.get("name", "")
    assistant_type = source_assistant.get("type", "AGENT")
    description = source_assistant.get("description", "")

    # Check if assistant with same name already exists
    try:
        paginator = wisdom.get_paginator("list_assistants")
        for page in paginator.paginate():
            for existing in page.get("assistantSummaries", []):
                if existing.get("name") == name:
                    return (
                        existing["assistantId"],
                        existing["assistantArn"],
                        f"Assistant '{name}' already exists in {target_region}",
                    )
    except ClientError:
        pass

    # Create new assistant
    try:
        params: dict[str, Any] = {
            "name": name,
            "type": assistant_type,
            "tags": {
                "AmazonConnectEnabled": "True",
                "CreatedBy": "connect-acgr-replicator",
                "SourceAssistantId": source_assistant.get("assistantId", ""),
            },
        }
        if description:
            params["description"] = description

        resp = wisdom.create_assistant(**params)
        assistant = resp.get("assistant", {})
        return (
            assistant.get("assistantId", ""),
            assistant.get("assistantArn", ""),
            f"Created assistant '{name}' in {target_region}",
        )
    except ClientError as exc:
        error_msg = str(exc)
        if "conflict" in error_msg.lower():
            # Try to find it again
            try:
                paginator = wisdom.get_paginator("list_assistants")
                for page in paginator.paginate():
                    for existing in page.get("assistantSummaries", []):
                        if existing.get("name") == name:
                            return (
                                existing["assistantId"],
                                existing["assistantArn"],
                                f"Assistant '{name}' already exists in {target_region}",
                            )
            except ClientError:
                pass
        return "", "", f"Failed to create assistant '{name}': {exc}"


def _replicate_knowledge_bases(
    source_assistant_id: str,
    target_assistant_id: str,
    source_region: str,
    target_region: str,
) -> list[dict[str, Any]]:
    """Replicate knowledge bases from source assistant to target assistant.

    - EXTERNAL type with AppIntegrations source: skip, return manual-setup message.
    - QUICK_RESPONSES / MESSAGE_TEMPLATES: skip (auto-created by Connect).
    - Other types: create in target region with KMS key cross-region mapping.
    """
    results: list[dict[str, Any]] = []
    source_kbs = _list_knowledge_bases_for_assistant(source_region, source_assistant_id)

    if not source_kbs:
        results.append({
            "resource": f"wisdom_kb_{source_assistant_id}",
            "resource_type": "WISDOM_KNOWLEDGE_BASE",
            "status": "info",
            "message": "No knowledge bases found for source assistant",
        })
        return results

    target_wisdom = create_target_client("wisdom", target_region)

    # List existing KBs in target to avoid duplicates
    existing_kb_names: set[str] = set()
    try:
        paginator = target_wisdom.get_paginator("list_knowledge_bases")
        for page in paginator.paginate():
            for kb in page.get("knowledgeBaseSummaries", []):
                existing_kb_names.add(kb.get("name", ""))
    except ClientError:
        pass

    for kb in source_kbs:
        kb_name = kb.get("name", "")
        kb_type = kb.get("knowledgeBaseType", "")
        kb_id = kb.get("knowledgeBaseId", "")

        # Skip auto-managed types
        if kb_type in ("QUICK_RESPONSES", "MESSAGE_TEMPLATES"):
            results.append({
                "resource": kb_name,
                "resource_type": "WISDOM_KNOWLEDGE_BASE",
                "status": "skipped",
                "message": f"KB '{kb_name}' (type: {kb_type}) is auto-managed by Connect — skipped",
            })
            continue

        # Get full KB details from source
        source_wisdom = create_source_client("wisdom", source_region)
        try:
            kb_detail = source_wisdom.get_knowledge_base(
                knowledgeBaseId=kb_id,
            ).get("knowledgeBase", {})
        except ClientError as exc:
            results.append({
                "resource": kb_name,
                "resource_type": "WISDOM_KNOWLEDGE_BASE",
                "status": "error",
                "error": f"Failed to get KB details: {exc}",
            })
            continue

        source_config = kb_detail.get("sourceConfiguration", {})

        # Check for AppIntegrations source — requires manual setup
        if source_config.get("appIntegrations"):
            app_integration_arn = (
                source_config["appIntegrations"]
                .get("appIntegrationArn", "unknown")
            )
            results.append({
                "resource": kb_name,
                "resource_type": "WISDOM_KNOWLEDGE_BASE",
                "status": "manual_setup_required",
                "message": (
                    f"KB '{kb_name}' uses AppIntegrations source "
                    f"({app_integration_arn}). External integrations cannot be "
                    f"auto-replicated — please set up the integration manually "
                    f"in {target_region} and associate it with the target assistant."
                ),
            })
            continue

        # Already exists?
        if kb_name in existing_kb_names:
            results.append({
                "resource": kb_name,
                "resource_type": "WISDOM_KNOWLEDGE_BASE",
                "status": "already_exists",
                "message": f"KB '{kb_name}' already exists in {target_region}",
            })
            continue

        # Handle KMS key cross-region
        kms_message = ""
        target_kms_key_id: str | None = None
        server_side_encryption = kb_detail.get("serverSideEncryptionConfiguration", {})
        source_kms_arn = server_side_encryption.get("kmsKeyId", "")
        if source_kms_arn:
            target_kms_arn, kms_message = _resolve_or_create_kms_key(
                source_kms_arn, source_region, target_region,
            )
            if target_kms_arn:
                target_kms_key_id = target_kms_arn

        # Create KB in target region
        try:
            create_params: dict[str, Any] = {
                "knowledgeBaseType": kb_type,
                "name": kb_name,
                "tags": {
                    "CreatedBy": "connect-acgr-replicator",
                    "SourceKnowledgeBaseId": kb_id,
                },
            }
            if kb_detail.get("description"):
                create_params["description"] = kb_detail["description"]
            if target_kms_key_id:
                create_params["serverSideEncryptionConfiguration"] = {
                    "kmsKeyId": target_kms_key_id,
                }

            resp = target_wisdom.create_knowledge_base(**create_params)
            new_kb = resp.get("knowledgeBase", {})
            msg = f"Created KB '{kb_name}' in {target_region}"
            if kms_message:
                msg += f" | KMS: {kms_message}"

            results.append({
                "resource": kb_name,
                "resource_type": "WISDOM_KNOWLEDGE_BASE",
                "status": "created",
                "target_kb_id": new_kb.get("knowledgeBaseId", ""),
                "message": msg,
            })
        except ClientError as exc:
            error_msg = str(exc)
            if "conflict" in error_msg.lower():
                results.append({
                    "resource": kb_name,
                    "resource_type": "WISDOM_KNOWLEDGE_BASE",
                    "status": "already_exists",
                    "message": f"KB '{kb_name}' already exists in {target_region}",
                })
            else:
                results.append({
                    "resource": kb_name,
                    "resource_type": "WISDOM_KNOWLEDGE_BASE",
                    "status": "error",
                    "error": f"Failed to create KB '{kb_name}': {exc}",
                })

    return results


# ---------------------------------------------------------------------------
# Link assistant to Connect instance
# ---------------------------------------------------------------------------

def _link_assistant_to_connect(
    target_assistant_arn: str,
    target_region: str,
    instance_id: str,
) -> dict[str, Any]:
    """Link a Wisdom assistant to the Connect instance via integration association."""
    connect = create_target_client("connect", target_region)

    # Check if already linked
    try:
        paginator = connect.get_paginator("list_integration_associations")
        for page in paginator.paginate(
            InstanceId=instance_id,
            IntegrationType="WISDOM_ASSISTANT",
        ):
            for assoc in page.get("IntegrationAssociationSummaryList", []):
                if assoc.get("IntegrationArn") == target_assistant_arn:
                    return {
                        "resource": "wisdom_connect_link",
                        "resource_type": "WISDOM_INTEGRATION",
                        "status": "already_linked",
                        "message": "Wisdom assistant is already linked to Connect instance",
                    }
    except ClientError:
        pass

    try:
        connect.create_integration_association(
            InstanceId=instance_id,
            IntegrationType="WISDOM_ASSISTANT",
            IntegrationArn=target_assistant_arn,
        )
        return {
            "resource": "wisdom_connect_link",
            "resource_type": "WISDOM_INTEGRATION",
            "status": "linked",
            "message": f"Linked Wisdom assistant to Connect instance in {target_region}",
        }
    except ClientError as exc:
        error_msg = str(exc)
        if "already" in error_msg.lower() or "duplicate" in error_msg.lower():
            return {
                "resource": "wisdom_connect_link",
                "resource_type": "WISDOM_INTEGRATION",
                "status": "already_linked",
                "message": "Wisdom assistant was already linked",
            }
        return {
            "resource": "wisdom_connect_link",
            "resource_type": "WISDOM_INTEGRATION",
            "status": "error",
            "error": f"Failed to link Wisdom assistant: {exc}",
        }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def replicate_wisdom_domains(
    source_region: str,
    target_region: str,
    instance_id: str,
) -> list[dict[str, Any]]:
    """Replicate Wisdom/Q in Connect domains from source to target region.

    Orchestrates:
    1. Discover Connect-enabled Wisdom assistants in source region
    2. For each assistant, create matching assistant in target region
    3. Replicate knowledge bases (with KMS cross-region handling)
    4. Link the target assistant to the Connect instance

    Returns a list of result dicts describing what was done.
    """
    results: list[dict[str, Any]] = []

    # Step 1: Discover source assistants
    source_assistants = _list_connect_wisdom_assistants(source_region, instance_id)

    if not source_assistants:
        results.append({
            "resource": "wisdom_domains",
            "resource_type": "WISDOM_ASSISTANT",
            "status": "info",
            "message": "No Connect-enabled Wisdom assistants found in source region",
        })
        return results

    logger.info(
        "Found %d Connect-enabled Wisdom assistant(s) in %s",
        len(source_assistants), source_region,
    )

    for assistant in source_assistants:
        assistant_name = assistant.get("name", "unknown")
        assistant_id = assistant.get("assistantId", "")

        # Step 2: Create assistant in target region
        target_id, target_arn, create_msg = _create_target_assistant(
            assistant, target_region,
        )
        results.append({
            "resource": assistant_name,
            "resource_type": "WISDOM_ASSISTANT",
            "status": "created" if target_id else "error",
            "message": create_msg,
        })

        if not target_id:
            continue

        # Step 3: Replicate knowledge bases
        kb_results = _replicate_knowledge_bases(
            assistant_id, target_id, source_region, target_region,
        )
        results.extend(kb_results)

        # Step 4: Link assistant to Connect instance
        link_result = _link_assistant_to_connect(
            target_arn, target_region, instance_id,
        )
        results.append(link_result)

    return results
