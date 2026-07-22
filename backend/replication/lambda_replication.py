"""Lambda function replication to the ACGR target region.

Creates an equivalent Lambda function in the target region with:
- Same function name, runtime, handler, memory size, timeout
- Downloaded and re-uploaded code package
- Environment variables with ARNs rewritten to the target region
- Execution role mapped to the replicated role ARN
- Layer ARNs rewritten to the target region
- Event Source Mappings recreated for replicated trigger resources

Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8
"""

from __future__ import annotations

import logging
from io import BytesIO

import requests
from botocore.exceptions import ClientError

from aws.arn_utils import parse_arn, rewrite_arn
from aws.client_factory import create_source_client, create_target_client
from models.resources import LambdaResource

logger = logging.getLogger(__name__)

# Mapping of deprecated runtimes to their supported replacements
_DEPRECATED_RUNTIME_UPGRADES = {
    "python2.7": "python3.12",
    "python3.6": "python3.12",
    "python3.7": "python3.12",
    "python3.8": "python3.12",
    "nodejs10.x": "nodejs20.x",
    "nodejs12.x": "nodejs20.x",
    "nodejs14.x": "nodejs20.x",
    "nodejs16.x": "nodejs20.x",
    "dotnetcore2.1": "dotnet8",
    "dotnetcore3.1": "dotnet8",
    "ruby2.5": "ruby3.3",
    "ruby2.7": "ruby3.3",
    "java8": "java21",
}


def _upgrade_runtime(runtime: str) -> str:
    """Upgrade a deprecated Lambda runtime to a supported version.

    Returns the original runtime if it is still supported.
    """
    upgraded = _DEPRECATED_RUNTIME_UPGRADES.get(runtime)
    if upgraded:
        logger.warning(
            "Runtime '%s' is deprecated; upgrading to '%s' for replication",
            runtime, upgraded,
        )
        return upgraded
    return runtime


def _rewrite_env_vars(
    environment: dict[str, str], target_region: str, source_region: str = ""
) -> dict[str, str]:
    """Rewrite ARN values and region strings in environment variables.

    Any value that starts with ``arn:`` is rewritten using :func:`rewrite_arn`.
    Non-ARN values that contain the source region string are also rewritten
    (e.g. endpoint URLs, bucket names with region in them).
    """
    rewritten: dict[str, str] = {}
    for key, value in environment.items():
        if value.startswith("arn:"):
            try:
                rewritten[key] = rewrite_arn(value, target_region)
            except ValueError:
                rewritten[key] = value
        elif source_region and source_region in value:
            rewritten[key] = value.replace(source_region, target_region)
        else:
            rewritten[key] = value
    return rewritten


def _rewrite_layer_arns(
    layers: list[str],
    target_region: str,
    layer_arn_mapping: dict[str, str] | None = None,
    owner_account: str | None = None,
) -> list[str]:
    """Rewrite layer ARNs to the target region using the layer mapping if available.

    Resolution order for each layer:
    1. If we successfully replicated the layer, use its mapped target ARN.
    2. If the layer is owned by a *different* AWS account than the function
       (e.g. AWS-managed extension layers such as ``aws-fis-extension`` owned by
       an AWS service account, or any third-party layer), it cannot be
       republished into the target region and our account has no access to it
       there. Attaching a region-rewritten ARN would make ``CreateFunction``
       fail with a ``GetLayerVersion`` AccessDenied. Such layers are therefore
       **dropped** (skipped) with a warning rather than failing the whole
       function replication.
    3. Otherwise (same-account layer, no mapping) fall back to region rewriting.

    Args:
        layers: Source layer version ARNs attached to the function.
        target_region: The ACGR target region.
        layer_arn_mapping: Source layer ARN → replicated target ARN.
        owner_account: The account that owns the function being replicated. When
            provided, unmapped layers owned by a different account are dropped.
    """
    if not layer_arn_mapping:
        layer_arn_mapping = {}

    rewritten: list[str] = []
    for layer_arn in layers:
        # Check if we have a replicated mapping for this layer
        if layer_arn in layer_arn_mapping:
            rewritten.append(layer_arn_mapping[layer_arn])
            continue

        # Drop cross-account layers we could not replicate — they are not
        # accessible to this account in the target region and would cause
        # CreateFunction to fail. AWS-managed extension layers (e.g.
        # aws-fis-extension) are the common case.
        try:
            layer_owner = parse_arn(layer_arn)["account"]
        except ValueError:
            layer_owner = None
        if owner_account and layer_owner and layer_owner != owner_account:
            logger.warning(
                "Dropping cross-account layer '%s' (owner %s != function owner %s); "
                "it cannot be replicated or accessed in %s",
                layer_arn, layer_owner, owner_account, target_region,
            )
            continue

        # Fall back to region rewriting for same-account (or unknown-owner) layers
        try:
            rewritten.append(rewrite_arn(layer_arn, target_region))
        except ValueError:
            logger.warning("Could not rewrite layer ARN '%s'; keeping as-is", layer_arn)
            rewritten.append(layer_arn)
    return rewritten


