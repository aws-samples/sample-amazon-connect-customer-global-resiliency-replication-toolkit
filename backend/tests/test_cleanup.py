"""Tests for the resource cleanup module."""

import pytest
from unittest.mock import patch, MagicMock

from cleanup.resource_cleanup import (
    cleanup_replicated_resources,
    CleanupResult,
)
from models.enums import ReplicationStatus, ResourceType
from models.resources import ResourceBase


def _make_resource(name, rtype, status=ReplicationStatus.REPLICATED, replicated_arn=None):
    return ResourceBase(
        id=f"id-{name}",
        name=name,
        arn=f"arn:aws:test:us-east-1:123456789012:{name}",
        resource_type=rtype,
        status=status,
        replicated_arn=replicated_arn or f"arn:aws:test:us-west-2:123456789012:{name}",
    )


class TestCleanupReplicatedResources:
    """Tests for cleanup_replicated_resources."""

    def test_empty_inventory_returns_empty_result(self):
        result = cleanup_replicated_resources({}, "us-west-2")
        assert result.deleted_count == 0
        assert result.failed_count == 0
        assert len(result.entries) == 0

    def test_no_replicated_resources_returns_empty(self):
        inventory = {
            "r1": _make_resource("fn1", ResourceType.LAMBDA, status=ReplicationStatus.NOT_REPLICATED, replicated_arn=None),
        }
        # Need to clear replicated_arn since NOT_REPLICATED shouldn't have one
        inventory["r1"].replicated_arn = None
        result = cleanup_replicated_resources(inventory, "us-west-2")
        assert result.deleted_count == 0
        assert len(result.entries) == 0

    @patch("cleanup.resource_cleanup.create_target_client")
    def test_successful_lambda_cleanup(self, mock_create_client):
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client

        inventory = {
            "r1": _make_resource(
                "my-function", ResourceType.LAMBDA,
                replicated_arn="arn:aws:lambda:us-west-2:123456789012:function:my-function",
            ),
        }

        result = cleanup_replicated_resources(inventory, "us-west-2")
        assert result.deleted_count == 1
        assert result.failed_count == 0
        mock_client.delete_function.assert_called_once()

    @patch("cleanup.resource_cleanup.create_target_client")
    def test_failed_cleanup_records_error(self, mock_create_client):
        mock_client = MagicMock()
        mock_client.delete_function.side_effect = Exception("Access denied")
        mock_create_client.return_value = mock_client

        inventory = {
            "r1": _make_resource(
                "my-function", ResourceType.LAMBDA,
                replicated_arn="arn:aws:lambda:us-west-2:123456789012:function:my-function",
            ),
        }

        result = cleanup_replicated_resources(inventory, "us-west-2")
        assert result.deleted_count == 0
        assert result.failed_count == 1
        assert "Access denied" in result.entries[0].error

    @patch("cleanup.resource_cleanup.create_target_client")
    def test_cleanup_processes_in_reverse_dependency_order(self, mock_create_client):
        """Lex bots should be deleted before Lambda functions."""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client

        call_order = []
        def track_delete_function(**kwargs):
            call_order.append("lambda")
        def track_delete_bot(**kwargs):
            call_order.append("lex")

        mock_client.delete_function.side_effect = track_delete_function
        mock_client.delete_bot.side_effect = track_delete_bot

        inventory = {
            "r1": _make_resource(
                "my-function", ResourceType.LAMBDA,
                replicated_arn="arn:aws:lambda:us-west-2:123456789012:function:my-function",
            ),
            "r2": _make_resource(
                "my-bot", ResourceType.LEX_BOT,
                replicated_arn="arn:aws:lex:us-west-2:123456789012:bot/BOT123",
            ),
        }

        result = cleanup_replicated_resources(inventory, "us-west-2")
        # Lex should be deleted before Lambda
        assert call_order[0] == "lex"
        assert call_order[1] == "lambda"

    @patch("cleanup.resource_cleanup.create_target_client")
    @patch("cleanup.resource_cleanup.create_client")
    def test_cleanup_multiple_resource_types(self, mock_create_global, mock_create_target):
        mock_client = MagicMock()
        mock_create_target.return_value = mock_client
        mock_create_global.return_value = mock_client

        inventory = {
            "r1": _make_resource(
                "my-function", ResourceType.LAMBDA,
                replicated_arn="arn:aws:lambda:us-west-2:123456789012:function:my-function",
            ),
            "r2": _make_resource(
                "my-stream", ResourceType.KINESIS_STREAM,
                replicated_arn="arn:aws:kinesis:us-west-2:123456789012:stream/my-stream",
            ),
        }

        result = cleanup_replicated_resources(inventory, "us-west-2")
        assert result.deleted_count == 2
        assert result.failed_count == 0


    @patch("cleanup.resource_cleanup.create_target_client")
    def test_iam_roles_are_skipped(self, mock_create_client):
        """IAM roles should never be deleted."""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client

        inventory = {
            "r1": _make_resource(
                "my-role", ResourceType.IAM_ROLE,
                replicated_arn="arn:aws:iam::123456789012:role/my-role",
            ),
        }

        result = cleanup_replicated_resources(inventory, "us-west-2")
        assert result.deleted_count == 0
        assert result.failed_count == 0
        assert len(result.entries) == 0

    @patch("cleanup.resource_cleanup.create_target_client")
    def test_filter_by_resource_ids(self, mock_create_client):
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client

        inventory = {
            "r1": _make_resource(
                "fn1", ResourceType.LAMBDA,
                replicated_arn="arn:aws:lambda:us-west-2:123456789012:function:fn1",
            ),
            "r2": _make_resource(
                "fn2", ResourceType.LAMBDA,
                replicated_arn="arn:aws:lambda:us-west-2:123456789012:function:fn2",
            ),
        }

        result = cleanup_replicated_resources(inventory, "us-west-2", resource_ids=["r1"])
        assert result.deleted_count == 1
        assert result.entries[0].resource_name == "fn1"


