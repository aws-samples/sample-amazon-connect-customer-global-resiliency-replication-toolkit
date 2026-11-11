"""Unit tests for streaming discovery module."""

from unittest.mock import MagicMock, patch

import pytest

from discovery.streaming_discovery import (
    _build_firehose_resource,
    _build_kinesis_stream_resource,
    _extract_firehose_arn,
    _extract_kinesis_stream_arn,
    _generate_resource_id,
    discover_streaming_resources,
)
from models.enums import ResourceType


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ACCOUNT = "123456789012"
REGION = "us-west-2"
INSTANCE_ID = "abc-def-123"


def _kinesis_arn(name: str) -> str:
    return f"arn:aws:kinesis:{REGION}:{ACCOUNT}:stream/{name}"


def _firehose_arn(name: str) -> str:
    return f"arn:aws:firehose:{REGION}:{ACCOUNT}:deliverystream/{name}"


# ---------------------------------------------------------------------------
# Storage config helpers
# ---------------------------------------------------------------------------


def _make_kinesis_storage_config(stream_arn: str) -> dict:
    return {
        "StorageType": "KINESIS_STREAM",
        "KinesisStreamConfig": {"StreamArn": stream_arn},
    }


def _make_firehose_storage_config(firehose_arn: str) -> dict:
    return {
        "StorageType": "KINESIS_FIREHOSE",
        "KinesisFirehoseConfig": {"FirehoseArn": firehose_arn},
    }


def _make_kinesis_describe_response(
    name: str,
    shard_count: int = 2,
    retention: int = 24,
    encryption: str = "NONE",
    stream_mode: str = "PROVISIONED",
) -> dict:
    return {
        "StreamName": name,
        "StreamARN": _kinesis_arn(name),
        "Shards": [{"ShardId": f"shard-{i}"} for i in range(shard_count)],
        "RetentionPeriodHours": retention,
        "EncryptionType": encryption,
        "StreamModeDetails": {"StreamMode": stream_mode},
    }


def _make_firehose_describe_response(
    name: str,
    bucket_arn: str = "arn:aws:s3:::my-bucket",
    prefix: str = "ctr/",
    size_mb: int = 5,
    interval_s: int = 300,
) -> dict:
    return {
        "DeliveryStreamName": name,
        "DeliveryStreamARN": _firehose_arn(name),
        "Destinations": [
            {
                "ExtendedS3DestinationDescription": {
                    "BucketARN": bucket_arn,
                    "Prefix": prefix,
                    "ErrorOutputPrefix": "errors/",
                    "BufferingHints": {
                        "SizeInMBs": size_mb,
                        "IntervalInSeconds": interval_s,
                    },
                }
            }
        ],
    }


# ---------------------------------------------------------------------------
# Tests for helper functions
# ---------------------------------------------------------------------------


class TestGenerateResourceId:
    def test_deterministic(self):
        arn = _kinesis_arn("my-stream")
        assert _generate_resource_id(arn) == _generate_resource_id(arn)

    def test_different_arns_produce_different_ids(self):
        assert _generate_resource_id(_kinesis_arn("a")) != _generate_resource_id(
            _kinesis_arn("b")
        )

    def test_returns_12_char_hex(self):
        rid = _generate_resource_id(_kinesis_arn("test"))
        assert len(rid) == 12
        assert all(c in "0123456789abcdef" for c in rid)


class TestExtractKinesisStreamArn:
    def test_valid_config(self):
        config = _make_kinesis_storage_config(_kinesis_arn("ctr-stream"))
        assert _extract_kinesis_stream_arn(config) == _kinesis_arn("ctr-stream")

    def test_missing_kinesis_config(self):
        assert _extract_kinesis_stream_arn({}) is None

    def test_empty_stream_arn(self):
        config = {"KinesisStreamConfig": {"StreamArn": ""}}
        assert _extract_kinesis_stream_arn(config) is None

    def test_no_stream_arn_key(self):
        config = {"KinesisStreamConfig": {}}
        assert _extract_kinesis_stream_arn(config) is None


class TestExtractFirehoseArn:
    def test_valid_config(self):
        config = _make_firehose_storage_config(_firehose_arn("ctr-firehose"))
        assert _extract_firehose_arn(config) == _firehose_arn("ctr-firehose")

    def test_missing_firehose_config(self):
        assert _extract_firehose_arn({}) is None

    def test_empty_firehose_arn(self):
        config = {"KinesisFirehoseConfig": {"FirehoseArn": ""}}
        assert _extract_firehose_arn(config) is None

    def test_no_firehose_arn_key(self):
        config = {"KinesisFirehoseConfig": {}}
        assert _extract_firehose_arn(config) is None