def replicate_lambda_layers(
    lambda_resources: list,
    source_region: str,
    target_region: str,
) -> dict[str, str]:
    """Replicate Lambda layers used by the given functions to the target region.

    For each unique layer version ARN across all functions:
    1. Call GetLayerVersion to get the layer code download URL and metadata
    2. Download the layer code
    3. Call PublishLayerVersion in the target region with the same config
    4. Build a mapping of source layer ARN → target layer ARN

    Args:
        lambda_resources: List of LambdaResource objects with layer ARNs.
        source_region: The source AWS region.
        target_region: The target AWS region.

    Returns:
        A dict mapping source layer version ARN → target layer version ARN.
    """
    # Collect unique layer ARNs across all functions
    unique_layers: dict[str, str] = {}  # layer_version_arn -> layer_name
    for resource in lambda_resources:
        for layer_arn in getattr(resource, "layers", []):
            if layer_arn and layer_arn not in unique_layers:
                # Extract layer name from ARN
                # Format: arn:aws:lambda:region:account:layer:name:version
                parts = layer_arn.split(":")
                if len(parts) >= 8:
                    unique_layers[layer_arn] = parts[6]  # layer name

    if not unique_layers:
        return {}

    logger.info("Replicating %d unique Lambda layer(s) to %s", len(unique_layers), target_region)

    source_lambda = create_source_client("lambda", source_region)
    target_lambda = create_target_client("lambda", target_region)
    layer_arn_mapping: dict[str, str] = {}

    for source_layer_arn, layer_name in unique_layers.items():
        try:
            mapped_arn = _replicate_single_layer(
                source_lambda, target_lambda, source_layer_arn, layer_name, target_region
            )
            if mapped_arn:
                layer_arn_mapping[source_layer_arn] = mapped_arn
                logger.info("Replicated layer '%s' → %s", layer_name, mapped_arn)
        except Exception:
            logger.warning(
                "Failed to replicate layer '%s' (%s); will fall back to region rewriting",
                layer_name, source_layer_arn, exc_info=True,
            )

    logger.info(
        "Layer replication complete: %d/%d succeeded",
        len(layer_arn_mapping), len(unique_layers),
    )
    return layer_arn_mapping


