"""Unit tests for KVS discovery module."""

from unittest.mock import MagicMock, patch

import pytest

from discovery.kvs_discovery import (
    _build_kvs_resource,
    _extract_kvs_stream_arns,
    _find_lambda_consumers,
    _generate_resource_id,
    _get_kvs_stream_prefix,
    discover_kvs_resources,
)
from models.enums import ResourceType


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ACCOUNT = "123456789012"
REGION = "us-west-2"
INSTANCE_ID = "abc-def-123"


def _kvs_arn(name: str) -> str:
    return f"arn:aws:kinesisvideo:{REGION}:{ACCOUNT}:stream/{name}/1234567890"


def _lambda_arn(name: str) -> str:
    return f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{name}"


# ---------------------------------------------------------------------------
# Storage config helpers
# ---------------------------------------------------------------------------


def _make_media_storage_config(
    prefix: str = "connect-kvs-",
    stream_arn: str | None = None,
    kms_key_id: str | None = None,
) -> dict:
    kvs_config: dict = {"Prefix": prefix}
    if stream_arn:
        kvs_config["StreamArn"] = stream_arn
    encryption_config: dict = {}
    if kms_key_id:
        encryption_config["KeyId"] = kms_key_id
        encryption_config["EncryptionType"] = "KMS"
    else:
        encryption_config["EncryptionType"] = "NONE"
    kvs_config["EncryptionConfig"] = encryption_config
    return {
        "StorageType": "KINESIS_VIDEO_STREAM",
        "KinesisVideoStreamConfig": kvs_config,
    }


def _make_kvs_stream_info(
    name: str,
    retention_hours: int = 24,
    kms_key_id: str | None = None,
) -> dict:
    info = {
        "StreamName": name,
        "StreamARN": _kvs_arn(name),
        "DataRetentionInHours": retention_hours,
        "Status": "ACTIVE",
        "CreationTime": "2024-01-01T00:00:00Z",
    }
    if kms_key_id:
        info["KmsKeyId"] = kms_key_id
    return info


# ---------------------------------------------------------------------------
# Tests for helper functions
# ---------------------------------------------------------------------------


class TestGenerateResourceId:
    def test_deterministic(self):
        arn = _kvs_arn("my-stream")
        assert _generate_resource_id(arn) == _generate_resource_id(arn)

    def test_different_arns_produce_different_ids(self):
        assert _generate_resource_id(_kvs_arn("a")) != _generate_resource_id(
            _kvs_arn("b")
        )

    def test_returns_12_char_hex(self):
        rid = _generate_resource_id(_kvs_arn("test"))
        assert len(rid) == 12
        assert all(c in "0123456789abcdef" for c in rid)


class TestExtractKvsStreamArns:
    def test_config_with_direct_stream_arn(self):
        arn = _kvs_arn("direct-stream")
        config = _make_media_storage_config(stream_arn=arn)
        arns = _extract_kvs_stream_arns(config)
        assert arns == [arn]

    def test_config_without_stream_arn(self):
        config = _make_media_storage_config(prefix="connect-kvs-")
        arns = _extract_kvs_stream_arns(config)
        assert arns == []

    def test_missing_kvs_config(self):
        arns = _extract_kvs_stream_arns({})
        assert arns == []

    def test_empty_stream_arn_ignored(self):
        config = {
            "KinesisVideoStreamConfig": {"StreamArn": "", "Prefix": "test-"}
        }
        arns = _extract_kvs_stream_arns(config)
        assert arns == []


class TestGetKvsStreamPrefix:
    def test_valid_prefix(self):
        config = _make_media_storage_config(prefix="connect-kvs-")
        assert _get_kvs_stream_prefix(config) == "connect-kvs-"

    def test_missing_kvs_config(self):
        assert _get_kvs_stream_prefix({}) is None

    def test_empty_prefix(self):
        config = {"KinesisVideoStreamConfig": {"Prefix": ""}}
        assert _get_kvs_stream_prefix(config) is None

    def test_no_prefix_key(self):
        config = {"KinesisVideoStreamConfig": {}}
        assert _get_kvs_stream_prefix(config) is None


# ---------------------------------------------------------------------------
# Tests for resource builder
# ---------------------------------------------------------------------------