class TestKvsCleanup:
    """Tests for KVS-specific cleanup behavior."""

    @patch("cleanup.resource_cleanup.create_target_client")
    def test_kvs_synthetic_arn_skips_delete_stream(self, mock_create_client):
        """KVS with synthetic config ARN should NOT call delete_stream."""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client

        synthetic_arn = (
            "arn:aws:kinesisvideo:us-west-2:123456789012"
            ":config/media-streams/f4d29ac8-fcfc-4cc1-be06-545ac29aefe9/connect-media-streams"
        )
        inventory = {
            "r1": _make_resource(
                "media-streams-config", ResourceType.KINESIS_VIDEO_STREAM,
                replicated_arn=synthetic_arn,
            ),
        }

        result = cleanup_replicated_resources(inventory, "us-west-2")
        assert result.deleted_count == 1
        assert result.failed_count == 0
        # delete_stream should NOT have been called
        mock_client.delete_stream.assert_not_called()

    @patch("cleanup.resource_cleanup.create_target_client")
    def test_kvs_real_stream_arn_calls_delete_stream(self, mock_create_client):
        """KVS with a real stream ARN should call delete_stream."""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client

        real_arn = "arn:aws:kinesisvideo:us-west-2:123456789012:stream/my-kvs-stream/1234567890"
        inventory = {
            "r1": _make_resource(
                "my-kvs-stream", ResourceType.KINESIS_VIDEO_STREAM,
                replicated_arn=real_arn,
            ),
        }

        result = cleanup_replicated_resources(inventory, "us-west-2")
        assert result.deleted_count == 1
        mock_client.delete_stream.assert_called_once_with(StreamARN=real_arn)

    @patch("cleanup.resource_cleanup.create_target_client")
    def test_kvs_cleanup_with_disassociation(self, mock_create_client):
        """KVS cleanup with instance_id should disassociate MEDIA_STREAMS then skip delete."""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client

        # Mock list_instance_storage_configs to return a MEDIA_STREAMS config
        mock_client.list_instance_storage_configs.return_value = {
            "StorageConfigs": [
                {"AssociationId": "assoc-123", "StorageType": "KINESIS_VIDEO_STREAM"}
            ]
        }

        synthetic_arn = (
            "arn:aws:kinesisvideo:us-west-2:123456789012"
            ":config/media-streams/inst-id/prefix"
        )
        inventory = {
            "r1": _make_resource(
                "media-config", ResourceType.KINESIS_VIDEO_STREAM,
                replicated_arn=synthetic_arn,
            ),
        }

        result = cleanup_replicated_resources(
            inventory, "us-west-2", instance_id="inst-id"
        )
        assert result.deleted_count == 1
        assert result.failed_count == 0
        # Should have disassociated the storage config
        mock_client.disassociate_instance_storage_config.assert_called_once()
        # Should NOT have called delete_stream
        mock_client.delete_stream.assert_not_called()


