"""Tests for approved origins discovery module."""

import unittest
from unittest.mock import MagicMock, patch

from discovery.approved_origins_discovery import (
    discover_approved_origins,
    _generate_resource_id,
)


class TestGenerateResourceId(unittest.TestCase):
    def test_deterministic(self):
        rid1 = _generate_resource_id("https://example.com")
        rid2 = _generate_resource_id("https://example.com")
        self.assertEqual(rid1, rid2)
        self.assertTrue(rid1.startswith("ao-"))

    def test_different_urls_different_ids(self):
        rid1 = _generate_resource_id("https://a.com")
        rid2 = _generate_resource_id("https://b.com")
        self.assertNotEqual(rid1, rid2)


class TestDiscoverApprovedOrigins(unittest.TestCase):
    @patch("discovery.approved_origins_discovery.create_source_client")
    def test_discovers_origins(self, mock_client_factory):
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client

        mock_paginator = MagicMock()
        mock_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Origins": ["https://app1.example.com", "https://app2.example.com"]},
        ]

        results = discover_approved_origins("inst-123", "us-east-1")

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].origin_url, "https://app1.example.com")
        self.assertEqual(results[0].name, "https://app1.example.com")
        self.assertEqual(results[0].resource_type, "APPROVED_ORIGIN")
        self.assertEqual(results[1].origin_url, "https://app2.example.com")
        mock_client.get_paginator.assert_called_once_with("list_approved_origins")

    @patch("discovery.approved_origins_discovery.create_source_client")
    def test_empty_origins(self, mock_client_factory):
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client

        mock_paginator = MagicMock()
        mock_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{"Origins": []}]

        results = discover_approved_origins("inst-123", "us-east-1")
        self.assertEqual(len(results), 0)

    @patch("discovery.approved_origins_discovery.create_source_client")
    def test_handles_api_error(self, mock_client_factory):
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client

        mock_paginator = MagicMock()
        mock_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.side_effect = Exception("AccessDenied")

        results = discover_approved_origins("inst-123", "us-east-1")
        self.assertEqual(len(results), 0)

    @patch("discovery.approved_origins_discovery.create_source_client")
    def test_multiple_pages(self, mock_client_factory):
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client

        mock_paginator = MagicMock()
        mock_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Origins": ["https://a.com"]},
            {"Origins": ["https://b.com", "https://c.com"]},
        ]

        results = discover_approved_origins("inst-123", "us-east-1")
        self.assertEqual(len(results), 3)


if __name__ == "__main__":
    unittest.main()
