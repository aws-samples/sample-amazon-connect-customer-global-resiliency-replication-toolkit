"""Unit tests for streaming resource replication (Kinesis, Firehose, KVS)."""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from models.resources import (
    KinesisFirehoseResource,
    KinesisStreamResource,
    KinesisVideoResource,
)
from replication.streaming_replication import (
    replicate_kinesis_stream,
    replicate_firehose_stream,
    replicate_kvs_stream,
    _build_s3_destination_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TARGET_REGION = "us-east-1"
SOURCE_REGION = "us-west-2"
ACCOUNT_ID = "123456789012"


def _make_kinesis_stream(
    name: str = "ctr-stream",
    shard_count: int = 2,
    retention_period: int = 24,
    encryption_type: str | None = None,
    stream_mode: str = "PROVISIONED",
) -> KinesisStreamResource:
    return KinesisStreamResource(
        id="kinesis-1",
        name=name,
        arn=f"arn:aws:kinesis:{SOURCE_REGION}:{ACCOUNT_ID}:stream/{name}",
        shard_count=shard_count,
        retention_period=retention_period,
        encryption_type=encryption_type,
        stream_mode=stream_mode,
    )


def _make_firehose_stream(
    name: str = "ctr-firehose",
    destination_type: str = "ExtendedS3",
    s3_destination: dict | None = None,
    buffering_hints: dict | None = None,
) -> KinesisFirehoseResource:
    return KinesisFirehoseResource(
        id="firehose-1",
        name=name,
        arn=f"arn:aws:firehose:{SOURCE_REGION}:{ACCOUNT_ID}:deliverystream/{name}",
        destination_type=destination_type,
        s3_destination=s3_destination,
        buffering_hints=buffering_hints,
    )


def _make_kvs_stream(
    name: str = "media-stream",
    data_retention_in_hours: int = 24,
    encryption_type: str | None = None,
) -> KinesisVideoResource:
    return KinesisVideoResource(
        id="kvs-1",
        name=name,
        arn=f"arn:aws:kinesisvideo:{SOURCE_REGION}:{ACCOUNT_ID}:stream/{name}/1234",
        data_retention_in_hours=data_retention_in_hours,
        encryption_type=encryption_type,
    )


def _client_error(code: str, message: str = "error") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "TestOperation",
    )


# ---------------------------------------------------------------------------
# replicate_kinesis_stream — success path
# ---------------------------------------------------------------------------


