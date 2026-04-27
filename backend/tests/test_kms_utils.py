"""Tests for the KMS utility module."""

import pytest
from unittest.mock import patch, MagicMock, call

from botocore.exceptions import ClientError

from aws.kms_utils import (
    ensure_kms_key_exists,
    _check_alias_exists,
    _resolve_cmk,
    _ensure_aws_managed_key,
    _bootstrap_kvs_kms_key,
    _cleanup_temp_kvs_stream,
)


def _make_client_error(code: str, message: str = "error") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "test_operation",
    )


class TestEnsureKmsKeyExists:
    """Tests for the main ensure_kms_key_exists dispatcher.

    ensure_kms_key_exists now returns a tuple of (key_id, kms_info_dict).
    """

    def test_empty_key_id_returns_empty(self):
        key_id, info = ensure_kms_key_exists("", "us-west-2")
        assert key_id == ""
        assert info["key_type"] == "NONE"
        assert info["action"] == "SKIPPED"

    def test_none_key_id_returns_empty(self):
        key_id, info = ensure_kms_key_exists(None, "us-west-2")
        assert key_id == ""
        assert info["key_type"] == "NONE"

    @patch("aws.kms_utils._resolve_cmk")
    def test_cmk_arn_delegates_to_resolve_cmk(self, mock_resolve):
        mock_resolve.return_value = ""
        key_arn = "arn:aws:kms:us-east-1:123456789012:key/abc-123"
        key_id, info = ensure_kms_key_exists(key_arn, "us-west-2")
        mock_resolve.assert_called_once_with(key_arn, "us-west-2")
        assert key_id == ""
        assert info["key_type"] == "CMK"
        assert info["action"] == "SKIPPED"

    @patch("aws.kms_utils._resolve_cmk")
    def test_key_with_key_slash_delegates_to_resolve_cmk(self, mock_resolve):
        mock_resolve.return_value = "some-key"
        key_id, info = ensure_kms_key_exists("something:key/abc", "us-west-2")
        mock_resolve.assert_called_once()
        assert key_id == "some-key"
        assert info["key_type"] == "CMK"
        assert info["action"] == "REUSED"

    @patch("aws.kms_utils._ensure_aws_managed_key")
    def test_aws_managed_alias_delegates(self, mock_ensure):
        mock_ensure.return_value = ("alias/aws/kinesisvideo", "REUSED")
        key_id, info = ensure_kms_key_exists("alias/aws/kinesisvideo", "us-west-2")
        mock_ensure.assert_called_once_with("alias/aws/kinesisvideo", "us-west-2")
        assert key_id == "alias/aws/kinesisvideo"
        assert info["key_type"] == "AWS_MANAGED"
        assert info["action"] == "REUSED"

    @patch("aws.kms_utils._check_alias_exists")
    def test_custom_alias_delegates(self, mock_check):
        mock_check.return_value = "alias/my-custom-key"
        key_id, info = ensure_kms_key_exists("alias/my-custom-key", "us-west-2")
        mock_check.assert_called_once_with("alias/my-custom-key", "us-west-2")
        assert key_id == "alias/my-custom-key"
        assert info["action"] == "REUSED"

    def test_raw_key_id_returned_as_is(self):
        key_id, info = ensure_kms_key_exists("abc-123-def", "us-west-2")
        assert key_id == "abc-123-def"
        assert info["action"] == "REUSED"


class TestResolveCmk:
    """Tests for customer-managed key resolution."""

    def test_same_region_cmk_returned_as_is(self):
        key_arn = "arn:aws:kms:us-west-2:123456789012:key/abc-123"
        result = _resolve_cmk(key_arn, "us-west-2")
        assert result == key_arn

    def test_cross_region_cmk_returns_empty(self):
        key_arn = "arn:aws:kms:us-east-1:123456789012:key/abc-123"
        result = _resolve_cmk(key_arn, "us-west-2")
        assert result == ""

    def test_malformed_arn_returns_empty(self):
        result = _resolve_cmk("arn:aws:kms", "us-west-2")
        assert result == ""


