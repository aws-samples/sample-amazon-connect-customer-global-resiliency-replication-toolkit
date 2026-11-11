"""Enums for resource types and replication statuses."""

from enum import Enum


class ResourceType(str, Enum):
    """Types of AWS resources that can be discovered and replicated."""

    LAMBDA = "LAMBDA"
    LEX_BOT = "LEX_BOT"
    KINESIS_STREAM = "KINESIS_STREAM"
    KINESIS_FIREHOSE = "KINESIS_FIREHOSE"
    KINESIS_VIDEO_STREAM = "KINESIS_VIDEO_STREAM"
    IAM_ROLE = "IAM_ROLE"
    S3_BUCKET = "S3_BUCKET"
    APPROVED_ORIGIN = "APPROVED_ORIGIN"


class ReplicationStatus(str, Enum):
    """Status of a resource's replication to the target region."""

    NOT_REPLICATED = "NOT_REPLICATED"
    IN_PROGRESS = "IN_PROGRESS"
    REPLICATED = "REPLICATED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"