class TestReplicateKinesisStreamSuccess:
    @patch("replication.streaming_replication.create_target_client")
    def test_basic_provisioned_stream(self, mock_create_client):
        mock_kinesis = MagicMock()
        mock_create_client.return_value = mock_kinesis
        stream_arn = f"arn:aws:kinesis:{TARGET_REGION}:{ACCOUNT_ID}:stream/ctr-stream"
        mock_kinesis.describe_stream.return_value = {
            "StreamDescription": {"StreamARN": stream_arn}
        }

        stream = _make_kinesis_stream()
        result = replicate_kinesis_stream(stream, TARGET_REGION)

        assert result == stream_arn
        mock_create_client.assert_called_once_with("kinesis", TARGET_REGION)
        mock_kinesis.create_stream.assert_called_once_with(
            StreamName="ctr-stream-dr",
            StreamModeDetails={"StreamMode": "PROVISIONED"},
            ShardCount=2,
        )

    @patch("replication.streaming_replication.create_target_client")
    def test_on_demand_stream_no_shard_count(self, mock_create_client):
        mock_kinesis = MagicMock()
        mock_create_client.return_value = mock_kinesis
        stream_arn = f"arn:aws:kinesis:{TARGET_REGION}:{ACCOUNT_ID}:stream/od-stream"
        mock_kinesis.describe_stream.return_value = {
            "StreamDescription": {"StreamARN": stream_arn}
        }

        stream = _make_kinesis_stream(name="od-stream", stream_mode="ON_DEMAND")
        result = replicate_kinesis_stream(stream, TARGET_REGION)

        assert result == stream_arn
        call_kwargs = mock_kinesis.create_stream.call_args[1]
        assert "ShardCount" not in call_kwargs
        assert call_kwargs["StreamModeDetails"] == {"StreamMode": "ON_DEMAND"}

    @patch("replication.streaming_replication.create_target_client")
    def test_stream_with_custom_retention(self, mock_create_client):
        mock_kinesis = MagicMock()
        mock_create_client.return_value = mock_kinesis
        stream_arn = f"arn:aws:kinesis:{TARGET_REGION}:{ACCOUNT_ID}:stream/ret-stream-dr"
        mock_kinesis.describe_stream.return_value = {
            "StreamDescription": {"StreamARN": stream_arn}
        }

        stream = _make_kinesis_stream(name="ret-stream", retention_period=168)
        replicate_kinesis_stream(stream, TARGET_REGION)

        mock_kinesis.increase_stream_retention_period.assert_called_once_with(
            StreamName="ret-stream-dr",
            RetentionPeriodHours=168,
        )

    @patch("replication.streaming_replication.create_target_client")
    def test_stream_with_default_retention_skips_update(self, mock_create_client):
        mock_kinesis = MagicMock()
        mock_create_client.return_value = mock_kinesis
        stream_arn = f"arn:aws:kinesis:{TARGET_REGION}:{ACCOUNT_ID}:stream/def-stream"
        mock_kinesis.describe_stream.return_value = {
            "StreamDescription": {"StreamARN": stream_arn}
        }

        stream = _make_kinesis_stream(name="def-stream", retention_period=24)
        replicate_kinesis_stream(stream, TARGET_REGION)

        mock_kinesis.increase_stream_retention_period.assert_not_called()

    @patch("replication.streaming_replication.create_target_client")
    def test_stream_with_encryption(self, mock_create_client):
        mock_kinesis = MagicMock()
        mock_create_client.return_value = mock_kinesis
        stream_arn = f"arn:aws:kinesis:{TARGET_REGION}:{ACCOUNT_ID}:stream/enc-stream-dr"
        mock_kinesis.describe_stream.return_value = {
            "StreamDescription": {"StreamARN": stream_arn}
        }

        stream = _make_kinesis_stream(name="enc-stream", encryption_type="KMS")
        replicate_kinesis_stream(stream, TARGET_REGION)

        mock_kinesis.start_stream_encryption.assert_called_once_with(
            StreamName="enc-stream-dr",
            EncryptionType="KMS",
            KeyId="alias/aws/kinesis",
        )

    @patch("replication.streaming_replication.create_target_client")
    def test_stream_without_encryption_skips(self, mock_create_client):
        mock_kinesis = MagicMock()
        mock_create_client.return_value = mock_kinesis
        stream_arn = f"arn:aws:kinesis:{TARGET_REGION}:{ACCOUNT_ID}:stream/noenc"
        mock_kinesis.describe_stream.return_value = {
            "StreamDescription": {"StreamARN": stream_arn}
        }

        stream = _make_kinesis_stream(encryption_type="NONE")
        replicate_kinesis_stream(stream, TARGET_REGION)

        mock_kinesis.start_stream_encryption.assert_not_called()

    @patch("replication.streaming_replication.create_target_client")
    def test_stream_with_none_encryption_skips(self, mock_create_client):
        mock_kinesis = MagicMock()
        mock_create_client.return_value = mock_kinesis
        stream_arn = f"arn:aws:kinesis:{TARGET_REGION}:{ACCOUNT_ID}:stream/noenc2"
        mock_kinesis.describe_stream.return_value = {
            "StreamDescription": {"StreamARN": stream_arn}
        }

        stream = _make_kinesis_stream(encryption_type=None)
        replicate_kinesis_stream(stream, TARGET_REGION)

        mock_kinesis.start_stream_encryption.assert_not_called()


# ---------------------------------------------------------------------------
# replicate_kinesis_stream — error handling
# ---------------------------------------------------------------------------