class TestStorageConfigDisassociation:
    """Tests for storage config type mapping completeness."""

    @patch("cleanup.resource_cleanup.create_target_client")
    def test_kinesis_stream_checks_both_storage_types(self, mock_create_client):
        """Kinesis stream should check both AGENT_EVENTS and CONTACT_TRACE_RECORDS."""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.list_instance_storage_configs.return_value = {"StorageConfigs": []}

        inventory = {
            "r1": _make_resource(
                "my-stream", ResourceType.KINESIS_STREAM,
                replicated_arn="arn:aws:kinesis:us-west-2:123456789012:stream/my-stream",
            ),
        }

        cleanup_replicated_resources(inventory, "us-west-2", instance_id="inst-id")

        # Should have called list_instance_storage_configs for both types
        call_args_list = mock_client.list_instance_storage_configs.call_args_list
        resource_types_checked = [
            call.kwargs.get("ResourceType") or call[1].get("ResourceType")
            for call in call_args_list
        ]
        assert "AGENT_EVENTS" in resource_types_checked
        assert "CONTACT_TRACE_RECORDS" in resource_types_checked

    @patch("cleanup.resource_cleanup.create_target_client")
    def test_firehose_checks_both_storage_types(self, mock_create_client):
        """Firehose should check both CONTACT_TRACE_RECORDS and AGENT_EVENTS."""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.list_instance_storage_configs.return_value = {"StorageConfigs": []}

        inventory = {
            "r1": _make_resource(
                "my-firehose", ResourceType.KINESIS_FIREHOSE,
                replicated_arn="arn:aws:firehose:us-west-2:123456789012:deliverystream/my-firehose",
            ),
        }

        cleanup_replicated_resources(inventory, "us-west-2", instance_id="inst-id")

        call_args_list = mock_client.list_instance_storage_configs.call_args_list
        resource_types_checked = [
            call.kwargs.get("ResourceType") or call[1].get("ResourceType")
            for call in call_args_list
        ]
        assert "CONTACT_TRACE_RECORDS" in resource_types_checked
        assert "AGENT_EVENTS" in resource_types_checked

    @patch("cleanup.resource_cleanup.create_target_client")
    def test_s3_checks_all_three_storage_types(self, mock_create_client):
        """S3 should check CALL_RECORDINGS, CHAT_TRANSCRIPTS, and SCHEDULED_REPORTS."""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client
        mock_client.list_instance_storage_configs.return_value = {"StorageConfigs": []}

        inventory = {
            "r1": _make_resource(
                "my-bucket", ResourceType.S3_BUCKET,
                replicated_arn="arn:aws:s3:::my-bucket",
            ),
        }

        cleanup_replicated_resources(inventory, "us-west-2", instance_id="inst-id")

        call_args_list = mock_client.list_instance_storage_configs.call_args_list
        resource_types_checked = [
            call.kwargs.get("ResourceType") or call[1].get("ResourceType")
            for call in call_args_list
        ]
        assert "CALL_RECORDINGS" in resource_types_checked
        assert "CHAT_TRANSCRIPTS" in resource_types_checked
        assert "SCHEDULED_REPORTS" in resource_types_checked


# ---------------------------------------------------------------------------
# Approved origins cleanup
# ---------------------------------------------------------------------------