class TestCheckAliasExists:
    """Tests for alias existence checking."""

    @patch("aws.kms_utils.create_target_client")
    def test_alias_exists(self, mock_create):
        mock_kms = MagicMock()
        mock_create.return_value = mock_kms
        result = _check_alias_exists("alias/my-key", "us-west-2")
        assert result == "alias/my-key"
        mock_kms.describe_key.assert_called_once_with(KeyId="alias/my-key")

    @patch("aws.kms_utils.create_target_client")
    def test_alias_not_found_returns_empty(self, mock_create):
        mock_kms = MagicMock()
        mock_kms.describe_key.side_effect = _make_client_error("NotFoundException")
        mock_create.return_value = mock_kms
        result = _check_alias_exists("alias/my-key", "us-west-2")
        assert result == ""

    @patch("aws.kms_utils.create_target_client")
    def test_throttling_returns_alias_anyway(self, mock_create):
        mock_kms = MagicMock()
        mock_kms.describe_key.side_effect = _make_client_error("ThrottlingException")
        mock_create.return_value = mock_kms
        result = _check_alias_exists("alias/my-key", "us-west-2")
        assert result == "alias/my-key"


class TestEnsureAwsManagedKey:
    """Tests for AWS-managed key bootstrapping.

    _ensure_aws_managed_key now returns a tuple of (key_id, action).
    """

    @patch("aws.kms_utils.create_target_client")
    def test_key_already_exists(self, mock_create):
        mock_kms = MagicMock()
        mock_create.return_value = mock_kms
        key_id, action = _ensure_aws_managed_key("alias/aws/kinesisvideo", "us-west-2")
        assert key_id == "alias/aws/kinesisvideo"
        assert action == "REUSED"

    @patch("aws.kms_utils._bootstrap_kvs_kms_key")
    @patch("aws.kms_utils.create_target_client")
    def test_kvs_key_not_found_triggers_bootstrap(self, mock_create, mock_bootstrap):
        mock_kms = MagicMock()
        mock_kms.describe_key.side_effect = _make_client_error("NotFoundException")
        mock_create.return_value = mock_kms
        mock_bootstrap.return_value = "alias/aws/kinesisvideo"

        key_id, action = _ensure_aws_managed_key("alias/aws/kinesisvideo", "us-west-2")
        assert key_id == "alias/aws/kinesisvideo"
        assert action == "BOOTSTRAPPED"
        mock_bootstrap.assert_called_once_with("us-west-2")

    @patch("aws.kms_utils.create_target_client")
    def test_s3_key_not_found_returns_alias_anyway(self, mock_create):
        mock_kms = MagicMock()
        mock_kms.describe_key.side_effect = _make_client_error("NotFoundException")
        mock_create.return_value = mock_kms
        key_id, action = _ensure_aws_managed_key("alias/aws/s3", "us-west-2")
        assert key_id == "alias/aws/s3"
        assert action == "REUSED"

    @patch("aws.kms_utils.create_target_client")
    def test_connect_key_not_found_returns_empty(self, mock_create):
        mock_kms = MagicMock()
        mock_kms.describe_key.side_effect = _make_client_error("NotFoundException")
        mock_create.return_value = mock_kms
        key_id, action = _ensure_aws_managed_key("alias/aws/connect", "us-west-2")
        assert key_id == ""
        assert action == "SKIPPED"

    @patch("aws.kms_utils.create_target_client")
    def test_unknown_aws_key_not_found_returns_alias(self, mock_create):
        mock_kms = MagicMock()
        mock_kms.describe_key.side_effect = _make_client_error("NotFoundException")
        mock_create.return_value = mock_kms
        key_id, action = _ensure_aws_managed_key("alias/aws/something-else", "us-west-2")
        assert key_id == "alias/aws/something-else"
        assert action == "REUSED"

    @patch("aws.kms_utils.create_target_client")
    def test_throttling_returns_alias(self, mock_create):
        mock_kms = MagicMock()
        mock_kms.describe_key.side_effect = _make_client_error("ThrottlingException")
        mock_create.return_value = mock_kms
        key_id, action = _ensure_aws_managed_key("alias/aws/kinesisvideo", "us-west-2")
        assert key_id == "alias/aws/kinesisvideo"
        assert action == "REUSED"

    @patch("aws.kms_utils._bootstrap_kvs_kms_key")
    @patch("aws.kms_utils.create_target_client")
    def test_kvs_bootstrap_failure_returns_skipped(self, mock_create, mock_bootstrap):
        mock_kms = MagicMock()
        mock_kms.describe_key.side_effect = _make_client_error("NotFoundException")
        mock_create.return_value = mock_kms
        mock_bootstrap.return_value = ""

        key_id, action = _ensure_aws_managed_key("alias/aws/kinesisvideo", "us-west-2")
        assert key_id == ""
        assert action == "SKIPPED"