class TestReplicateKinesisStreamErrors:
    @patch("replication.streaming_replication.create_target_client")
    def test_stream_already_exists_reuses_existing(self, mock_create_client):
        mock_kinesis = MagicMock()
        mock_create_client.return_value = mock_kinesis
        mock_kinesis.create_stream.side_effect = _client_error(
            "ResourceInUseException", "Stream already exists"
        )
        existing_arn = f"arn:aws:kinesis:{TARGET_REGION}:{ACCOUNT_ID}:stream/ctr-stream"
        mock_kinesis.describe_stream.return_value = {
            "StreamDescription": {"StreamARN": existing_arn}
        }

        stream = _make_kinesis_stream()
        result = replicate_kinesis_stream(stream, TARGET_REGION)
        assert result == existing_arn
        # Should NOT try to set retention or encryption on existing stream
        mock_kinesis.increase_stream_retention_period.assert_not_called()
        mock_kinesis.start_stream_encryption.assert_not_called()

    @patch("replication.streaming_replication.create_target_client")
    def test_access_denied_raises_permission_error(self, mock_create_client):
        mock_kinesis = MagicMock()
        mock_create_client.return_value = mock_kinesis
        mock_kinesis.create_stream.side_effect = _client_error(
            "AccessDeniedException", "no perms"
        )

        stream = _make_kinesis_stream()
        with pytest.raises(PermissionError, match="Permission denied"):
            replicate_kinesis_stream(stream, TARGET_REGION)

    @patch("replication.streaming_replication.create_target_client")
    def test_unknown_error_raises_runtime_error(self, mock_create_client):
        mock_kinesis = MagicMock()
        mock_create_client.return_value = mock_kinesis
        mock_kinesis.create_stream.side_effect = _client_error(
            "LimitExceededException", "too many streams"
        )

        stream = _make_kinesis_stream()
        with pytest.raises(RuntimeError, match="Failed to create Kinesis Data Stream"):
            replicate_kinesis_stream(stream, TARGET_REGION)


# ---------------------------------------------------------------------------
# replicate_firehose_stream — success path
# ---------------------------------------------------------------------------


class TestReplicateFirehoseStreamSuccess:
    @patch("replication.streaming_replication.create_target_client")
    def test_basic_firehose_with_s3_destination(self, mock_create_client):
        mock_firehose = MagicMock()
        mock_create_client.return_value = mock_firehose
        stream_arn = (
            f"arn:aws:firehose:{TARGET_REGION}:{ACCOUNT_ID}:"
            f"deliverystream/ctr-firehose"
        )
        mock_firehose.create_delivery_stream.return_value = {
            "DeliveryStreamARN": stream_arn
        }

        s3_dest = {
            "BucketARN": f"arn:aws:s3:::source-bucket",
            "RoleARN": f"arn:aws:iam::{ACCOUNT_ID}:role/firehose-role",
            "Prefix": "ctr/",
            "BufferingHints": {"SizeInMBs": 5, "IntervalInSeconds": 300},
        }
        stream = _make_firehose_stream(s3_destination=s3_dest)
        result = replicate_firehose_stream(stream, TARGET_REGION)

        assert result == stream_arn
        mock_create_client.assert_called_once_with("firehose", TARGET_REGION)
        call_kwargs = mock_firehose.create_delivery_stream.call_args[1]
        assert call_kwargs["DeliveryStreamName"] == "ctr-firehose-dr"
        assert "ExtendedS3DestinationConfiguration" in call_kwargs

    @patch("replication.streaming_replication.create_target_client")
    def test_firehose_with_target_s3_bucket_override(self, mock_create_client):
        mock_firehose = MagicMock()
        mock_create_client.return_value = mock_firehose
        stream_arn = (
            f"arn:aws:firehose:{TARGET_REGION}:{ACCOUNT_ID}:"
            f"deliverystream/ctr-firehose"
        )
        mock_firehose.create_delivery_stream.return_value = {
            "DeliveryStreamARN": stream_arn
        }

        s3_dest = {
            "BucketARN": "arn:aws:s3:::source-bucket",
            "RoleARN": f"arn:aws:iam::{ACCOUNT_ID}:role/firehose-role",
        }
        stream = _make_firehose_stream(s3_destination=s3_dest)
        result = replicate_firehose_stream(
            stream, TARGET_REGION, s3_bucket_name="target-bucket"
        )

        assert result == stream_arn
        call_kwargs = mock_firehose.create_delivery_stream.call_args[1]
        s3_config = call_kwargs["ExtendedS3DestinationConfiguration"]
        assert s3_config["BucketARN"] == "arn:aws:s3:::target-bucket"

    @patch("replication.streaming_replication.create_target_client")
    def test_firehose_with_buffering_hints_from_resource(self, mock_create_client):
        mock_firehose = MagicMock()
        mock_create_client.return_value = mock_firehose
        stream_arn = (
            f"arn:aws:firehose:{TARGET_REGION}:{ACCOUNT_ID}:"
            f"deliverystream/buf-firehose"
        )
        mock_firehose.create_delivery_stream.return_value = {
            "DeliveryStreamARN": stream_arn
        }

        s3_dest = {
            "BucketARN": "arn:aws:s3:::my-bucket",
            "RoleARN": f"arn:aws:iam::{ACCOUNT_ID}:role/firehose-role",
        }
        stream = _make_firehose_stream(
            name="buf-firehose",
            s3_destination=s3_dest,
            buffering_hints={"SizeInMBs": 10, "IntervalInSeconds": 60},
        )
        replicate_firehose_stream(stream, TARGET_REGION)

        call_kwargs = mock_firehose.create_delivery_stream.call_args[1]
        s3_config = call_kwargs["ExtendedS3DestinationConfiguration"]
        assert s3_config["BufferingHints"]["SizeInMBs"] == 10
        assert s3_config["BufferingHints"]["IntervalInSeconds"] == 60

    @patch("replication.streaming_replication.create_target_client")
    def test_firehose_without_s3_destination(self, mock_create_client):
        mock_firehose = MagicMock()
        mock_create_client.return_value = mock_firehose
        stream_arn = (
            f"arn:aws:firehose:{TARGET_REGION}:{ACCOUNT_ID}:"
            f"deliverystream/no-s3"
        )
        mock_firehose.create_delivery_stream.return_value = {
            "DeliveryStreamARN": stream_arn
        }

        stream = _make_firehose_stream(
            name="no-s3", destination_type="Splunk", s3_destination=None
        )
        result = replicate_firehose_stream(stream, TARGET_REGION)

        assert result == stream_arn
        call_kwargs = mock_firehose.create_delivery_stream.call_args[1]
        assert "ExtendedS3DestinationConfiguration" not in call_kwargs