def _replicate_single_layer(
    source_lambda,
    target_lambda,
    source_layer_arn: str,
    layer_name: str,
    target_region: str,
) -> str | None:
    """Replicate a single AWS Lambda layer version to the target region.

    Returns the target layer version ARN, or None on failure.
    """
    # Parse version from ARN
    parts = source_layer_arn.split(":")
    if len(parts) < 8:
        logger.warning("Invalid layer ARN format: %s", source_layer_arn)
        return None

    version = int(parts[7])

    # Get layer version details from source
    try:
        resp = source_lambda.get_layer_version(
            LayerName=layer_name, VersionNumber=version
        )
    except Exception:
        logger.warning("Failed to get layer version for '%s' v%d", layer_name, version)
        return None

    code_url = resp.get("Content", {}).get("Location")
    if not code_url:
        logger.warning("No code location for layer '%s' v%d", layer_name, version)
        return None

    compatible_runtimes = resp.get("CompatibleRuntimes", [])
    compatible_architectures = resp.get("CompatibleArchitectures", [])
    description = resp.get("Description", "")
    license_info = resp.get("LicenseInfo", "")

    # Download layer code
    try:
        code_resp = requests.get(code_url, timeout=300)
        code_resp.raise_for_status()
        layer_code = code_resp.content
    except Exception:
        logger.warning("Failed to download layer code for '%s'", layer_name)
        return None

    # Check if layer already exists in target region with same name
    # (we publish a new version regardless — layers are immutable versions)
    publish_params: dict = {
        "LayerName": layer_name,
        "Content": {"ZipFile": layer_code},
        "Description": description or f"Replicated from {source_layer_arn}",
    }
    if compatible_runtimes:
        publish_params["CompatibleRuntimes"] = compatible_runtimes
    if compatible_architectures:
        publish_params["CompatibleArchitectures"] = compatible_architectures
    if license_info:
        publish_params["LicenseInfo"] = license_info

    try:
        publish_resp = target_lambda.publish_layer_version(**publish_params)
        return publish_resp.get("LayerVersionArn", "")
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        logger.warning(
            "Failed to publish layer '%s' in %s: [%s] %s",
            layer_name, target_region, error_code, exc.response["Error"]["Message"],
        )
        return None


def _download_code_package(lambda_resource: LambdaResource, source_region: str) -> bytes:
    """Download the AWS Lambda function code package from the source region.

    Uses Lambda GetFunction to obtain a pre-signed URL for the code, then
    downloads the zip package.

    Returns:
        The raw bytes of the deployment package zip.

    Raises:
        RuntimeError: If the code download fails.
    """
    lambda_client = create_source_client("lambda", source_region)
    try:
        response = lambda_client.get_function(FunctionName=lambda_resource.name)
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        error_msg = exc.response["Error"]["Message"]
        raise RuntimeError(
            f"Failed to get function '{lambda_resource.name}' for code download: "
            f"[{error_code}] {error_msg}"
        ) from exc

    code_location = response.get("Code", {}).get("Location")
    if not code_location:
        raise RuntimeError(
            f"No code location returned for function '{lambda_resource.name}'"
        )

    try:
        resp = requests.get(code_location, timeout=300)
        resp.raise_for_status()
        return resp.content
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Failed to download code package for '{lambda_resource.name}': {exc}"
        ) from exc