# ---------------------------------------------------------------------------
# Tests for resource builders
# ---------------------------------------------------------------------------


class TestBuildKinesisStreamResource:
    def test_basic_stream(self):
        arn = _kinesis_arn("ctr-stream")
        desc = _make_kinesis_describe_response("ctr-stream")
        resource = _build_kinesis_stream_resource(arn, desc)

        assert resource.name == "ctr-stream"
        assert resource.arn == arn
        assert resource.resource_type == ResourceType.KINESIS_STREAM
        assert resource.shard_count == 2
        assert resource.retention_period == 24
        assert resource.encryption_type == "NONE"
        assert resource.stream_mode == "PROVISIONED"
        assert resource.config_summary["shard_count"] == "2"
        assert resource.config_summary["retention_hours"] == "24"

    def test_encrypted_on_demand_stream(self):
        arn = _kinesis_arn("encrypted-stream")
        desc = _make_kinesis_describe_response(
            "encrypted-stream",
            shard_count=4,
            retention=168,
            encryption="KMS",
            stream_mode="ON_DEMAND",
        )
        resource = _build_kinesis_stream_resource(arn, desc)

        assert resource.shard_count == 4
        assert resource.retention_period == 168
        assert resource.encryption_type == "KMS"
        assert resource.stream_mode == "ON_DEMAND"
        assert resource.config_summary["encryption"] == "KMS"


class TestBuildFirehoseResource:
    def test_extended_s3_destination(self):
        arn = _firehose_arn("ctr-firehose")
        desc = _make_firehose_describe_response("ctr-firehose")
        resource = _build_firehose_resource(arn, desc)

        assert resource.name == "ctr-firehose"
        assert resource.arn == arn
        assert resource.resource_type == ResourceType.KINESIS_FIREHOSE
        assert resource.destination_type == "ExtendedS3"
        assert resource.s3_destination is not None
        assert resource.s3_destination["BucketARN"] == "arn:aws:s3:::my-bucket"
        assert resource.buffering_hints is not None
        assert resource.buffering_hints["SizeInMBs"] == 5

    def test_no_destinations(self):
        arn = _firehose_arn("empty-firehose")
        desc = {"DeliveryStreamName": "empty-firehose", "Destinations": []}
        resource = _build_firehose_resource(arn, desc)

        assert resource.destination_type == "Unknown"
        assert resource.s3_destination is None
        assert resource.buffering_hints is None


# ---------------------------------------------------------------------------
# Tests for discover_streaming_resources
# ---------------------------------------------------------------------------