class TestApprovedOriginsCleanup:
    """Tests for approved origins removal during cleanup."""

    @patch("cleanup.resource_cleanup.create_target_client")
    @patch("cleanup.resource_cleanup.create_client")
    def test_approved_origins_removed_during_cleanup(self, mock_create_client, mock_create_target):
        """Approved origins from source that also exist on target should be disassociated from target."""
        mock_source_connect = MagicMock()
        mock_target_connect = MagicMock()
        mock_create_client.return_value = mock_source_connect
        mock_create_target.return_value = mock_target_connect

        mock_source_connect.list_approved_origins.return_value = {
            "Origins": ["https://example.com", "https://app.example.com"],
        }
        mock_target_connect.list_approved_origins.return_value = {
            "Origins": ["https://example.com", "https://app.example.com"],
        }

        # One Lambda so the cleanup function actually runs
        inventory = {
            "id-func": _make_resource("func", ResourceType.LAMBDA),
        }

        result = cleanup_replicated_resources(
            inventory, "us-west-2",
            source_region="us-east-1",
            instance_id="source-inst-123",
            target_instance_id="target-inst-456",
        )

        # Verify source list_approved_origins used source instance_id
        source_list_call = mock_source_connect.list_approved_origins.call_args
        assert source_list_call.kwargs.get("InstanceId") or source_list_call[1].get("InstanceId") == "source-inst-123"

        # Verify target list_approved_origins used target instance_id
        target_list_call = mock_target_connect.list_approved_origins.call_args
        assert target_list_call.kwargs.get("InstanceId") or target_list_call[1].get("InstanceId") == "target-inst-456"

        # Verify disassociate_approved_origin used target instance_id
        calls = mock_target_connect.disassociate_approved_origin.call_args_list
        origins_removed = {c.kwargs.get("Origin") or c[1].get("Origin") for c in calls}
        assert "https://example.com" in origins_removed
        assert "https://app.example.com" in origins_removed
        for c in calls:
            used_id = c.kwargs.get("InstanceId") or c[1].get("InstanceId")
            assert used_id == "target-inst-456"

        # Check entries include approved origin results
        origin_entries = [e for e in result.entries if e.resource_type == "APPROVED_ORIGIN"]
        assert len(origin_entries) == 2
        assert all(e.deleted for e in origin_entries)

    @patch("cleanup.resource_cleanup.create_target_client")
    @patch("cleanup.resource_cleanup.create_client")
    def test_approved_origins_not_found_is_silent(self, mock_create_client, mock_create_target):
        """If an origin doesn't exist on target, it should be silently skipped."""
        from botocore.exceptions import ClientError

        mock_source_connect = MagicMock()
        mock_target_connect = MagicMock()
        mock_create_client.return_value = mock_source_connect
        mock_create_target.return_value = mock_target_connect

        mock_source_connect.list_approved_origins.return_value = {
            "Origins": ["https://gone.example.com"],
        }
        mock_target_connect.list_approved_origins.return_value = {
            "Origins": ["https://gone.example.com"],
        }
        mock_target_connect.disassociate_approved_origin.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Origin does not exist"}},
            "DisassociateApprovedOrigin",
        )

        result = cleanup_replicated_resources(
            {}, "us-west-2",
            source_region="us-east-1",
            instance_id="source-inst-123",
            target_instance_id="target-inst-456",
        )

        # Should not produce error entries for "not found"
        origin_entries = [e for e in result.entries if e.resource_type == "APPROVED_ORIGIN"]
        assert len(origin_entries) == 0

    @patch("cleanup.resource_cleanup.create_target_client")
    @patch("cleanup.resource_cleanup.create_client")
    def test_approved_origins_skipped_without_instance_id(self, mock_create_client, mock_create_target):
        """Approved origins cleanup should be skipped when no instance_id is provided."""
        mock_create_target.return_value = MagicMock()

        result = cleanup_replicated_resources(
            {}, "us-west-2",
            source_region="us-east-1",
            instance_id="",
        )

        origin_entries = [e for e in result.entries if e.resource_type == "APPROVED_ORIGIN"]
        assert len(origin_entries) == 0

    @patch("cleanup.resource_cleanup.create_target_client")
    @patch("cleanup.resource_cleanup.create_client")
    def test_approved_origins_error_recorded(self, mock_create_client, mock_create_target):
        """Non-'not found' errors should be recorded as failed entries."""
        from botocore.exceptions import ClientError

        mock_source_connect = MagicMock()
        mock_target_connect = MagicMock()
        mock_create_client.return_value = mock_source_connect
        mock_create_target.return_value = mock_target_connect

        mock_source_connect.list_approved_origins.return_value = {
            "Origins": ["https://fail.example.com"],
        }
        mock_target_connect.list_approved_origins.return_value = {
            "Origins": ["https://fail.example.com"],
        }
        mock_target_connect.disassociate_approved_origin.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "No permission"}},
            "DisassociateApprovedOrigin",
        )

        result = cleanup_replicated_resources(
            {}, "us-west-2",
            source_region="us-east-1",
            instance_id="source-inst-123",
            target_instance_id="target-inst-456",
        )

        origin_entries = [e for e in result.entries if e.resource_type == "APPROVED_ORIGIN"]
        assert len(origin_entries) == 1
        assert not origin_entries[0].deleted
        assert "No permission" in origin_entries[0].error

    @patch("cleanup.resource_cleanup.create_target_client")
    @patch("cleanup.resource_cleanup.create_client")
    def test_approved_origins_only_intersection_removed(self, mock_create_client, mock_create_target):
        """Only origins present on both source and target should be disassociated."""
        mock_source_connect = MagicMock()
        mock_target_connect = MagicMock()
        mock_create_client.return_value = mock_source_connect
        mock_create_target.return_value = mock_target_connect

        mock_source_connect.list_approved_origins.return_value = {
            "Origins": ["https://shared.example.com", "https://source-only.example.com"],
        }
        mock_target_connect.list_approved_origins.return_value = {
            "Origins": ["https://shared.example.com", "https://target-only.example.com"],
        }

        result = cleanup_replicated_resources(
            {}, "us-west-2",
            source_region="us-east-1",
            instance_id="source-inst-123",
            target_instance_id="target-inst-456",
        )

        # Only the shared origin should be disassociated
        calls = mock_target_connect.disassociate_approved_origin.call_args_list
        assert len(calls) == 1
        removed_origin = calls[0].kwargs.get("Origin") or calls[0][1].get("Origin")
        assert removed_origin == "https://shared.example.com"

        origin_entries = [e for e in result.entries if e.resource_type == "APPROVED_ORIGIN"]
        assert len(origin_entries) == 1
        assert origin_entries[0].deleted

    @patch("cleanup.resource_cleanup.create_target_client")
    @patch("cleanup.resource_cleanup.create_client")
    def test_approved_origins_fallback_to_instance_id_when_no_target_id(self, mock_create_client, mock_create_target):
        """When target_instance_id is not provided, falls back to instance_id for backward compat."""
        mock_source_connect = MagicMock()
        mock_target_connect = MagicMock()
        mock_create_client.return_value = mock_source_connect
        mock_create_target.return_value = mock_target_connect

        mock_source_connect.list_approved_origins.return_value = {
            "Origins": ["https://example.com"],
        }
        mock_target_connect.list_approved_origins.return_value = {
            "Origins": ["https://example.com"],
        }

        result = cleanup_replicated_resources(
            {}, "us-west-2",
            source_region="us-east-1",
            instance_id="inst-123",
            # target_instance_id not provided — should fall back to instance_id
        )

        # Verify target list_approved_origins used the fallback instance_id
        target_list_call = mock_target_connect.list_approved_origins.call_args
        used_id = target_list_call.kwargs.get("InstanceId") or target_list_call[1].get("InstanceId")
        assert used_id == "inst-123"

        # Verify disassociate used the fallback instance_id
        disassoc_call = mock_target_connect.disassociate_approved_origin.call_args
        used_id = disassoc_call.kwargs.get("InstanceId") or disassoc_call[1].get("InstanceId")
        assert used_id == "inst-123"