# ---------------------------------------------------------------------------
# replicate_firehose_stream — error handling
# ---------------------------------------------------------------------------


class TestReplicateFirehoseStreamErrors:
    @patch("replication.streaming_replication.create_target_client")
    def test_firehose_already_exists_reuses_existing(self, mock_create_client):
        mock_firehose = MagicMock()
        mock_create_client.return_value = mock_firehose
        mock_firehose.create_delivery_stream.side_effect = _client_error(
            "ResourceInUseException", "Stream already exists"
        )
        existing_arn = (
            f"arn:aws:firehose:{TARGET_REGION}:{ACCOUNT_ID}:"
            f"deliverystream/ctr-firehose"
        )
        mock_firehose.describe_delivery_stream.return_value = {
            "DeliveryStreamDescription": {"DeliveryStreamARN": existing_arn}
        }

        stream = _make_firehose_stream(
            s3_destination={
                "BucketARN": "arn:aws:s3:::b",
                "RoleARN": "arn:aws:iam::123:role/r",
            }
        )
        result = replicate_firehose_stream(stream, TARGET_REGION)
        assert result == existing_arn
        mock_firehose.describe_delivery_stream.assert_called_once_with(
            DeliveryStreamName="ctr-firehose-dr"
        )

    @patch("replication.streaming_replication.create_target_client")
    def test_firehose_access_denied_raises_permission_error(self, mock_create_client):
        mock_firehose = MagicMock()
        mock_create_client.return_value = mock_firehose
        mock_firehose.create_delivery_stream.side_effect = _client_error(
            "AccessDeniedException", "no perms"
        )

        stream = _make_firehose_stream(
            s3_destination={
                "BucketARN": "arn:aws:s3:::b",
                "RoleARN": "arn:aws:iam::123:role/r",
            }
        )
        with pytest.raises(PermissionError, match="Permission denied"):
            replicate_firehose_stream(stream, TARGET_REGION)

    @patch("replication.streaming_replication.create_target_client")
    def test_firehose_unknown_error_raises_runtime_error(self, mock_create_client):
        mock_firehose = MagicMock()
        mock_create_client.return_value = mock_firehose
        mock_firehose.create_delivery_stream.side_effect = _client_error(
            "ServiceUnavailableException", "try later"
        )

        stream = _make_firehose_stream(
            s3_destination={
                "BucketARN": "arn:aws:s3:::b",
                "RoleARN": "arn:aws:iam::123:role/r",
            }
        )
        with pytest.raises(RuntimeError, match="Failed to create Firehose"):
            replicate_firehose_stream(stream, TARGET_REGION)


