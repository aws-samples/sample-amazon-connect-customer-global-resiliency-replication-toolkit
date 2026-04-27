"""Unit tests for S3 bucket replication including policy, CORS, and lifecycle."""

from unittest.mock import MagicMock, patch, call

import pytest
from botocore.exceptions import ClientError

from models.resources import S3BucketResource
from replication.s3_replication import (
    replicate_s3_bucket,
    _replicate_bucket_policy,
    _replicate_cors_configuration,
    _replicate_lifecycle_rules,
)


TARGET_REGION = "us-east-1"
SOURCE_REGION = "us-west-2"


def _make_s3_resource(
    name: str = "my-bucket",
    encryption_type: str | None = "AES256",
    versioning_enabled: bool = False,
) -> S3BucketResource:
    return S3BucketResource(
        id="s3-1",
        name=name,
        arn=f"arn:aws:s3:::{name}",
        bucket_region=SOURCE_REGION,
        storage_types=["CALL_RECORDINGS"],
        encryption_type=encryption_type,
        versioning_enabled=versioning_enabled,
    )


def _client_error(code: str, message: str = "error") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "TestOperation",
    )


class TestReplicateBucketPolicy:
    @patch("replication.s3_replication._get_source_region_from_bucket", return_value=SOURCE_REGION)
    @patch("replication.s3_replication.create_target_client")
    @patch("replication.s3_replication.create_source_client")
    def test_replicates_policy_with_bucket_name_rewriting(
        self, mock_src, mock_tgt, mock_region
    ):
        mock_s3_src = MagicMock()
        mock_src.return_value = mock_s3_src
        mock_s3_tgt = MagicMock()
        mock_tgt.return_value = mock_s3_tgt

        policy = '{"Statement":[{"Resource":"arn:aws:s3:::my-bucket/*"}]}'
        mock_s3_src.get_bucket_policy.return_value = {"Policy": policy}

        _replicate_bucket_policy("my-bucket", "my-bucket-dr", TARGET_REGION)

        mock_s3_tgt.put_bucket_policy.assert_called_once()
        put_args = mock_s3_tgt.put_bucket_policy.call_args
        assert "my-bucket-dr" in put_args[1]["Policy"]
        assert "my-bucket/*" not in put_args[1]["Policy"]

    @patch("replication.s3_replication._get_source_region_from_bucket", return_value=SOURCE_REGION)
    @patch("replication.s3_replication.create_target_client")
    @patch("replication.s3_replication.create_source_client")
    def test_no_policy_does_nothing(self, mock_src, mock_tgt, mock_region):
        mock_s3_src = MagicMock()
        mock_src.return_value = mock_s3_src
        mock_s3_tgt = MagicMock()
        mock_tgt.return_value = mock_s3_tgt

        mock_s3_src.get_bucket_policy.side_effect = _client_error("NoSuchBucketPolicy")
        mock_s3_src.exceptions.ClientError = ClientError

        _replicate_bucket_policy("my-bucket", "my-bucket-dr", TARGET_REGION)

        mock_s3_tgt.put_bucket_policy.assert_not_called()


class TestReplicateCorsConfiguration:
    @patch("replication.s3_replication._get_source_region_from_bucket", return_value=SOURCE_REGION)
    @patch("replication.s3_replication.create_target_client")
    @patch("replication.s3_replication.create_source_client")
    def test_replicates_cors_rules(self, mock_src, mock_tgt, mock_region):
        mock_s3_src = MagicMock()
        mock_src.return_value = mock_s3_src
        mock_s3_tgt = MagicMock()
        mock_tgt.return_value = mock_s3_tgt

        cors_rules = [
            {"AllowedOrigins": ["*"], "AllowedMethods": ["GET", "PUT"]},
        ]
        mock_s3_src.get_bucket_cors.return_value = {"CORSRules": cors_rules}

        _replicate_cors_configuration("my-bucket", "my-bucket-dr", TARGET_REGION)

        mock_s3_tgt.put_bucket_cors.assert_called_once_with(
            Bucket="my-bucket-dr",
            CORSConfiguration={"CORSRules": cors_rules},
        )

    @patch("replication.s3_replication._get_source_region_from_bucket", return_value=SOURCE_REGION)
    @patch("replication.s3_replication.create_target_client")
    @patch("replication.s3_replication.create_source_client")
    def test_no_cors_does_nothing(self, mock_src, mock_tgt, mock_region):
        mock_s3_src = MagicMock()
        mock_src.return_value = mock_s3_src
        mock_s3_tgt = MagicMock()
        mock_tgt.return_value = mock_s3_tgt

        mock_s3_src.get_bucket_cors.side_effect = _client_error("NoSuchCORSConfiguration")
        mock_s3_src.exceptions.ClientError = ClientError

        _replicate_cors_configuration("my-bucket", "my-bucket-dr", TARGET_REGION)

        mock_s3_tgt.put_bucket_cors.assert_not_called()


