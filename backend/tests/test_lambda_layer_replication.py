"""Unit tests for Lambda layer replication."""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from models.resources import LambdaResource
from replication.lambda_replication import (
    _rewrite_layer_arns,
    replicate_lambda_layers,
    _replicate_single_layer,
)


TARGET_REGION = "us-east-1"
SOURCE_REGION = "us-west-2"
ACCOUNT_ID = "123456789012"


def _make_lambda(
    name: str = "my-func",
    layers: list[str] | None = None,
) -> LambdaResource:
    return LambdaResource(
        id="lambda-1",
        name=name,
        arn=f"arn:aws:lambda:{SOURCE_REGION}:{ACCOUNT_ID}:function:{name}",
        runtime="python3.12",
        handler="index.handler",
        memory_size=256,
        timeout=30,
        environment={},
        layers=layers or [],
        execution_role_arn=f"arn:aws:iam::{ACCOUNT_ID}:role/{name}-role",
    )


class TestRewriteLayerArnsWithMapping:
    def test_uses_mapping_when_available(self):
        source_arn = f"arn:aws:lambda:{SOURCE_REGION}:{ACCOUNT_ID}:layer:my-layer:1"
        target_arn = f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:layer:my-layer:5"
        mapping = {source_arn: target_arn}

        result = _rewrite_layer_arns([source_arn], TARGET_REGION, layer_arn_mapping=mapping)
        assert result == [target_arn]

    def test_falls_back_to_region_rewrite_without_mapping(self):
        source_arn = f"arn:aws:lambda:{SOURCE_REGION}:{ACCOUNT_ID}:layer:my-layer:1"
        result = _rewrite_layer_arns([source_arn], TARGET_REGION)
        assert result == [f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:layer:my-layer:1"]

    def test_mixed_mapped_and_unmapped(self):
        layer1 = f"arn:aws:lambda:{SOURCE_REGION}:{ACCOUNT_ID}:layer:layer1:1"
        layer2 = f"arn:aws:lambda:{SOURCE_REGION}:{ACCOUNT_ID}:layer:layer2:2"
        target1 = f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:layer:layer1:7"
        mapping = {layer1: target1}

        result = _rewrite_layer_arns([layer1, layer2], TARGET_REGION, layer_arn_mapping=mapping)
        assert result[0] == target1
        assert TARGET_REGION in result[1]

    def test_empty_mapping_falls_back(self):
        source_arn = f"arn:aws:lambda:{SOURCE_REGION}:{ACCOUNT_ID}:layer:my-layer:1"
        result = _rewrite_layer_arns([source_arn], TARGET_REGION, layer_arn_mapping={})
        assert result == [f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:layer:my-layer:1"]


class TestReplicateSingleLayer:
    def test_successful_replication(self):
        source_lambda = MagicMock()
        target_lambda = MagicMock()

        source_arn = f"arn:aws:lambda:{SOURCE_REGION}:{ACCOUNT_ID}:layer:my-layer:1"

        source_lambda.get_layer_version.return_value = {
            "Content": {"Location": "https://example.com/layer.zip"},
            "CompatibleRuntimes": ["python3.12"],
            "CompatibleArchitectures": ["x86_64"],
            "Description": "My layer",
            "LicenseInfo": "MIT",
        }

        target_arn = f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:layer:my-layer:1"
        target_lambda.publish_layer_version.return_value = {
            "LayerVersionArn": target_arn
        }

        with patch("replication.lambda_replication.requests") as mock_requests:
            mock_resp = MagicMock()
            mock_resp.content = b"layer-zip-bytes"
            mock_resp.raise_for_status = MagicMock()
            mock_requests.get.return_value = mock_resp

            result = _replicate_single_layer(
                source_lambda, target_lambda, source_arn, "my-layer", TARGET_REGION
            )

        assert result == target_arn
        target_lambda.publish_layer_version.assert_called_once()
        publish_args = target_lambda.publish_layer_version.call_args[1]
        assert publish_args["LayerName"] == "my-layer"
        assert publish_args["CompatibleRuntimes"] == ["python3.12"]
        assert publish_args["CompatibleArchitectures"] == ["x86_64"]

    def test_invalid_arn_returns_none(self):
        result = _replicate_single_layer(
            MagicMock(), MagicMock(), "arn:bad", "bad", TARGET_REGION
        )
        assert result is None

    def test_get_layer_version_failure_returns_none(self):
        source_lambda = MagicMock()
        source_lambda.get_layer_version.side_effect = Exception("not found")

        source_arn = f"arn:aws:lambda:{SOURCE_REGION}:{ACCOUNT_ID}:layer:my-layer:1"
        result = _replicate_single_layer(
            source_lambda, MagicMock(), source_arn, "my-layer", TARGET_REGION
        )
        assert result is None


class TestReplicateLambdaLayers:
    @patch("replication.lambda_replication.create_target_client")
    @patch("replication.lambda_replication.create_source_client")
    @patch("replication.lambda_replication.requests")
    def test_replicates_unique_layers(self, mock_requests, mock_src, mock_tgt):
        mock_src_lambda = MagicMock()
        mock_src.return_value = mock_src_lambda
        mock_tgt_lambda = MagicMock()
        mock_tgt.return_value = mock_tgt_lambda

        layer_arn = f"arn:aws:lambda:{SOURCE_REGION}:{ACCOUNT_ID}:layer:shared-layer:3"
        func1 = _make_lambda("func1", layers=[layer_arn])
        func2 = _make_lambda("func2", layers=[layer_arn])

        mock_src_lambda.get_layer_version.return_value = {
            "Content": {"Location": "https://example.com/layer.zip"},
            "CompatibleRuntimes": ["python3.12"],
        }

        target_layer_arn = f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:layer:shared-layer:1"
        mock_tgt_lambda.publish_layer_version.return_value = {
            "LayerVersionArn": target_layer_arn
        }

        mock_resp = MagicMock()
        mock_resp.content = b"zip"
        mock_resp.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_resp

        mapping = replicate_lambda_layers([func1, func2], SOURCE_REGION, TARGET_REGION)

        assert layer_arn in mapping
        assert mapping[layer_arn] == target_layer_arn
        # Should only replicate once despite two functions using it
        mock_tgt_lambda.publish_layer_version.assert_called_once()

    def test_no_layers_returns_empty_mapping(self):
        func = _make_lambda("func1", layers=[])
        mapping = replicate_lambda_layers([func], SOURCE_REGION, TARGET_REGION)
        assert mapping == {}