class TestBuildKvsResource:
    def test_basic_stream(self):
        arn = _kvs_arn("my-stream")
        info = _make_kvs_stream_info("my-stream")
        resource = _build_kvs_resource(arn, info)

        assert resource.name == "my-stream"
        assert resource.arn == arn
        assert resource.resource_type == ResourceType.KINESIS_VIDEO_STREAM
        assert resource.data_retention_in_hours == 24
        assert resource.encryption_type is None
        assert resource.config_summary["data_retention_hours"] == "24"
        assert "encryption" not in resource.config_summary

    def test_encrypted_stream(self):
        arn = _kvs_arn("encrypted-stream")
        info = _make_kvs_stream_info(
            "encrypted-stream",
            retention_hours=48,
            kms_key_id="arn:aws:kms:us-west-2:123456789012:key/abc-123",
        )
        resource = _build_kvs_resource(arn, info)

        assert resource.data_retention_in_hours == 48
        assert resource.encryption_type == "KMS"
        assert resource.config_summary["encryption"] == "KMS"
        assert resource.config_summary["data_retention_hours"] == "48"

    def test_zero_retention(self):
        arn = _kvs_arn("no-retention")
        info = _make_kvs_stream_info("no-retention", retention_hours=0)
        resource = _build_kvs_resource(arn, info)

        assert resource.data_retention_in_hours == 0


# ---------------------------------------------------------------------------
# Tests for Lambda consumer discovery
# ---------------------------------------------------------------------------


class TestFindLambdaConsumers:
    def test_no_consumers(self):
        mock_lambda = MagicMock()
        mock_lambda.list_event_source_mappings.return_value = {
            "EventSourceMappings": []
        }
        result = _find_lambda_consumers(mock_lambda, [_kvs_arn("stream1")])
        assert result == []

    def test_single_consumer(self):
        func_arn = _lambda_arn("kvs-processor")
        mock_lambda = MagicMock()
        mock_lambda.list_event_source_mappings.return_value = {
            "EventSourceMappings": [{"FunctionArn": func_arn}]
        }
        result = _find_lambda_consumers(mock_lambda, [_kvs_arn("stream1")])
        assert result == [func_arn]

    def test_deduplicates_consumers(self):
        func_arn = _lambda_arn("shared-processor")
        mock_lambda = MagicMock()
        mock_lambda.list_event_source_mappings.return_value = {
            "EventSourceMappings": [{"FunctionArn": func_arn}]
        }
        result = _find_lambda_consumers(
            mock_lambda, [_kvs_arn("stream1"), _kvs_arn("stream2")]
        )
        assert result == [func_arn]

    def test_multiple_consumers_across_streams(self):
        func1 = _lambda_arn("processor-1")
        func2 = _lambda_arn("processor-2")
        mock_lambda = MagicMock()
        mock_lambda.list_event_source_mappings.side_effect = [
            {"EventSourceMappings": [{"FunctionArn": func1}]},
            {"EventSourceMappings": [{"FunctionArn": func2}]},
        ]
        result = _find_lambda_consumers(
            mock_lambda, [_kvs_arn("stream1"), _kvs_arn("stream2")]
        )
        assert set(result) == {func1, func2}

    def test_api_failure_returns_empty(self):
        mock_lambda = MagicMock()
        mock_lambda.list_event_source_mappings.side_effect = RuntimeError("Access denied")
        result = _find_lambda_consumers(mock_lambda, [_kvs_arn("stream1")])
        assert result == []


# ---------------------------------------------------------------------------
# Tests for discover_kvs_resources
# ---------------------------------------------------------------------------


