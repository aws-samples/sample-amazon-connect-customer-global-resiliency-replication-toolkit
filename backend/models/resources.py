"""Pydantic models for AWS resources in the resource inventory."""

from typing import Literal

from pydantic import BaseModel

from .enums import ReplicationStatus, ResourceType


class KmsInfo(BaseModel):
    """KMS handling details for a replicated resource."""
    key_type: Literal["CMK", "AWS_MANAGED", "NONE"]
    key_alias_or_arn: str
    action: Literal["REUSED", "BOOTSTRAPPED", "SKIPPED"]
    message: str


class ResourceBase(BaseModel):
    """Base model for all discoverable/replicable AWS resources."""

    id: str
    name: str
    arn: str
    resource_type: ResourceType
    status: ReplicationStatus = ReplicationStatus.NOT_REPLICATED
    config_summary: dict[str, str] = {}
    dependencies: list[str] = []
    replicated_arn: str | None = None
    error: str | None = None
    error_classification: dict | None = None
    kms_info: dict | None = None





class LambdaResource(ResourceBase):
    """Lambda function resource with full configuration details."""

    resource_type: Literal[ResourceType.LAMBDA] = ResourceType.LAMBDA
    runtime: str
    handler: str
    memory_size: int
    timeout: int
    environment: dict[str, str] = {}
    layers: list[str] = []
    vpc_config: dict | None = None
    execution_role_arn: str
    esm_triggers: list[dict] = []
    is_lex_codehook: bool = False  # True if this Lambda is only used as a Lex codehook/fulfillment (not directly associated with Connect)


class LexBotResource(ResourceBase):
    """Lex V2 bot resource with intents, slots, and locale configuration."""

    resource_type: Literal[ResourceType.LEX_BOT] = ResourceType.LEX_BOT
    bot_id: str
    locales: list[str] = []
    intents: list[dict] = []
    slot_types: list[dict] = []
    fulfillment_lambda_arns: list[str] = []


class KinesisStreamResource(ResourceBase):
    """Kinesis Data Stream resource."""

    resource_type: Literal[ResourceType.KINESIS_STREAM] = ResourceType.KINESIS_STREAM
    shard_count: int
    retention_period: int
    encryption_type: str | None = None
    stream_mode: str


class KinesisFirehoseResource(ResourceBase):
    """Kinesis Firehose delivery stream resource."""

    resource_type: Literal[ResourceType.KINESIS_FIREHOSE] = ResourceType.KINESIS_FIREHOSE
    destination_type: str
    s3_destination: dict | None = None
    buffering_hints: dict | None = None


class KinesisVideoResource(ResourceBase):
    """Kinesis Video Stream resource."""

    resource_type: Literal[ResourceType.KINESIS_VIDEO_STREAM] = ResourceType.KINESIS_VIDEO_STREAM
    data_retention_in_hours: int
    encryption_type: str | None = None


class IAMRoleResource(ResourceBase):
    """IAM role resource with policies."""

    resource_type: Literal[ResourceType.IAM_ROLE] = ResourceType.IAM_ROLE
    assume_role_policy: dict
    attached_policies: list[dict] = []
    inline_policies: list[dict] = []


class S3BucketResource(ResourceBase):
    """S3 bucket resource discovered from Connect instance storage configs."""

    resource_type: Literal[ResourceType.S3_BUCKET] = ResourceType.S3_BUCKET
    bucket_region: str = ""
    storage_types: list[str] = []  # e.g. ["CALL_RECORDINGS", "CHAT_TRANSCRIPTS"]
    encryption_type: str | None = None
    versioning_enabled: bool = False


class ApprovedOriginResource(ResourceBase):
    """Approved origin URL associated with a Connect instance."""

    resource_type: Literal[ResourceType.APPROVED_ORIGIN] = ResourceType.APPROVED_ORIGIN
    origin_url: str = ""
