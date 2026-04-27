"""Lambda function discovery for Connect ACGR Resource Replicator.

Discovers all Lambda functions associated with a Connect instance, their IAM
execution roles with policies, and any ESM triggers (DynamoDB streams, Kinesis
streams). Returns structured resource models for the inventory.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from aws.client_factory import create_source_client
from aws.arn_utils import parse_arn
from models.enums import ReplicationStatus, ResourceType
from models.resources import IAMRoleResource, LambdaResource

logger = logging.getLogger(__name__)


def _generate_resource_id(arn: str) -> str:
    """Generate a deterministic resource ID from an ARN using a SHA-256 hash prefix."""
    return hashlib.sha256(arn.encode()).hexdigest()[:12]


def _get_lambda_arns(connect_client: Any, instance_id: str) -> list[str]:
    """Call Connect ListLambdaFunctions to get all Lambda ARNs for the instance.

    Handles pagination via NextToken.
    """
    lambda_arns: list[str] = []
    params: dict[str, Any] = {"InstanceId": instance_id}

    while True:
        response = connect_client.list_lambda_functions(**params)
        lambda_arns.extend(response.get("LambdaFunctions", []))
        next_token = response.get("NextToken")
        if not next_token:
            break
        params["NextToken"] = next_token

    return lambda_arns


def _get_function_config(lambda_client: Any, function_arn: str) -> dict[str, Any] | None:
    """Call Lambda GetFunction to retrieve full function configuration.

    Returns None if the function cannot be retrieved.
    """
    try:
        response = lambda_client.get_function(FunctionName=function_arn)
        return response
    except Exception:
        logger.exception("Failed to get Lambda function: %s", function_arn)
        return None



def _get_iam_role_details(
    iam_client: Any, role_arn: str
) -> tuple[dict[str, Any] | None, list[dict], list[dict]]:
    """Retrieve IAM role details including attached and inline policies.

    Returns:
        A tuple of (role_response, attached_policies, inline_policies).
        role_response is None if the role cannot be retrieved.
    """
    role_name = role_arn.rsplit("/", 1)[-1]

    try:
        role_response = iam_client.get_role(RoleName=role_name)
    except Exception:
        logger.exception("Failed to get IAM role: %s", role_arn)
        return None, [], []

    # Get attached managed policies
    attached_policies: list[dict] = []
    try:
        paginator = iam_client.get_paginator("list_attached_role_policies")
        for page in paginator.paginate(RoleName=role_name):
            attached_policies.extend(page.get("AttachedPolicies", []))
    except Exception:
        logger.exception("Failed to list attached policies for role: %s", role_name)

    # Get inline policies
    inline_policies: list[dict] = []
    try:
        inline_names: list[str] = []
        paginator = iam_client.get_paginator("list_role_policies")
        for page in paginator.paginate(RoleName=role_name):
            inline_names.extend(page.get("PolicyNames", []))

        for policy_name in inline_names:
            try:
                policy_resp = iam_client.get_role_policy(
                    RoleName=role_name, PolicyName=policy_name
                )
                inline_policies.append({
                    "PolicyName": policy_name,
                    "PolicyDocument": policy_resp.get("PolicyDocument", {}),
                })
            except Exception:
                logger.exception(
                    "Failed to get inline policy %s for role %s", policy_name, role_name
                )
    except Exception:
        logger.exception("Failed to list inline policies for role: %s", role_name)

    return role_response, attached_policies, inline_policies


def _get_event_source_mappings(lambda_client: Any, function_arn: str) -> list[dict[str, Any]]:
    """Call Lambda ListEventSourceMappings to discover all ESM triggers for a function.

    Handles pagination via NextMarker.
    """
    mappings: list[dict[str, Any]] = []
    params: dict[str, Any] = {"FunctionName": function_arn}

    try:
        while True:
            response = lambda_client.list_event_source_mappings(**params)
            mappings.extend(response.get("EventSourceMappings", []))
            next_marker = response.get("NextMarker")
            if not next_marker:
                break
            params["Marker"] = next_marker
    except Exception:
        logger.exception("Failed to list event source mappings for: %s", function_arn)

    return mappings


def _extract_dynamodb_table_arn_from_stream(event_source_arn: str) -> str | None:
    """Extract the DynamoDB table ARN from a DynamoDB stream ARN.

    DynamoDB stream ARN format:
        arn:aws:dynamodb:<region>:<account>:table/<table-name>/stream/<timestamp>

    Returns the table ARN (without the /stream/ suffix), or None if not a DynamoDB stream.
    """
    try:
        parsed = parse_arn(event_source_arn)
    except ValueError:
        return None

    if parsed["service"] != "dynamodb":
        return None

    resource = parsed["resource"]
    # Match table/<name>/stream/<timestamp>
    match = re.match(r"^(table/[^/]+)/stream/.+$", resource)
    if not match:
        return None

    table_resource = match.group(1)
    return f"arn:{parsed['partition']}:dynamodb:{parsed['region']}:{parsed['account']}:{table_resource}"


def _extract_kinesis_stream_arn(event_source_arn: str) -> str | None:
    """Check if an event source ARN is a Kinesis Data Stream and return it.

    Kinesis stream ARN format:
        arn:aws:kinesis:<region>:<account>:stream/<stream-name>

    Returns the ARN if it's a Kinesis stream, None otherwise.
    """
    try:
        parsed = parse_arn(event_source_arn)
    except ValueError:
        return None

    if parsed["service"] != "kinesis":
        return None

    if parsed["resource"].startswith("stream/"):
        return event_source_arn

    return None



def _build_lambda_resource(
    function_arn: str,
    func_response: dict[str, Any],
    esm_triggers: list[dict[str, Any]],
    role_resource_id: str,
) -> LambdaResource:
    """Build a LambdaResource from GetFunction response and ESM data."""
    config = func_response.get("Configuration", {})
    code = func_response.get("Code", {})

    runtime = config.get("Runtime", "")
    handler = config.get("Handler", "")
    memory_size = config.get("MemorySize", 128)
    timeout = config.get("Timeout", 3)
    environment = config.get("Environment", {}).get("Variables", {})
    layers = [layer.get("Arn", "") for layer in config.get("Layers", [])]
    vpc_config = config.get("VpcConfig")
    execution_role_arn = config.get("Role", "")
    function_name = config.get("FunctionName", "")

    # Clean up VPC config — remove empty values
    if vpc_config and not vpc_config.get("SubnetIds"):
        vpc_config = None

    # Build config summary for display
    config_summary = {
        "runtime": runtime,
        "handler": handler,
        "memory": f"{memory_size} MB",
        "timeout": f"{timeout}s",
    }
    if layers:
        config_summary["layers"] = str(len(layers))
    if vpc_config:
        config_summary["vpc"] = "Yes"
    if esm_triggers:
        config_summary["esm_triggers"] = str(len(esm_triggers))

    # Serialize ESM triggers for storage
    esm_data = []
    for mapping in esm_triggers:
        esm_data.append({
            "UUID": mapping.get("UUID", ""),
            "EventSourceArn": mapping.get("EventSourceArn", ""),
            "BatchSize": mapping.get("BatchSize"),
            "StartingPosition": mapping.get("StartingPosition"),
            "State": mapping.get("State", ""),
            "MaximumBatchingWindowInSeconds": mapping.get("MaximumBatchingWindowInSeconds"),
        })

    resource_id = _generate_resource_id(function_arn)

    return LambdaResource(
        id=resource_id,
        name=function_name,
        arn=function_arn,
        runtime=runtime,
        handler=handler,
        memory_size=memory_size,
        timeout=timeout,
        environment=environment,
        layers=layers,
        vpc_config=vpc_config,
        execution_role_arn=execution_role_arn,
        esm_triggers=esm_data,
        config_summary=config_summary,
        dependencies=[role_resource_id],
    )


def _build_iam_role_resource(
    role_arn: str,
    role_response: dict[str, Any],
    attached_policies: list[dict],
    inline_policies: list[dict],
) -> IAMRoleResource:
    """Build an IAMRoleResource from GetRole response and policy data."""
    role = role_response.get("Role", {})
    role_name = role.get("RoleName", "")
    assume_role_policy = role.get("AssumeRolePolicyDocument", {})

    config_summary = {
        "attached_policies": str(len(attached_policies)),
        "inline_policies": str(len(inline_policies)),
    }

    resource_id = _generate_resource_id(role_arn)

    return IAMRoleResource(
        id=resource_id,
        name=role_name,
        arn=role_arn,
        assume_role_policy=assume_role_policy,
        attached_policies=attached_policies,
        inline_policies=inline_policies,
        config_summary=config_summary,
    )


def discover_lambda_functions(
    instance_id: str, source_region: str
) -> tuple[list[LambdaResource], list[IAMRoleResource], list[str], list[str]]:
    """Discover all Lambda functions associated with a Connect instance.

    This function:
    1. Calls Connect ListLambdaFunctions to get all Lambda ARNs
    2. For each Lambda, calls GetFunction to get full configuration
    3. For each Lambda, retrieves the IAM execution role and all policies
    4. For each Lambda, calls ListEventSourceMappings to discover ESM triggers
    5. Extracts DynamoDB table ARNs from ESM triggers referencing DynamoDB streams
    6. Extracts Kinesis stream ARNs from ESM triggers referencing Kinesis

    Args:
        instance_id: The Connect instance ID.
        source_region: The AWS region of the Connect instance.

    Returns:
        A tuple of:
            - lambda_resources: List of discovered LambdaResource objects
            - iam_role_resources: List of discovered IAMRoleResource objects
            - dynamodb_table_arns: List of DynamoDB table ARNs found in ESM triggers
            - kinesis_stream_arns: List of Kinesis stream ARNs found in ESM triggers
    """
    connect_client = create_source_client("connect", source_region)
    lambda_client = create_source_client("lambda", source_region)
    iam_client = create_source_client("iam", source_region)

    # Step 1: Get all Lambda ARNs from Connect
    lambda_arns = _get_lambda_arns(connect_client, instance_id)
    logger.info("Found %d Lambda functions for instance %s", len(lambda_arns), instance_id)

    lambda_resources: list[LambdaResource] = []
    iam_role_resources: list[IAMRoleResource] = []
    dynamodb_table_arns: list[str] = []
    kinesis_stream_arns: list[str] = []

    # Track seen role ARNs to avoid duplicates
    seen_role_arns: set[str] = set()
    seen_dynamodb_arns: set[str] = set()
    seen_kinesis_arns: set[str] = set()

    for function_arn in lambda_arns:
        # Step 2: Get full function configuration
        func_response = _get_function_config(lambda_client, function_arn)
        if func_response is None:
            logger.warning("Skipping Lambda function (could not retrieve): %s", function_arn)
            continue

        config = func_response.get("Configuration", {})
        role_arn = config.get("Role", "")

        # Step 3: Get IAM role and policies
        role_resource_id = ""
        if role_arn and role_arn not in seen_role_arns:
            seen_role_arns.add(role_arn)
            role_response, attached_policies, inline_policies = _get_iam_role_details(
                iam_client, role_arn
            )
            if role_response is not None:
                iam_resource = _build_iam_role_resource(
                    role_arn, role_response, attached_policies, inline_policies
                )
                iam_role_resources.append(iam_resource)
                role_resource_id = iam_resource.id
        elif role_arn in seen_role_arns:
            # Reuse the existing role resource ID
            role_resource_id = _generate_resource_id(role_arn)

        # Step 4: Get ESM triggers
        esm_mappings = _get_event_source_mappings(lambda_client, function_arn)

        # Steps 5 & 6: Extract DynamoDB and Kinesis ARNs from ESM triggers
        for mapping in esm_mappings:
            event_source_arn = mapping.get("EventSourceArn", "")

            # Check for DynamoDB stream
            table_arn = _extract_dynamodb_table_arn_from_stream(event_source_arn)
            if table_arn and table_arn not in seen_dynamodb_arns:
                seen_dynamodb_arns.add(table_arn)
                dynamodb_table_arns.append(table_arn)

            # Check for Kinesis stream
            kinesis_arn = _extract_kinesis_stream_arn(event_source_arn)
            if kinesis_arn and kinesis_arn not in seen_kinesis_arns:
                seen_kinesis_arns.add(kinesis_arn)
                kinesis_stream_arns.append(kinesis_arn)

        # Build the Lambda resource
        lambda_resource = _build_lambda_resource(
            function_arn, func_response, esm_mappings, role_resource_id
        )
        lambda_resources.append(lambda_resource)

    logger.info(
        "Lambda discovery complete: %d functions, %d IAM roles, %d DynamoDB tables, %d Kinesis streams",
        len(lambda_resources),
        len(iam_role_resources),
        len(dynamodb_table_arns),
        len(kinesis_stream_arns),
    )

    return lambda_resources, iam_role_resources, dynamodb_table_arns, kinesis_stream_arns


def discover_single_lambda(
    function_arn: str, source_region: str
) -> list:
    """Discover a single Lambda function by ARN using lambda:GetFunction.

    Used for fulfillment Lambdas and KVS consumer Lambdas that weren't
    found via Connect ListLambdaFunctions.

    Returns:
        A list of ResourceBase objects (Lambda + IAM role if found).
    """
    lambda_client = create_source_client("lambda", source_region)
    iam_client = create_source_client("iam", source_region)

    resources = []

    func_response = _get_function_config(lambda_client, function_arn)
    if func_response is None:
        logger.warning("Could not retrieve Lambda function: %s", function_arn)
        return resources

    config = func_response.get("Configuration", {})
    role_arn = config.get("Role", "")

    # Discover IAM role
    if role_arn:
        role_response, attached_policies, inline_policies = _get_iam_role_details(
            iam_client, role_arn
        )
        if role_response:
            role_resource = _build_iam_role_resource(
                role_arn, role_response, attached_policies, inline_policies
            )
            resources.append(role_resource)

    # Build Lambda resource — pass full func_response (not config) because
    # _build_lambda_resource expects the top-level GetFunction response with
    # a "Configuration" key inside it.
    esm_triggers = _get_event_source_mappings(lambda_client, function_arn)
    lambda_resource = _build_lambda_resource(
        function_arn, func_response, esm_triggers, _generate_resource_id(role_arn) if role_arn else ""
    )
    resources.append(lambda_resource)

    return resources