class TestBootstrapKvsKmsKey:
    """Tests for KVS KMS key bootstrapping via temp stream creation."""

    @patch("aws.kms_utils.create_target_client")
    def test_successful_bootstrap(self, mock_create):
        mock_kvs = MagicMock()
        mock_kms = MagicMock()

        def side_effect(service, region):
            if service == "kinesisvideo":
                return mock_kvs
            return mock_kms

        mock_create.side_effect = side_effect

        # create_stream succeeds, list_streams returns the stream for cleanup
        mock_kvs.list_streams.return_value = {
            "StreamInfoList": [
                {"StreamName": "_acgr-kms-bootstrap-temp", "StreamARN": "arn:aws:kinesisvideo:us-west-2:123:stream/_acgr-kms-bootstrap-temp/123"}
            ]
        }

        result = _bootstrap_kvs_kms_key("us-west-2")
        assert result == "alias/aws/kinesisvideo"
        mock_kvs.create_stream.assert_called_once()
        mock_kvs.delete_stream.assert_called_once()

    @patch("aws.kms_utils.create_target_client")
    def test_stream_already_exists_returns_alias(self, mock_create):
        mock_kvs = MagicMock()
        mock_kvs.create_stream.side_effect = _make_client_error("ResourceInUseException")
        mock_kvs.list_streams.return_value = {
            "StreamInfoList": [
                {"StreamName": "_acgr-kms-bootstrap-temp", "StreamARN": "arn:stream/123"}
            ]
        }
        mock_create.return_value = mock_kvs

        result = _bootstrap_kvs_kms_key("us-west-2")
        assert result == "alias/aws/kinesisvideo"

    @patch("aws.kms_utils.create_target_client")
    def test_create_stream_access_denied_returns_empty(self, mock_create):
        mock_kvs = MagicMock()
        mock_kvs.create_stream.side_effect = _make_client_error("AccessDeniedException")
        mock_create.return_value = mock_kvs

        result = _bootstrap_kvs_kms_key("us-west-2")
        assert result == ""

    @patch("aws.kms_utils.create_target_client")
    def test_create_stream_generic_error_returns_empty(self, mock_create):
        mock_kvs = MagicMock()
        mock_kvs.create_stream.side_effect = Exception("network error")
        mock_create.return_value = mock_kvs

        result = _bootstrap_kvs_kms_key("us-west-2")
        assert result == ""

    @patch("aws.kms_utils.create_target_client")
    def test_cleanup_failure_still_returns_alias(self, mock_create):
        mock_kvs = MagicMock()
        mock_kms = MagicMock()

        def side_effect(service, region):
            if service == "kinesisvideo":
                return mock_kvs
            return mock_kms

        mock_create.side_effect = side_effect
        # create succeeds but cleanup fails
        mock_kvs.list_streams.side_effect = Exception("list failed")

        result = _bootstrap_kvs_kms_key("us-west-2")
        assert result == "alias/aws/kinesisvideo"