class TestDiscoverStreamingResources:
    """Tests for the main discover_streaming_resources orchestration."""

    @patch("discovery.streaming_discovery.create_source_client")
    def test_no_storage_configs(self, mock_create_client):
        """When Connect returns no storage configs, result should be empty."""
        mock_connect = MagicMock()
        mock_connect.list_instance_storage_configs.return_value = {
            "StorageConfigs": []
        }
        mock_kinesis = MagicMock()
        mock_firehose = MagicMock()

        def side_effect(service, region):
            return {
                "connect": mock_connect,
                "kinesis": mock_kinesis,
                "firehose": mock_firehose,
            }[service]

        mock_create_client.side_effect = side_effect

        resources = discover_streaming_resources(INSTANCE_ID, REGION)

        assert resources == []
        # Should be called for both CONTACT_TRACE_RECORDS and AGENT_EVENTS
        assert mock_connect.list_instance_storage_configs.call_count == 2

    @patch("discovery.streaming_discovery.create_source_client")
    def test_single_kinesis_stream(self, mock_create_client):
        """Discover a single Kinesis Data Stream from CTR config."""
        stream_name = "ctr-stream"
        stream_arn = _kinesis_arn(stream_name)

        mock_connect = MagicMock()
        mock_connect.list_instance_storage_configs.side_effect = [
            {"StorageConfigs": [_make_kinesis_storage_config(stream_arn)]},
            {"StorageConfigs": []},
        ]

        mock_kinesis = MagicMock()
        mock_kinesis.describe_stream.return_value = {
            "StreamDescription": _make_kinesis_describe_response(stream_name)
        }

        mock_firehose = MagicMock()

        def side_effect(service, region):
            return {
                "connect": mock_connect,
                "kinesis": mock_kinesis,
                "firehose": mock_firehose,
            }[service]

        mock_create_client.side_effect = side_effect

        resources = discover_streaming_resources(INSTANCE_ID, REGION)

        assert len(resources) == 1
        assert resources[0].name == stream_name
        assert resources[0].resource_type == ResourceType.KINESIS_STREAM
        assert resources[0].shard_count == 2
        mock_kinesis.describe_stream.assert_called_once_with(StreamName=stream_name)

    @patch("discovery.streaming_discovery.create_source_client")
    def test_single_firehose_stream(self, mock_create_client):
        """Discover a single Firehose delivery stream from CTR config."""
        stream_name = "ctr-firehose"
        stream_arn = _firehose_arn(stream_name)

        mock_connect = MagicMock()
        mock_connect.list_instance_storage_configs.side_effect = [
            {"StorageConfigs": [_make_firehose_storage_config(stream_arn)]},
            {"StorageConfigs": []},
        ]

        mock_kinesis = MagicMock()
        mock_firehose = MagicMock()
        mock_firehose.describe_delivery_stream.return_value = {
            "DeliveryStreamDescription": _make_firehose_describe_response(stream_name)
        }

        def side_effect(service, region):
            return {
                "connect": mock_connect,
                "kinesis": mock_kinesis,
                "firehose": mock_firehose,
            }[service]

        mock_create_client.side_effect = side_effect

        resources = discover_streaming_resources(INSTANCE_ID, REGION)

        assert len(resources) == 1
        assert resources[0].name == stream_name
        assert resources[0].resource_type == ResourceType.KINESIS_FIREHOSE
        assert resources[0].destination_type == "ExtendedS3"
        mock_firehose.describe_delivery_stream.assert_called_once_with(
            DeliveryStreamName=stream_name
        )

    @patch("discovery.streaming_discovery.create_source_client")
    def test_both_kinesis_and_firehose(self, mock_create_client):
        """Discover both Kinesis and Firehose from different resource types."""
        kinesis_name = "ctr-kinesis"
        firehose_name = "agent-firehose"

        mock_connect = MagicMock()
        mock_connect.list_instance_storage_configs.side_effect = [
            # CTR has a Kinesis stream
            {"StorageConfigs": [_make_kinesis_storage_config(_kinesis_arn(kinesis_name))]},
            # Agent Events has a Firehose
            {"StorageConfigs": [_make_firehose_storage_config(_firehose_arn(firehose_name))]},
        ]

        mock_kinesis = MagicMock()
        mock_kinesis.describe_stream.return_value = {
            "StreamDescription": _make_kinesis_describe_response(kinesis_name)
        }

        mock_firehose = MagicMock()
        mock_firehose.describe_delivery_stream.return_value = {
            "DeliveryStreamDescription": _make_firehose_describe_response(firehose_name)
        }

        def side_effect(service, region):
            return {
                "connect": mock_connect,
                "kinesis": mock_kinesis,
                "firehose": mock_firehose,
            }[service]

        mock_create_client.side_effect = side_effect

        resources = discover_streaming_resources(INSTANCE_ID, REGION)

        assert len(resources) == 2
        types = {r.resource_type for r in resources}
        assert ResourceType.KINESIS_STREAM in types
        assert ResourceType.KINESIS_FIREHOSE in types

    @patch("discovery.streaming_discovery.create_source_client")
    def test_duplicate_stream_arns_deduplicated(self, mock_create_client):
        """Same stream ARN in CTR and Agent Events should appear only once."""
        stream_name = "shared-stream"
        stream_arn = _kinesis_arn(stream_name)

        mock_connect = MagicMock()
        mock_connect.list_instance_storage_configs.side_effect = [
            {"StorageConfigs": [_make_kinesis_storage_config(stream_arn)]},
            {"StorageConfigs": [_make_kinesis_storage_config(stream_arn)]},
        ]

        mock_kinesis = MagicMock()
        mock_kinesis.describe_stream.return_value = {
            "StreamDescription": _make_kinesis_describe_response(stream_name)
        }

        mock_firehose = MagicMock()

        def side_effect(service, region):
            return {
                "connect": mock_connect,
                "kinesis": mock_kinesis,
                "firehose": mock_firehose,
            }[service]

        mock_create_client.side_effect = side_effect

        resources = discover_streaming_resources(INSTANCE_ID, REGION)

        assert len(resources) == 1
        assert resources[0].name == stream_name
        # DescribeStream should only be called once
        mock_kinesis.describe_stream.assert_called_once()

    @patch("discovery.streaming_discovery.create_source_client")
    def test_describe_stream_failure_skips_resource(self, mock_create_client):
        """If DescribeStream fails, the stream should be skipped gracefully."""
        good_name = "good-stream"
        bad_name = "bad-stream"

        mock_connect = MagicMock()
        mock_connect.list_instance_storage_configs.side_effect = [
            {
                "StorageConfigs": [
                    _make_kinesis_storage_config(_kinesis_arn(good_name)),
                    _make_kinesis_storage_config(_kinesis_arn(bad_name)),
                ]
            },
            {"StorageConfigs": []},
        ]

        mock_kinesis = MagicMock()

        def describe_side_effect(StreamName):
            if StreamName == bad_name:
                raise RuntimeError("Access denied")
            return {"StreamDescription": _make_kinesis_describe_response(good_name)}

        mock_kinesis.describe_stream.side_effect = describe_side_effect

        mock_firehose = MagicMock()

        def side_effect(service, region):
            return {
                "connect": mock_connect,
                "kinesis": mock_kinesis,
                "firehose": mock_firehose,
            }[service]

        mock_create_client.side_effect = side_effect

        resources = discover_streaming_resources(INSTANCE_ID, REGION)

        assert len(resources) == 1
        assert resources[0].name == good_name

    @patch("discovery.streaming_discovery.create_source_client")
    def test_describe_firehose_failure_skips_resource(self, mock_create_client):
        """If DescribeDeliveryStream fails, the firehose should be skipped."""
        mock_connect = MagicMock()
        mock_connect.list_instance_storage_configs.side_effect = [
            {"StorageConfigs": [_make_firehose_storage_config(_firehose_arn("bad-firehose"))]},
            {"StorageConfigs": []},
        ]

        mock_kinesis = MagicMock()
        mock_firehose = MagicMock()
        mock_firehose.describe_delivery_stream.side_effect = RuntimeError("Not found")

        def side_effect(service, region):
            return {
                "connect": mock_connect,
                "kinesis": mock_kinesis,
                "firehose": mock_firehose,
            }[service]

        mock_create_client.side_effect = side_effect

        resources = discover_streaming_resources(INSTANCE_ID, REGION)

        assert resources == []

    @patch("discovery.streaming_discovery.create_source_client")
    def test_list_storage_configs_failure_continues(self, mock_create_client):
        """If ListInstanceStorageConfigs fails for one type, the other should still work."""
        stream_name = "agent-stream"

        mock_connect = MagicMock()
        mock_connect.list_instance_storage_configs.side_effect = [
            RuntimeError("CTR config failed"),
            {"StorageConfigs": [_make_kinesis_storage_config(_kinesis_arn(stream_name))]},
        ]

        mock_kinesis = MagicMock()
        mock_kinesis.describe_stream.return_value = {
            "StreamDescription": _make_kinesis_describe_response(stream_name)
        }

        mock_firehose = MagicMock()

        def side_effect(service, region):
            return {
                "connect": mock_connect,
                "kinesis": mock_kinesis,
                "firehose": mock_firehose,
            }[service]

        mock_create_client.side_effect = side_effect

        resources = discover_streaming_resources(INSTANCE_ID, REGION)

        assert len(resources) == 1
        assert resources[0].name == stream_name

    @patch("discovery.streaming_discovery.create_source_client")
    def test_pagination_of_storage_configs(self, mock_create_client):
        """Pagination of ListInstanceStorageConfigs should be handled."""
        stream1 = "stream-1"
        stream2 = "stream-2"

        mock_connect = MagicMock()
        mock_connect.list_instance_storage_configs.side_effect = [
            # CTR: paginated
            {
                "StorageConfigs": [_make_kinesis_storage_config(_kinesis_arn(stream1))],
                "NextToken": "page2",
            },
            {
                "StorageConfigs": [_make_kinesis_storage_config(_kinesis_arn(stream2))],
            },
            # Agent Events: empty
            {"StorageConfigs": []},
        ]

        mock_kinesis = MagicMock()
        mock_kinesis.describe_stream.side_effect = [
            {"StreamDescription": _make_kinesis_describe_response(stream1)},
            {"StreamDescription": _make_kinesis_describe_response(stream2)},
        ]

        mock_firehose = MagicMock()

        def side_effect(service, region):
            return {
                "connect": mock_connect,
                "kinesis": mock_kinesis,
                "firehose": mock_firehose,
            }[service]

        mock_create_client.side_effect = side_effect

        resources = discover_streaming_resources(INSTANCE_ID, REGION)

        assert len(resources) == 2
        names = {r.name for r in resources}
        assert names == {stream1, stream2}