class TestReplicateLifecycleRules:
    @patch("replication.s3_replication._get_source_region_from_bucket", return_value=SOURCE_REGION)
    @patch("replication.s3_replication.create_target_client")
    @patch("replication.s3_replication.create_source_client")
    def test_replicates_lifecycle_rules(self, mock_src, mock_tgt, mock_region):
        mock_s3_src = MagicMock()
        mock_src.return_value = mock_s3_src
        mock_s3_tgt = MagicMock()
        mock_tgt.return_value = mock_s3_tgt

        rules = [
            {"ID": "expire-old", "Status": "Enabled", "Expiration": {"Days": 90}},
        ]
        mock_s3_src.get_bucket_lifecycle_configuration.return_value = {"Rules": rules}

        _replicate_lifecycle_rules("my-bucket", "my-bucket-dr", TARGET_REGION)

        mock_s3_tgt.put_bucket_lifecycle_configuration.assert_called_once_with(
            Bucket="my-bucket-dr",
            LifecycleConfiguration={"Rules": rules},
        )

    @patch("replication.s3_replication._get_source_region_from_bucket", return_value=SOURCE_REGION)
    @patch("replication.s3_replication.create_target_client")
    @patch("replication.s3_replication.create_source_client")
    def test_no_lifecycle_does_nothing(self, mock_src, mock_tgt, mock_region):
        mock_s3_src = MagicMock()
        mock_src.return_value = mock_s3_src
        mock_s3_tgt = MagicMock()
        mock_tgt.return_value = mock_s3_tgt

        mock_s3_src.get_bucket_lifecycle_configuration.side_effect = _client_error(
            "NoSuchLifecycleConfiguration"
        )
        mock_s3_src.exceptions.ClientError = ClientError

        _replicate_lifecycle_rules("my-bucket", "my-bucket-dr", TARGET_REGION)

        mock_s3_tgt.put_bucket_lifecycle_configuration.assert_not_called()


class TestReplicateS3BucketIntegration:
    @patch("replication.s3_replication._replicate_lifecycle_rules")
    @patch("replication.s3_replication._replicate_cors_configuration")
    @patch("replication.s3_replication._replicate_bucket_policy")
    @patch("replication.s3_replication.create_target_client")
    def test_full_replication_calls_all_config_replicators(
        self, mock_tgt, mock_policy, mock_cors, mock_lifecycle
    ):
        mock_s3 = MagicMock()
        mock_tgt.return_value = mock_s3
        # Bucket doesn't exist
        mock_s3.head_bucket.side_effect = _client_error("404")
        mock_s3.exceptions.ClientError = ClientError

        resource = _make_s3_resource()
        result = replicate_s3_bucket(resource, TARGET_REGION, resource_tags={})

        assert result == "arn:aws:s3:::my-bucket-dr"
        mock_s3.create_bucket.assert_called_once()
        mock_policy.assert_called_once_with("my-bucket", "my-bucket-dr", TARGET_REGION)
        mock_cors.assert_called_once_with("my-bucket", "my-bucket-dr", TARGET_REGION)
        mock_lifecycle.assert_called_once_with("my-bucket", "my-bucket-dr", TARGET_REGION)
