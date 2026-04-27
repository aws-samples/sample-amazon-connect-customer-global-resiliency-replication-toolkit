"""Unit tests for the AWS client factory."""

from __future__ import annotations

import os
from unittest.mock import patch, MagicMock

import pytest

from aws.client_factory import (
    create_client,
    create_source_client,
    create_target_client,
    get_deployment_mode,
)


# ---------------------------------------------------------------------------
# get_deployment_mode
# ---------------------------------------------------------------------------

class TestGetDeploymentMode:
    def test_defaults_to_local(self):
        with patch.dict(os.environ, {}, clear=True):
            # Remove DEPLOYMENT_MODE if present
            os.environ.pop("DEPLOYMENT_MODE", None)
            assert get_deployment_mode() == "local"

    def test_returns_lambda_when_set(self):
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "lambda"}):
            assert get_deployment_mode() == "lambda"

    def test_returns_local_when_set(self):
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "local"}):
            assert get_deployment_mode() == "local"

    def test_case_insensitive(self):
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "Lambda"}):
            assert get_deployment_mode() == "lambda"


# ---------------------------------------------------------------------------
# create_client
# ---------------------------------------------------------------------------

class TestCreateClient:
    @patch("aws.client_factory.boto3.client")
    def test_creates_client_with_service_and_region(self, mock_boto_client):
        mock_boto_client.return_value = MagicMock()
        client = create_client("connect", "us-west-2")
        mock_boto_client.assert_called_once_with("connect", region_name="us-west-2")
        assert client is mock_boto_client.return_value

    @patch("aws.client_factory.boto3.client")
    def test_no_explicit_credentials_in_lambda_mode(self, mock_boto_client):
        """In Lambda mode, create_client must NOT pass aws_access_key_id or
        aws_secret_access_key — the execution role provides credentials."""
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "lambda"}):
            create_client("lambda", "us-east-1")
            _args, kwargs = mock_boto_client.call_args
            assert "aws_access_key_id" not in kwargs
            assert "aws_secret_access_key" not in kwargs
            assert "aws_session_token" not in kwargs

    @patch("aws.client_factory.boto3.client")
    def test_no_explicit_credentials_in_local_mode(self, mock_boto_client):
        """In local mode, create_client also must NOT pass explicit credentials
        — it relies on the default boto3 credential chain."""
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "local"}):
            create_client("dynamodb", "eu-west-2")
            _args, kwargs = mock_boto_client.call_args
            assert "aws_access_key_id" not in kwargs
            assert "aws_secret_access_key" not in kwargs
            assert "aws_session_token" not in kwargs

    @patch("aws.client_factory.boto3.client")
    def test_passes_region_name(self, mock_boto_client):
        create_client("sts", "ap-northeast-1")
        _args, kwargs = mock_boto_client.call_args
        assert kwargs["region_name"] == "ap-northeast-1"


# ---------------------------------------------------------------------------
# create_source_client / create_target_client
# ---------------------------------------------------------------------------

class TestConvenienceFunctions:
    @patch("aws.client_factory.create_client")
    def test_create_source_client_delegates(self, mock_create):
        mock_create.return_value = MagicMock()
        result = create_source_client("lambda", "us-west-2")
        mock_create.assert_called_once_with("lambda", "us-west-2")
        assert result is mock_create.return_value

    @patch("aws.client_factory.create_client")
    def test_create_target_client_delegates(self, mock_create):
        mock_create.return_value = MagicMock()
        result = create_target_client("dynamodb", "us-east-1")
        mock_create.assert_called_once_with("dynamodb", "us-east-1")
        assert result is mock_create.return_value