class TestCleanupTempKvsStream:
    """Tests for temp KVS stream cleanup."""

    def test_successful_cleanup(self):
        mock_kvs = MagicMock()
        mock_kvs.list_streams.return_value = {
            "StreamInfoList": [
                {"StreamName": "_acgr-kms-bootstrap-temp", "StreamARN": "arn:stream/123"}
            ]
        }
        _cleanup_temp_kvs_stream(mock_kvs, "_acgr-kms-bootstrap-temp")
        mock_kvs.delete_stream.assert_called_once_with(StreamARN="arn:stream/123")

    def test_stream_not_in_list(self):
        mock_kvs = MagicMock()
        mock_kvs.list_streams.return_value = {"StreamInfoList": []}
        _cleanup_temp_kvs_stream(mock_kvs, "_acgr-kms-bootstrap-temp")
        mock_kvs.delete_stream.assert_not_called()

    def test_list_streams_fails_gracefully(self):
        mock_kvs = MagicMock()
        mock_kvs.list_streams.side_effect = Exception("access denied")
        # Should not raise
        _cleanup_temp_kvs_stream(mock_kvs, "_acgr-kms-bootstrap-temp")

    def test_delete_stream_fails_gracefully(self):
        mock_kvs = MagicMock()
        mock_kvs.list_streams.return_value = {
            "StreamInfoList": [
                {"StreamName": "_acgr-kms-bootstrap-temp", "StreamARN": "arn:stream/123"}
            ]
        }
        mock_kvs.delete_stream.side_effect = Exception("delete failed")
        # Should not raise
        _cleanup_temp_kvs_stream(mock_kvs, "_acgr-kms-bootstrap-temp")

    def test_stream_with_no_arn_skipped(self):
        mock_kvs = MagicMock()
        mock_kvs.list_streams.return_value = {
            "StreamInfoList": [
                {"StreamName": "_acgr-kms-bootstrap-temp", "StreamARN": ""}
            ]
        }
        _cleanup_temp_kvs_stream(mock_kvs, "_acgr-kms-bootstrap-temp")
        mock_kvs.delete_stream.assert_not_called()


class TestStorageConfigEncryptionIntegration:
    """Integration tests verifying encryption handling in storage config replication."""

    @patch("aws.kms_utils.create_target_client")
    def test_ensure_kms_key_exists_full_flow_kvs(self, mock_create):
        """Full flow: KVS key not found → bootstrap → success."""
        mock_kms = MagicMock()
        mock_kvs = MagicMock()

        call_count = {"describe": 0}

        def describe_side_effect(**kwargs):
            call_count["describe"] += 1
            if call_count["describe"] == 1:
                raise _make_client_error("NotFoundException")
            return {"KeyMetadata": {"KeyId": "abc"}}

        mock_kms.describe_key.side_effect = describe_side_effect
        mock_kvs.list_streams.return_value = {
            "StreamInfoList": [
                {"StreamName": "_acgr-kms-bootstrap-temp", "StreamARN": "arn:stream/123"}
            ]
        }

        def create_side_effect(service, region):
            if service == "kms":
                return mock_kms
            return mock_kvs

        mock_create.side_effect = create_side_effect

        key_id, info = ensure_kms_key_exists("alias/aws/kinesisvideo", "us-west-2")
        assert key_id == "alias/aws/kinesisvideo"
        assert info["key_type"] == "AWS_MANAGED"
        assert info["action"] == "BOOTSTRAPPED"

    def test_ensure_kms_key_exists_cmk_same_region(self):
        key_arn = "arn:aws:kms:us-west-2:123456789012:key/abc-123"
        key_id, info = ensure_kms_key_exists(key_arn, "us-west-2")
        assert key_id == key_arn
        assert info["key_type"] == "CMK"
        assert info["action"] == "REUSED"

    def test_ensure_kms_key_exists_cmk_cross_region(self):
        key_arn = "arn:aws:kms:us-east-1:123456789012:key/abc-123"
        key_id, info = ensure_kms_key_exists(key_arn, "us-west-2")
        assert key_id == ""
        assert info["key_type"] == "CMK"
        assert info["action"] == "SKIPPED"
        assert "region-specific" in info["message"]