def _create_function(
    lambda_client,
    lambda_resource: LambdaResource,
    target_region: str,
    role_arn_mapping: dict[str, str],
    code_bytes: bytes,
    name_override: str = "",
    source_region: str = "",
    layer_arn_mapping: dict[str, str] | None = None,
) -> str:
    """Create the Lambda function in the target region.

    For code packages >50MB, uploads to a staging S3 bucket first and
    uses S3 reference instead of inline ZipFile.

    Args:
        lambda_client: boto3 Lambda client for the target region.
        lambda_resource: The source Lambda resource to replicate.
        target_region: The ACGR target region.
        role_arn_mapping: Mapping of source role ARN → target role ARN.
        code_bytes: The deployment package bytes.
        source_region: The source region (for env var region rewriting).

    Returns:
        The ARN of the function (newly created or existing).

    Raises:
        PermissionError: If access is denied.
        RuntimeError: For other AWS errors.
    """
    # Map execution role to replicated role
    target_role_arn = role_arn_mapping.get(
        lambda_resource.execution_role_arn, lambda_resource.execution_role_arn
    )

    # Rewrite environment variable ARNs and region strings
    rewritten_env = _rewrite_env_vars(
        lambda_resource.environment, target_region, source_region=source_region
    )

    # Rewrite layer ARNs. Pass the function's owner account so that
    # un-replicable cross-account layers (e.g. the AWS FIS extension) are
    # dropped rather than attached, which would fail CreateFunction.
    try:
        function_owner_account = parse_arn(lambda_resource.arn)["account"]
    except ValueError:
        function_owner_account = None
    rewritten_layers = _rewrite_layer_arns(
        lambda_resource.layers,
        target_region,
        layer_arn_mapping=layer_arn_mapping,
        owner_account=function_owner_account,
    )

    # Determine code parameter — use S3 staging for packages >50MB
    code_param: dict
    if len(code_bytes) > 50_000_000:
        s3_bucket, s3_key = _upload_to_staging_bucket(
            code_bytes, target_region, lambda_resource.name, name_override
        )
        code_param = {"S3Bucket": s3_bucket, "S3Key": s3_key}
        logger.info(
            "Code package for '%s' is %dMB — uploaded to S3 staging bucket %s/%s",
            lambda_resource.name, len(code_bytes) // (1024 * 1024), s3_bucket, s3_key,
        )
    else:
        code_param = {"ZipFile": code_bytes}

    create_params: dict = {
        "FunctionName": name_override or lambda_resource.name,
        "Runtime": _upgrade_runtime(lambda_resource.runtime),
        "Role": target_role_arn,
        "Handler": lambda_resource.handler,
        "Code": code_param,
        "Description": f"Replicated from {lambda_resource.arn}",
        "Timeout": lambda_resource.timeout,
        "MemorySize": lambda_resource.memory_size,
    }

    if rewritten_env:
        create_params["Environment"] = {"Variables": rewritten_env}

    if rewritten_layers:
        create_params["Layers"] = rewritten_layers

    try:
        response = lambda_client.create_function(**create_params)
        return response["FunctionArn"]
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        error_msg = exc.response["Error"]["Message"]

        if error_code == "ResourceConflictException":
            func_name = name_override or lambda_resource.name
            logger.info(
                "Lambda function '%s' already exists in target region; reusing",
                func_name,
            )
            return _get_existing_function_arn(lambda_client, func_name)
        if error_code in ("AccessDeniedException", "AccessDenied"):
            raise PermissionError(
                f"Permission denied creating Lambda function "
                f"'{name_override or lambda_resource.name}': {error_msg}"
            ) from exc
        raise RuntimeError(
            f"Failed to create Lambda function '{name_override or lambda_resource.name}': "
            f"[{error_code}] {error_msg}"
        ) from exc