# ---------------------------------------------------------------------------
# replicate_kvs_stream — no physical stream creation
# ---------------------------------------------------------------------------


class TestReplicateKvsStreamSuccess:
    """KVS replication no longer creates physical streams.

    Amazon Connect creates KVS streams on-the-fly during calls. The
    replicate_kvs_stream function returns a synthetic ARN and defers
    actual enablement to the association step (MEDIA_STREAMS storage config).
    """

    def test_basic_kvs_stream_returns_synthetic_arn(self):
        """Physical KVS stream should return a synthetic config-based ARN."""
        stream = _make_kvs_stream()
        result = replicate_kvs_stream(stream, TARGET_REGION)

        # Should return a synthetic ARN with the target region
        assert TARGET_REGION in result
        assert "config/media-streams" in result or TARGET_REGION in result

    def test_kvs_config_based_stream_swaps_region(self):
        """Config-based KVS resource (synthetic ARN) should swap region."""
        stream = KinesisVideoResource(
            id="kvs-config-1",
            name="media-prefix",
            arn=f"arn:aws:kinesisvideo:{SOURCE_REGION}:{ACCOUNT_ID}:config/media-streams/media-prefix",
            data_retention_in_hours=24,
            encryption_type=None,
        )
        result = replicate_kvs_stream(stream, TARGET_REGION)

        assert TARGET_REGION in result
        assert SOURCE_REGION not in result
        assert "config/media-streams" in result

    def test_kvs_stream_does_not_call_aws(self):
        """No AWS API calls should be made — no physical stream creation."""
        stream = _make_kvs_stream()
        # No mocking needed — the function should not call any AWS APIs
        result = replicate_kvs_stream(stream, TARGET_REGION)
        assert result is not None
        assert isinstance(result, str)

    def test_kvs_stream_with_custom_retention_returns_synthetic(self):
        """Custom retention doesn't affect synthetic ARN generation."""
        stream = _make_kvs_stream(name="long-ret", data_retention_in_hours=168)
        result = replicate_kvs_stream(stream, TARGET_REGION)
        assert TARGET_REGION in result

    def test_kvs_stream_with_encryption_returns_synthetic(self):
        """Encryption type doesn't affect synthetic ARN generation."""
        stream = _make_kvs_stream(name="enc-stream", encryption_type="KMS")
        result = replicate_kvs_stream(stream, TARGET_REGION)
        assert TARGET_REGION in result


# ---------------------------------------------------------------------------
# _build_s3_destination_config
# ---------------------------------------------------------------------------