class TestDiscoverKvsResources:
    """Tests for the main discover_kvs_resources orchestration."""

    @patch("discovery.kvs_discovery.create_source_client")
    def test_no_storage_configs(self, mock_create_client):
        """When Connect returns no MEDIA_STREAMS configs, result should be empty."""
        mock_connect = MagicMock()
        mock_connect.list_instance_storage_configs.return_value = {
            "StorageConfigs": []
        }
        mock_kvs = MagicMock()
        mock_lambda = MagicMock()

        def side_effect(service, region):
            return {
                "connect": mock_connect,
                "kinesisvideo": mock_kvs,
                "lambda": mock_lambda,
            }[service]

        mock_create_client.side_effect = side_effect

        resources, lambda_arns = discover_kvs_resources(INSTANCE_ID, REGION)

        assert resources == []
        assert lambda_arns == []
        mock_connect.list_instance_storage_configs.assert_called_once()

    @patch("discovery.kvs_discovery.create_source_client")
    def test_direct_stream_arn_discovery(self, mock_create_client):
        """Discover a KVS stream from a direct ARN in the storage config."""
        stream_name = "direct-kvs-stream"
        stream_arn = _kvs_arn(stream_name)

        mock_connect = MagicMock()
        mock_connect.list_instance_storage_configs.return_value = {
            "StorageConfigs": [_make_media_storage_config(stream_arn=stream_arn)]
        }

        mock_kvs = MagicMock()
        mock_kvs.describe_stream.return_value = {
            "StreamInfo": _make_kvs_stream_info(stream_name)
        }
        mock_kvs.list_streams.return_value = {"StreamInfoList": []}

        mock_lambda = MagicMock()
        mock_lambda.list_event_source_mappings.return_value = {
            "EventSourceMappings": []
        }

        def side_effect(service, region):
            return {
                "connect": mock_connect,
                "kinesisvideo": mock_kvs,
                "lambda": mock_lambda,
            }[service]

        mock_create_client.side_effect = side_effect

        resources, lambda_arns = discover_kvs_resources(INSTANCE_ID, REGION)

        assert len(resources) == 1
        assert resources[0].name == stream_name
        assert resources[0].resource_type == ResourceType.KINESIS_VIDEO_STREAM
        assert resources[0].data_retention_in_hours == 24

    @patch("discovery.kvs_discovery.create_source_client")
    def test_prefix_based_discovery(self, mock_create_client):
        """Discover KVS streams via prefix-based listing."""
        stream_name = "connect-kvs-call-123"
        stream_arn = _kvs_arn(stream_name)

        mock_connect = MagicMock()
        mock_connect.list_instance_storage_configs.return_value = {
            "StorageConfigs": [_make_media_storage_config(prefix="connect-kvs-")]
        }

        mock_kvs = MagicMock()
        mock_kvs.list_streams.return_value = {
            "StreamInfoList": [{"StreamARN": stream_arn, "StreamName": stream_name}]
        }
        mock_kvs.describe_stream.return_value = {
            "StreamInfo": _make_kvs_stream_info(stream_name)
        }

        mock_lambda = MagicMock()
        mock_lambda.list_event_source_mappings.return_value = {
            "EventSourceMappings": []
        }

        def side_effect(service, region):
            return {
                "connect": mock_connect,
                "kinesisvideo": mock_kvs,
                "lambda": mock_lambda,
            }[service]

        mock_create_client.side_effect = side_effect

        resources, lambda_arns = discover_kvs_resources(INSTANCE_ID, REGION)

        assert len(resources) == 1
        assert resources[0].name == stream_name

    @patch("discovery.kvs_discovery.create_source_client")
    def test_deduplicates_streams(self, mock_create_client):
        """Same stream found via direct ARN and prefix should appear only once."""
        stream_name = "connect-kvs-stream"
        stream_arn = _kvs_arn(stream_name)

        mock_connect = MagicMock()
        mock_connect.list_instance_storage_configs.return_value = {
            "StorageConfigs": [
                _make_media_storage_config(
                    prefix="connect-kvs-", stream_arn=stream_arn
                )
            ]
        }

        mock_kvs = MagicMock()
        # Prefix listing returns the same stream
        mock_kvs.list_streams.return_value = {
            "StreamInfoList": [{"StreamARN": stream_arn, "StreamName": stream_name}]
        }
        mock_kvs.describe_stream.return_value = {
            "StreamInfo": _make_kvs_stream_info(stream_name)
        }

        mock_lambda = MagicMock()
        mock_lambda.list_event_source_mappings.return_value = {
            "EventSourceMappings": []
        }

        def side_effect(service, region):
            return {
                "connect": mock_connect,
                "kinesisvideo": mock_kvs,
                "lambda": mock_lambda,
            }[service]

        mock_create_client.side_effect = side_effect

        resources, _ = discover_kvs_resources(INSTANCE_ID, REGION)

        assert len(resources) == 1
        # DescribeStream should only be called once
        mock_kvs.describe_stream.assert_called_once()

    @patch("discovery.kvs_discovery.create_source_client")
    def test_describe_stream_failure_skips_resource(self, mock_create_client):
        """If DescribeStream fails, the stream should be skipped gracefully."""
        good_name = "good-stream"
        bad_name = "bad-stream"
        good_arn = _kvs_arn(good_name)
        bad_arn = _kvs_arn(bad_name)

        mock_connect = MagicMock()
        mock_connect.list_instance_storage_configs.return_value = {
            "StorageConfigs": [
                _make_media_storage_config(stream_arn=good_arn),
                _make_media_storage_config(stream_arn=bad_arn, prefix=""),
            ]
        }

        mock_kvs = MagicMock()

        def describe_side_effect(StreamARN):
            if StreamARN == bad_arn:
                raise RuntimeError("Access denied")
            return {"StreamInfo": _make_kvs_stream_info(good_name)}

        mock_kvs.describe_stream.side_effect = describe_side_effect
        mock_kvs.list_streams.return_value = {"StreamInfoList": []}

        mock_lambda = MagicMock()
        mock_lambda.list_event_source_mappings.return_value = {
            "EventSourceMappings": []
        }

        def side_effect(service, region):
            return {
                "connect": mock_connect,
                "kinesisvideo": mock_kvs,
                "lambda": mock_lambda,
            }[service]

        mock_create_client.side_effect = side_effect

        resources, _ = discover_kvs_resources(INSTANCE_ID, REGION)

        assert len(resources) == 1
        assert resources[0].name == good_name

    @patch("discovery.kvs_discovery.create_source_client")
    def test_list_storage_configs_failure_returns_empty(self, mock_create_client):
        """If ListInstanceStorageConfigs fails, return empty results."""
        mock_connect = MagicMock()
        mock_connect.list_instance_storage_configs.side_effect = RuntimeError(
            "Connect API error"
        )
        mock_kvs = MagicMock()
        mock_lambda = MagicMock()

        def side_effect(service, region):
            return {
                "connect": mock_connect,
                "kinesisvideo": mock_kvs,
                "lambda": mock_lambda,
            }[service]

        mock_create_client.side_effect = side_effect

        resources, lambda_arns = discover_kvs_resources(INSTANCE_ID, REGION)

        assert resources == []
        assert lambda_arns == []

    @patch("discovery.kvs_discovery.create_source_client")
    def test_lambda_consumers_discovered(self, mock_create_client):
        """Lambda consumers of KVS streams should be returned for cross-discovery."""
        stream_name = "kvs-stream"
        stream_arn = _kvs_arn(stream_name)
        consumer_arn = _lambda_arn("kvs-processor")

        mock_connect = MagicMock()
        mock_connect.list_instance_storage_configs.return_value = {
            "StorageConfigs": [_make_media_storage_config(stream_arn=stream_arn)]
        }

        mock_kvs = MagicMock()
        mock_kvs.describe_stream.return_value = {
            "StreamInfo": _make_kvs_stream_info(stream_name)
        }
        mock_kvs.list_streams.return_value = {"StreamInfoList": []}

        mock_lambda = MagicMock()
        mock_lambda.list_event_source_mappings.return_value = {
            "EventSourceMappings": [{"FunctionArn": consumer_arn}]
        }

        def side_effect(service, region):
            return {
                "connect": mock_connect,
                "kinesisvideo": mock_kvs,
                "lambda": mock_lambda,
            }[service]

        mock_create_client.side_effect = side_effect

        resources, lambda_arns = discover_kvs_resources(INSTANCE_ID, REGION)

        assert len(resources) == 1
        assert lambda_arns == [consumer_arn]

    @patch("discovery.kvs_discovery.create_source_client")
    def test_encrypted_stream_with_kms(self, mock_create_client):
        """Encrypted KVS stream should have encryption_type set."""
        stream_name = "encrypted-kvs"
        stream_arn = _kvs_arn(stream_name)

        mock_connect = MagicMock()
        mock_connect.list_instance_storage_configs.return_value = {
            "StorageConfigs": [
                _make_media_storage_config(
                    stream_arn=stream_arn,
                    kms_key_id="arn:aws:kms:us-west-2:123456789012:key/abc",
                )
            ]
        }

        mock_kvs = MagicMock()
        mock_kvs.describe_stream.return_value = {
            "StreamInfo": _make_kvs_stream_info(
                stream_name,
                kms_key_id="arn:aws:kms:us-west-2:123456789012:key/abc",
            )
        }
        mock_kvs.list_streams.return_value = {"StreamInfoList": []}

        mock_lambda = MagicMock()
        mock_lambda.list_event_source_mappings.return_value = {
            "EventSourceMappings": []
        }

        def side_effect(service, region):
            return {
                "connect": mock_connect,
                "kinesisvideo": mock_kvs,
                "lambda": mock_lambda,
            }[service]

        mock_create_client.side_effect = side_effect

        resources, _ = discover_kvs_resources(INSTANCE_ID, REGION)

        assert len(resources) == 1
        assert resources[0].encryption_type == "KMS"
        assert resources[0].config_summary["encryption"] == "KMS"

    @patch("discovery.kvs_discovery.create_source_client")
    def test_pagination_of_storage_configs(self, mock_create_client):
        """Pagination of ListInstanceStorageConfigs should be handled."""
        stream1_arn = _kvs_arn("stream-1")
        stream2_arn = _kvs_arn("stream-2")

        mock_connect = MagicMock()
        mock_connect.list_instance_storage_configs.side_effect = [
            {
                "StorageConfigs": [_make_media_storage_config(stream_arn=stream1_arn)],
                "NextToken": "page2",
            },
            {
                "StorageConfigs": [_make_media_storage_config(stream_arn=stream2_arn, prefix="")],
            },
        ]

        mock_kvs = MagicMock()
        mock_kvs.describe_stream.side_effect = [
            {"StreamInfo": _make_kvs_stream_info("stream-1")},
            {"StreamInfo": _make_kvs_stream_info("stream-2")},
        ]
        mock_kvs.list_streams.return_value = {"StreamInfoList": []}

        mock_lambda = MagicMock()
        mock_lambda.list_event_source_mappings.return_value = {
            "EventSourceMappings": []
        }

        def side_effect(service, region):
            return {
                "connect": mock_connect,
                "kinesisvideo": mock_kvs,
                "lambda": mock_lambda,
            }[service]

        mock_create_client.side_effect = side_effect

        resources, _ = discover_kvs_resources(INSTANCE_ID, REGION)

        assert len(resources) == 2
        names = {r.name for r in resources}
        assert names == {"stream-1", "stream-2"}

    @patch("discovery.kvs_discovery.create_source_client")
    def test_multiple_configs_multiple_streams(self, mock_create_client):
        """Multiple storage configs with different streams should all be discovered."""
        stream1_arn = _kvs_arn("media-stream-1")
        stream2_name = "connect-kvs-call-456"
        stream2_arn = _kvs_arn(stream2_name)

        mock_connect = MagicMock()
        mock_connect.list_instance_storage_configs.return_value = {
            "StorageConfigs": [
                _make_media_storage_config(stream_arn=stream1_arn),
                _make_media_storage_config(prefix="connect-kvs-"),
            ]
        }

        mock_kvs = MagicMock()
        mock_kvs.list_streams.return_value = {
            "StreamInfoList": [{"StreamARN": stream2_arn, "StreamName": stream2_name}]
        }
        mock_kvs.describe_stream.side_effect = [
            {"StreamInfo": _make_kvs_stream_info("media-stream-1")},
            {"StreamInfo": _make_kvs_stream_info(stream2_name)},
        ]

        mock_lambda = MagicMock()
        mock_lambda.list_event_source_mappings.return_value = {
            "EventSourceMappings": []
        }

        def side_effect(service, region):
            return {
                "connect": mock_connect,
                "kinesisvideo": mock_kvs,
                "lambda": mock_lambda,
            }[service]

        mock_create_client.side_effect = side_effect

        resources, _ = discover_kvs_resources(INSTANCE_ID, REGION)

        assert len(resources) == 2