def _upload_to_staging_bucket(
    code_bytes: bytes, target_region: str, func_name: str, name_override: str = ""
) -> tuple[str, str]:
    """Upload Lambda code to a staging S3 bucket for >50MB packages.

    Creates the staging bucket if it doesn't exist. The bucket has:
    - SSE-S3 encryption
    - Public access blocked
    - 7-day lifecycle rule to auto-delete objects

    Returns:
        Tuple of (bucket_name, s3_key).
    """
    import boto3 as _boto3
    import uuid as _uuid

    # Determine account ID for bucket naming
    sts = _boto3.client("sts")
    account_id = sts.get_caller_identity()["Account"]
    bucket_name = f"connect-idr-lambda-staging-{account_id}-{target_region}"

    s3_client = create_target_client("s3", target_region)

    # Create bucket if it doesn't exist
    try:
        s3_client.head_bucket(Bucket=bucket_name)
    except Exception:
        create_params: dict = {"Bucket": bucket_name}
        if target_region != "us-east-1":
            create_params["CreateBucketConfiguration"] = {
                "LocationConstraint": target_region
            }
        try:
            s3_client.create_bucket(**create_params)
            logger.info("Created Lambda staging bucket: %s", bucket_name)

            # Enable SSE-S3 encryption
            s3_client.put_bucket_encryption(
                Bucket=bucket_name,
                ServerSideEncryptionConfiguration={
                    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
                },
            )

            # Block public access
            s3_client.put_public_access_block(
                Bucket=bucket_name,
                PublicAccessBlockConfiguration={
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                },
            )

            # 7-day lifecycle rule
            s3_client.put_bucket_lifecycle_configuration(
                Bucket=bucket_name,
                LifecycleConfiguration={
                    "Rules": [
                        {
                            "ID": "auto-cleanup",
                            "Status": "Enabled",
                            "Expiration": {"Days": 7},
                            "Filter": {"Prefix": ""},
                        }
                    ]
                },
            )
        except Exception:
            logger.debug("Staging bucket creation may have raced; continuing")

    # Upload the code
    key = f"lambda-code/{name_override or func_name}/{_uuid.uuid4().hex}.zip"
    s3_client.put_object(Bucket=bucket_name, Key=key, Body=code_bytes)
    logger.info("Uploaded %dMB to s3://%s/%s", len(code_bytes) // (1024 * 1024), bucket_name, key)

    return bucket_name, key


def _get_existing_function_arn(lambda_client, function_name: str) -> str:
    """Fetch the ARN of an existing Lambda function by name.

    Raises:
        RuntimeError: If the function cannot be retrieved.
    """
    try:
        response = lambda_client.get_function(FunctionName=function_name)
        return response["Configuration"]["FunctionArn"]
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        error_msg = exc.response["Error"]["Message"]
        raise RuntimeError(
            f"Function '{function_name}' exists but failed to retrieve ARN: "
            f"[{error_code}] {error_msg}"
        ) from exc


def _create_event_source_mappings(
    lambda_client,
    lambda_resource: LambdaResource,
    target_function_arn: str,
    target_region: str,
) -> list[dict]:
    """Create Event Source Mappings for the replicated function.

    Rewrites the event source ARN to the target region and creates each ESM
    pointing to the new function.

    Args:
        lambda_client: boto3 Lambda client for the target region.
        lambda_resource: The source Lambda resource with ESM triggers.
        target_function_arn: The ARN of the newly created function.
        target_region: The ACGR target region.

    Returns:
        A list of dicts with ``UUID`` and ``EventSourceArn`` for each created ESM.
    """
    created_esms: list[dict] = []

    for esm in lambda_resource.esm_triggers:
        source_arn = esm.get("EventSourceArn", "")
        if not source_arn:
            logger.warning("ESM trigger missing EventSourceArn; skipping")
            continue

        try:
            target_source_arn = rewrite_arn(source_arn, target_region)
        except ValueError:
            logger.warning(
                "Could not rewrite ESM source ARN '%s'; skipping", source_arn
            )
            continue

        esm_params: dict = {
            "EventSourceArn": target_source_arn,
            "FunctionName": target_function_arn,
            "Enabled": esm.get("State", "Enabled") == "Enabled",
        }

        # Carry over batch size if present
        if "BatchSize" in esm:
            esm_params["BatchSize"] = esm["BatchSize"]

        # Carry over starting position if present
        if "StartingPosition" in esm:
            esm_params["StartingPosition"] = esm["StartingPosition"]

        try:
            response = lambda_client.create_event_source_mapping(**esm_params)
            created_esms.append({
                "UUID": response.get("UUID", ""),
                "EventSourceArn": target_source_arn,
            })
            logger.debug(
                "Created ESM for function '%s' → %s",
                lambda_resource.name,
                target_source_arn,
            )
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            error_msg = exc.response["Error"]["Message"]
            logger.warning(
                "Failed to create ESM for '%s' (source: %s): [%s] %s",
                lambda_resource.name,
                target_source_arn,
                error_code,
                error_msg,
            )

    return created_esms


def replicate_lambda_function(
    lambda_resource: LambdaResource,
    target_region: str,
    role_arn_mapping: dict[str, str] | None = None,
    resource_tags: dict[str, str] | None = None,
    instance_id: str = "",
    layer_arn_mapping: dict[str, str] | None = None,
) -> str:
    """Replicate a Lambda function to the ACGR target region.

    Performs the following steps:
    1. Download the code package from the source region.
    2. Create the function in the target region with:
       - Same name, runtime, handler, memory, timeout
       - Environment variables with ARNs and region strings rewritten
       - Execution role mapped to the replicated role
       - Layer ARNs rewritten to the target region
       - Code uploaded via S3 staging bucket if >50MB
    3. Create Event Source Mappings for any ESM triggers.
    4. Associate the function with the replica Connect instance (best-effort).

    Args:
        lambda_resource: The discovered Lambda function resource.
        target_region: The ACGR target region.
        role_arn_mapping: Optional mapping of source role ARN → target role ARN.
        instance_id: Connect instance ID for post-replication association.

    Returns:
        The ARN of the Lambda function in the target region (newly created or existing).

    Raises:
        PermissionError: If the caller lacks Lambda permissions.
        RuntimeError: For any other AWS API error during replication.
    """
    if role_arn_mapping is None:
        role_arn_mapping = {}

    source_region = lambda_resource.arn.split(":")[3]

    # Step 1: Download code package
    logger.info("Downloading code for Lambda function '%s'", lambda_resource.name)
    code_bytes = _download_code_package(lambda_resource, source_region)

    # Step 2: Create function in target region
    lambda_client = create_target_client("lambda", target_region)
    # Lex codehook/fulfillment Lambdas keep the original name (same as
    # Connect-associated Lambdas) so the Amazon Lex bot can reference them by
    # the same function name in the target region.
    target_name = lambda_resource.name
    logger.info(
        "Creating Lambda function '%s' in %s (%dMB code)",
        target_name, target_region, len(code_bytes) // (1024 * 1024),
    )
    function_arn = _create_function(
        lambda_client, lambda_resource, target_region, role_arn_mapping, code_bytes,
        name_override=target_name, source_region=source_region,
        layer_arn_mapping=layer_arn_mapping,
    )

    # Step 2b: Apply tags
    if resource_tags:
        try:
            lambda_client.tag_resource(Resource=function_arn, Tags=resource_tags)
            logger.info("Applied %d tag(s) to Lambda function '%s'", len(resource_tags), target_name)
        except Exception:
            logger.warning("Failed to apply tags to Lambda function '%s'", target_name, exc_info=True)

    # Step 3: Create ESM triggers
    if lambda_resource.esm_triggers:
        logger.info(
            "Creating %d ESM trigger(s) for '%s'",
            len(lambda_resource.esm_triggers),
            lambda_resource.name,
        )
        _create_event_source_mappings(
            lambda_client, lambda_resource, function_arn, target_region
        )

    # Step 4: Associate with replica Connect instance (best-effort)
    # Lex codehook Lambdas are NOT associated with Connect — they are
    # invoked by the Amazon Lex bot, not by Connect contact flows directly.
    if instance_id and not lambda_resource.is_lex_codehook:
        _associate_lambda_with_connect(function_arn, instance_id, target_region)

    logger.info(
        "Successfully replicated Lambda function '%s' → %s",
        lambda_resource.name,
        function_arn,
    )
    return function_arn


def _associate_lambda_with_connect(
    function_arn: str, instance_id: str, target_region: str
) -> None:
    """Associate a Lambda function with a Connect instance (best-effort).

    Calls connect:AssociateLambdaFunction. Failures are logged but don't
    fail the replication.
    """
    try:
        connect_client = create_target_client("connect", target_region)
        connect_client.associate_lambda_function(
            InstanceId=instance_id,
            FunctionArn=function_arn,
        )
        logger.info("Associated Lambda '%s' with Connect instance '%s'", function_arn, instance_id)
    except Exception:
        logger.debug(
            "Could not associate Lambda '%s' with Connect instance '%s' (best-effort)",
            function_arn, instance_id, exc_info=True,
        )