class TestBuildS3DestinationConfig:
    def test_uses_override_bucket_name(self):
        stream = _make_firehose_stream(
            s3_destination={
                "BucketARN": "arn:aws:s3:::source-bucket",
                "RoleARN": "arn:aws:iam::123:role/r",
            }
        )
        config = _build_s3_destination_config(stream, "target-bucket")
        assert config["BucketARN"] == "arn:aws:s3:::target-bucket"

    def test_uses_original_bucket_when_no_override(self):
        stream = _make_firehose_stream(
            s3_destination={
                "BucketARN": "arn:aws:s3:::source-bucket",
                "RoleARN": "arn:aws:iam::123:role/r",
            }
        )
        config = _build_s3_destination_config(stream, None)
        assert config["BucketARN"] == "arn:aws:s3:::source-bucket"

    def test_includes_prefix(self):
        stream = _make_firehose_stream(
            s3_destination={
                "BucketARN": "arn:aws:s3:::b",
                "RoleARN": "arn:aws:iam::123:role/r",
                "Prefix": "ctr/year=!{timestamp:yyyy}/",
            }
        )
        config = _build_s3_destination_config(stream, None)
        assert config["Prefix"] == "ctr/year=!{timestamp:yyyy}/"

    def test_includes_buffering_from_resource(self):
        stream = _make_firehose_stream(
            s3_destination={
                "BucketARN": "arn:aws:s3:::b",
                "RoleARN": "arn:aws:iam::123:role/r",
            },
            buffering_hints={"SizeInMBs": 10, "IntervalInSeconds": 120},
        )
        config = _build_s3_destination_config(stream, None)
        assert config["BufferingHints"]["SizeInMBs"] == 10
        assert config["BufferingHints"]["IntervalInSeconds"] == 120

    def test_includes_buffering_from_s3_destination(self):
        stream = _make_firehose_stream(
            s3_destination={
                "BucketARN": "arn:aws:s3:::b",
                "RoleARN": "arn:aws:iam::123:role/r",
                "BufferingHints": {"SizeInMBs": 8, "IntervalInSeconds": 60},
            },
        )
        config = _build_s3_destination_config(stream, None)
        assert config["BufferingHints"]["SizeInMBs"] == 8
        assert config["BufferingHints"]["IntervalInSeconds"] == 60

    def test_includes_compression_format(self):
        stream = _make_firehose_stream(
            s3_destination={
                "BucketARN": "arn:aws:s3:::b",
                "RoleARN": "arn:aws:iam::123:role/r",
                "CompressionFormat": "GZIP",
            }
        )
        config = _build_s3_destination_config(stream, None)
        assert config["CompressionFormat"] == "GZIP"

    def test_no_s3_destination_returns_minimal_config(self):
        stream = _make_firehose_stream(s3_destination=None)
        config = _build_s3_destination_config(stream, None)
        assert config["BucketARN"] == ""
        assert config["RoleARN"] == ""


# ---------------------------------------------------------------------------
# KVS ARN doubling fix — positional region replacement
# ---------------------------------------------------------------------------

class TestKvsArnRegionReplacement:
    """Tests that the KVS synthetic ARN uses positional replacement to avoid doubling."""

    def test_config_based_arn_replaces_region_correctly(self):
        """Config-based ARN should correctly replace only the region field."""
        # Real-world config-based ARN format: region is at colon-index 3
        stream = KinesisVideoResource(
            id="kvs-1",
            name="acgr-iad-media",
            arn=f"arn:aws:kinesisvideo:us-east-1:{ACCOUNT_ID}:config/media-streams/acgr-iad-media",
            data_retention_in_hours=24,
            encryption_type=None,
        )
        result = replicate_kvs_stream(stream, "us-west-2")
        assert result == f"arn:aws:kinesisvideo:us-west-2:{ACCOUNT_ID}:config/media-streams/acgr-iad-media"
        # Verify no doubling — the ARN should have exactly one 'config/media-streams'
        assert result.count("config/media-streams") == 1

    def test_config_based_arn_no_doubling_with_region_in_name(self):
        """ARN where the resource name contains the source region string should not double."""
        stream = KinesisVideoResource(
            id="kvs-2",
            name="us-east-1-prefix",
            arn="arn:aws:kinesisvideo:us-east-1:123456789012:config/media-streams/us-east-1-prefix",
            data_retention_in_hours=24,
            encryption_type=None,
        )
        result = replicate_kvs_stream(stream, "us-west-2")
        # The region field (index 3) should be replaced, but the name part should NOT be
        parts = result.split(":")
        assert parts[3] == "us-west-2"
        # The resource part should still contain the original name
        assert "us-east-1-prefix" in result

    def test_physical_kvs_stream_returns_synthetic_arn(self):
        """Physical KVS stream should return a clean synthetic config-based ARN."""
        stream = KinesisVideoResource(
            id="kvs-3",
            name="acgr-iad-media-stream",
            arn="arn:aws:kinesisvideo:us-east-1:123456789012:stream/acgr-iad-media-stream/1234567890",
            data_retention_in_hours=24,
            encryption_type=None,
        )
        result = replicate_kvs_stream(stream, "us-west-2")
        assert "us-west-2" in result
        assert "config/media-streams" in result
        assert result.count("config/media-streams") == 1
