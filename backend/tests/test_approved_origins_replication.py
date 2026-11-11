"""Tests for approved origins replication module."""

import unittest
from unittest.mock import MagicMock, patch

from models.resources import ApprovedOriginResource
from replication.approved_origins_replication import replicate_approved_origin


class TestReplicateApprovedOrigin(unittest.TestCase):
    @patch("replication.approved_origins_replication.create_target_client")
    def test_replicates_origin(self, mock_client_factory):
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client

        resource = ApprovedOriginResource(
            id="ao-abc123",
            name="https://myapp.example.com",
            arn="arn:aws:connect:us-east-1:approved-origin:https://myapp.example.com",
            origin_url="https://myapp.example.com",
        )

        result = replicate_approved_origin(resource, "us-west-2", "target-inst-456")

        mock_client.associate_approved_origin.assert_called_once_with(
            InstanceId="target-inst-456",
            Origin="https://myapp.example.com",
        )
        self.assertIn("us-west-2", result)
        self.assertIn("https://myapp.example.com", result)

    @patch("replication.approved_origins_replication.create_target_client")
    def test_uses_name_as_fallback(self, mock_client_factory):
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client

        resource = ApprovedOriginResource(
            id="ao-abc123",
            name="https://fallback.example.com",
            arn="arn:aws:connect:us-east-1:approved-origin:https://fallback.example.com",
            origin_url="",
        )

        replicate_approved_origin(resource, "us-west-2", "target-inst-456")

        mock_client.associate_approved_origin.assert_called_once_with(
            InstanceId="target-inst-456",
            Origin="https://fallback.example.com",
        )

    @patch("replication.approved_origins_replication.create_target_client")
    def test_deserialized_resource_base_uses_config_summary(self, mock_client_factory):
        """When deserialized from DynamoDB, resources are plain ResourceBase
        objects without origin_url. The replication code should fall back to
        config_summary['origin_url']."""
        from models.resources import ResourceBase
        from models.enums import ResourceType

        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client

        # Simulate a ResourceBase (no origin_url attribute) as returned from DynamoDB
        resource = ResourceBase(
            id="ao-abc123",
            name="https://www.abc.com",
            arn="arn:aws:connect:us-east-1:approved-origin:https://www.abc.com",
            resource_type=ResourceType.APPROVED_ORIGIN,
            config_summary={"origin_url": "https://www.abc.com"},
        )

        result = replicate_approved_origin(resource, "us-west-2", "target-inst-456")

        mock_client.associate_approved_origin.assert_called_once_with(
            InstanceId="target-inst-456",
            Origin="https://www.abc.com",
        )
        self.assertIn("us-west-2", result)

    @patch("replication.approved_origins_replication.create_target_client")
    def test_raises_on_error(self, mock_client_factory):
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client
        mock_client.associate_approved_origin.side_effect = Exception("Forbidden")

        resource = ApprovedOriginResource(
            id="ao-abc123",
            name="https://myapp.example.com",
            arn="arn:aws:connect:us-east-1:approved-origin:https://myapp.example.com",
            origin_url="https://myapp.example.com",
        )

        with self.assertRaises(Exception):
            replicate_approved_origin(resource, "us-west-2", "target-inst-456")


if __name__ == "__main__":
    unittest.main()
