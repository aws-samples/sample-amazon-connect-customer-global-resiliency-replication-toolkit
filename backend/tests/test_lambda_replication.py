"""Unit tests for Lambda function replication."""

from unittest.mock import MagicMock, patch, call

import pytest
from botocore.exceptions import ClientError

from models.resources import LambdaResource
from replication.lambda_replication import (
    replicate_lambda_function,
    _rewrite_env_vars,
    _rewrite_layer_arns,
    _create_event_source_mappings,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TARGET_REGION = "us-east-1"
SOURCE_REGION = "us-west-2"
ACCOUNT_ID = "123456789012"


def _make_lambda(
    name: str = "my-func",
    runtime: str = "python3.12",
    handler: str = "index.handler",
    memory_size: int = 256,
    timeout: int = 30,
    environment: dict[str, str] | None = None,
    layers: list[str] | None = None,
    vpc_config: dict | None = None,
    execution_role_arn: str | None = None,
    esm_triggers: list[dict] | None = None,
    is_lex_codehook: bool = False,
) -> LambdaResource:
    return LambdaResource(
        id="lambda-1",
        name=name,
        arn=f"arn:aws:lambda:{SOURCE_REGION}:{ACCOUNT_ID}:function:{name}",
        runtime=runtime,
        handler=handler,
        memory_size=memory_size,
        timeout=timeout,
        environment=environment or {},
        layers=layers or [],
        vpc_config=vpc_config,
        execution_role_arn=execution_role_arn
        or f"arn:aws:iam::{ACCOUNT_ID}:role/{name}-role",
        esm_triggers=esm_triggers or [],
        is_lex_codehook=is_lex_codehook,
    )


def _client_error(code: str, message: str = "error") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "TestOperation",
    )


# ---------------------------------------------------------------------------
# _rewrite_env_vars
# ---------------------------------------------------------------------------


class TestRewriteEnvVars:
    def test_rewrites_arn_values(self):
        env = {
            "TABLE_ARN": f"arn:aws:dynamodb:{SOURCE_REGION}:{ACCOUNT_ID}:table/my-table",
            "PLAIN_VAR": "hello",
        }
        result = _rewrite_env_vars(env, TARGET_REGION)
        assert result["TABLE_ARN"] == f"arn:aws:dynamodb:{TARGET_REGION}:{ACCOUNT_ID}:table/my-table"
        assert result["PLAIN_VAR"] == "hello"

    def test_leaves_non_arn_values_unchanged(self):
        env = {"KEY": "value", "NUM": "42"}
        result = _rewrite_env_vars(env, TARGET_REGION)
        assert result == env

    def test_handles_invalid_arn_gracefully(self):
        env = {"BAD": "arn:bad"}
        result = _rewrite_env_vars(env, TARGET_REGION)
        assert result["BAD"] == "arn:bad"

    def test_empty_environment(self):
        assert _rewrite_env_vars({}, TARGET_REGION) == {}

    def test_multiple_arns(self):
        env = {
            "STREAM": f"arn:aws:kinesis:{SOURCE_REGION}:{ACCOUNT_ID}:stream/s1",
            "QUEUE": f"arn:aws:sqs:{SOURCE_REGION}:{ACCOUNT_ID}:my-queue",
        }
        result = _rewrite_env_vars(env, TARGET_REGION)
        assert TARGET_REGION in result["STREAM"]
        assert TARGET_REGION in result["QUEUE"]


# ---------------------------------------------------------------------------
# _rewrite_layer_arns
# ---------------------------------------------------------------------------


class TestRewriteLayerArns:
    def test_rewrites_layer_arns(self):
        layers = [f"arn:aws:lambda:{SOURCE_REGION}:{ACCOUNT_ID}:layer:my-layer:1"]
        result = _rewrite_layer_arns(layers, TARGET_REGION)
        assert result == [f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:layer:my-layer:1"]

    def test_keeps_invalid_arn_as_is(self):
        layers = ["arn:bad"]
        result = _rewrite_layer_arns(layers, TARGET_REGION)
        assert result == ["arn:bad"]

    def test_empty_layers(self):
        assert _rewrite_layer_arns([], TARGET_REGION) == []

    def test_multiple_layers(self):
        layers = [
            f"arn:aws:lambda:{SOURCE_REGION}:{ACCOUNT_ID}:layer:layer1:1",
            f"arn:aws:lambda:{SOURCE_REGION}:{ACCOUNT_ID}:layer:layer2:3",
        ]
        result = _rewrite_layer_arns(layers, TARGET_REGION)
        assert all(TARGET_REGION in l for l in result)


# ---------------------------------------------------------------------------
# replicate_lambda_function — success path
# ---------------------------------------------------------------------------


class TestReplicateLambdaSuccess:
    @patch("replication.lambda_replication.requests")
    @patch("replication.lambda_replication.create_target_client")
    @patch("replication.lambda_replication.create_source_client")
    def test_basic_replication(self, mock_src_client, mock_tgt_client, mock_requests):
        # Source client: get_function returns code location
        mock_src_lambda = MagicMock()
        mock_src_client.return_value = mock_src_lambda
        mock_src_lambda.get_function.return_value = {
            "Code": {"Location": "https://example.com/code.zip"}
        }

        # Mock code download
        mock_response = MagicMock()
        mock_response.content = b"fake-zip-bytes"
        mock_response.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_response

        # Target client: create_function
        mock_tgt_lambda = MagicMock()
        mock_tgt_client.return_value = mock_tgt_lambda
        target_arn = f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:function:my-func"
        mock_tgt_lambda.create_function.return_value = {"FunctionArn": target_arn}

        func = _make_lambda()
        result = replicate_lambda_function(func, TARGET_REGION)

        assert result == target_arn
        mock_src_client.assert_called_once_with("lambda", SOURCE_REGION)
        mock_tgt_client.assert_called_once_with("lambda", TARGET_REGION)
        mock_tgt_lambda.create_function.assert_called_once()

        # Verify create_function params
        create_kwargs = mock_tgt_lambda.create_function.call_args[1]
        assert create_kwargs["FunctionName"] == "my-func"
        assert create_kwargs["Runtime"] == "python3.12"
        assert create_kwargs["Handler"] == "index.handler"
        assert create_kwargs["MemorySize"] == 256
        assert create_kwargs["Timeout"] == 30
        assert create_kwargs["Code"] == {"ZipFile": b"fake-zip-bytes"}

    @patch("replication.lambda_replication.requests")
    @patch("replication.lambda_replication.create_target_client")
    @patch("replication.lambda_replication.create_source_client")
    def test_role_arn_mapping(self, mock_src_client, mock_tgt_client, mock_requests):
        mock_src_lambda = MagicMock()
        mock_src_client.return_value = mock_src_lambda
        mock_src_lambda.get_function.return_value = {
            "Code": {"Location": "https://example.com/code.zip"}
        }
        mock_response = MagicMock()
        mock_response.content = b"zip"
        mock_response.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_response

        mock_tgt_lambda = MagicMock()
        mock_tgt_client.return_value = mock_tgt_lambda
        mock_tgt_lambda.create_function.return_value = {
            "FunctionArn": f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:function:my-func"
        }

        source_role = f"arn:aws:iam::{ACCOUNT_ID}:role/my-func-role"
        target_role = f"arn:aws:iam::{ACCOUNT_ID}:role/replicated-role"
        func = _make_lambda(execution_role_arn=source_role)

        replicate_lambda_function(func, TARGET_REGION, {source_role: target_role})

        create_kwargs = mock_tgt_lambda.create_function.call_args[1]
        assert create_kwargs["Role"] == target_role

    @patch("replication.lambda_replication.requests")
    @patch("replication.lambda_replication.create_target_client")
    @patch("replication.lambda_replication.create_source_client")
    def test_env_var_arn_rewriting(self, mock_src_client, mock_tgt_client, mock_requests):
        mock_src_lambda = MagicMock()
        mock_src_client.return_value = mock_src_lambda
        mock_src_lambda.get_function.return_value = {
            "Code": {"Location": "https://example.com/code.zip"}
        }
        mock_response = MagicMock()
        mock_response.content = b"zip"
        mock_response.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_response

        mock_tgt_lambda = MagicMock()
        mock_tgt_client.return_value = mock_tgt_lambda
        mock_tgt_lambda.create_function.return_value = {
            "FunctionArn": f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:function:my-func"
        }

        func = _make_lambda(
            environment={
                "TABLE_ARN": f"arn:aws:dynamodb:{SOURCE_REGION}:{ACCOUNT_ID}:table/t1",
                "PLAIN": "hello",
            }
        )
        replicate_lambda_function(func, TARGET_REGION)

        create_kwargs = mock_tgt_lambda.create_function.call_args[1]
        env_vars = create_kwargs["Environment"]["Variables"]
        assert env_vars["TABLE_ARN"] == f"arn:aws:dynamodb:{TARGET_REGION}:{ACCOUNT_ID}:table/t1"
        assert env_vars["PLAIN"] == "hello"

    @patch("replication.lambda_replication.requests")
    @patch("replication.lambda_replication.create_target_client")
    @patch("replication.lambda_replication.create_source_client")
    def test_layer_arn_rewriting(self, mock_src_client, mock_tgt_client, mock_requests):
        mock_src_lambda = MagicMock()
        mock_src_client.return_value = mock_src_lambda
        mock_src_lambda.get_function.return_value = {
            "Code": {"Location": "https://example.com/code.zip"}
        }
        mock_response = MagicMock()
        mock_response.content = b"zip"
        mock_response.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_response

        mock_tgt_lambda = MagicMock()
        mock_tgt_client.return_value = mock_tgt_lambda
        mock_tgt_lambda.create_function.return_value = {
            "FunctionArn": f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:function:my-func"
        }

        func = _make_lambda(
            layers=[f"arn:aws:lambda:{SOURCE_REGION}:{ACCOUNT_ID}:layer:my-layer:1"]
        )
        replicate_lambda_function(func, TARGET_REGION)

        create_kwargs = mock_tgt_lambda.create_function.call_args[1]
        assert create_kwargs["Layers"] == [
            f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:layer:my-layer:1"
        ]

    @patch("replication.lambda_replication.requests")
    @patch("replication.lambda_replication.create_target_client")
    @patch("replication.lambda_replication.create_source_client")
    def test_esm_trigger_creation(self, mock_src_client, mock_tgt_client, mock_requests):
        mock_src_lambda = MagicMock()
        mock_src_client.return_value = mock_src_lambda
        mock_src_lambda.get_function.return_value = {
            "Code": {"Location": "https://example.com/code.zip"}
        }
        mock_response = MagicMock()
        mock_response.content = b"zip"
        mock_response.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_response

        target_fn_arn = f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:function:my-func"
        mock_tgt_lambda = MagicMock()
        mock_tgt_client.return_value = mock_tgt_lambda
        mock_tgt_lambda.create_function.return_value = {"FunctionArn": target_fn_arn}
        mock_tgt_lambda.create_event_source_mapping.return_value = {
            "UUID": "esm-uuid-1"
        }

        esm = {
            "EventSourceArn": f"arn:aws:dynamodb:{SOURCE_REGION}:{ACCOUNT_ID}:table/t1/stream/2024",
            "BatchSize": 100,
            "StartingPosition": "LATEST",
            "State": "Enabled",
        }
        func = _make_lambda(esm_triggers=[esm])
        replicate_lambda_function(func, TARGET_REGION)

        mock_tgt_lambda.create_event_source_mapping.assert_called_once()
        esm_kwargs = mock_tgt_lambda.create_event_source_mapping.call_args[1]
        assert esm_kwargs["FunctionName"] == target_fn_arn
        assert TARGET_REGION in esm_kwargs["EventSourceArn"]
        assert esm_kwargs["BatchSize"] == 100
        assert esm_kwargs["StartingPosition"] == "LATEST"
        assert esm_kwargs["Enabled"] is True

    @patch("replication.lambda_replication.requests")
    @patch("replication.lambda_replication.create_target_client")
    @patch("replication.lambda_replication.create_source_client")
    def test_no_env_vars_omits_environment_key(self, mock_src_client, mock_tgt_client, mock_requests):
        mock_src_lambda = MagicMock()
        mock_src_client.return_value = mock_src_lambda
        mock_src_lambda.get_function.return_value = {
            "Code": {"Location": "https://example.com/code.zip"}
        }
        mock_response = MagicMock()
        mock_response.content = b"zip"
        mock_response.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_response

        mock_tgt_lambda = MagicMock()
        mock_tgt_client.return_value = mock_tgt_lambda
        mock_tgt_lambda.create_function.return_value = {
            "FunctionArn": f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:function:my-func"
        }

        func = _make_lambda(environment={})
        replicate_lambda_function(func, TARGET_REGION)

        create_kwargs = mock_tgt_lambda.create_function.call_args[1]
        assert "Environment" not in create_kwargs

    @patch("replication.lambda_replication.requests")
    @patch("replication.lambda_replication.create_target_client")
    @patch("replication.lambda_replication.create_source_client")
    def test_no_layers_omits_layers_key(self, mock_src_client, mock_tgt_client, mock_requests):
        mock_src_lambda = MagicMock()
        mock_src_client.return_value = mock_src_lambda
        mock_src_lambda.get_function.return_value = {
            "Code": {"Location": "https://example.com/code.zip"}
        }
        mock_response = MagicMock()
        mock_response.content = b"zip"
        mock_response.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_response

        mock_tgt_lambda = MagicMock()
        mock_tgt_client.return_value = mock_tgt_lambda
        mock_tgt_lambda.create_function.return_value = {
            "FunctionArn": f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:function:my-func"
        }

        func = _make_lambda(layers=[])
        replicate_lambda_function(func, TARGET_REGION)

        create_kwargs = mock_tgt_lambda.create_function.call_args[1]
        assert "Layers" not in create_kwargs

    @patch("replication.lambda_replication.requests")
    @patch("replication.lambda_replication.create_target_client")
    @patch("replication.lambda_replication.create_source_client")
    def test_lex_codehook_lambda_keeps_same_name(self, mock_src_client, mock_tgt_client, mock_requests):
        """Lex codehook Lambdas should keep the same name (no suffix)."""
        mock_src_lambda = MagicMock()
        mock_src_client.return_value = mock_src_lambda
        mock_src_lambda.get_function.return_value = {
            "Code": {"Location": "https://example.com/code.zip"}
        }
        mock_response = MagicMock()
        mock_response.content = b"zip"
        mock_response.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_response

        mock_tgt_lambda = MagicMock()
        mock_tgt_client.return_value = mock_tgt_lambda
        target_arn = f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:function:my-func"
        mock_tgt_lambda.create_function.return_value = {"FunctionArn": target_arn}

        func = _make_lambda(is_lex_codehook=True)
        result = replicate_lambda_function(func, TARGET_REGION)

        assert result == target_arn
        create_kwargs = mock_tgt_lambda.create_function.call_args[1]
        assert create_kwargs["FunctionName"] == "my-func"

    @patch("replication.lambda_replication.requests")
    @patch("replication.lambda_replication.create_target_client")
    @patch("replication.lambda_replication.create_source_client")
    def test_connect_lambda_keeps_original_name(self, mock_src_client, mock_tgt_client, mock_requests):
        """Connect-associated Lambdas (is_lex_codehook=False) keep the original name."""
        mock_src_lambda = MagicMock()
        mock_src_client.return_value = mock_src_lambda
        mock_src_lambda.get_function.return_value = {
            "Code": {"Location": "https://example.com/code.zip"}
        }
        mock_response = MagicMock()
        mock_response.content = b"zip"
        mock_response.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_response

        mock_tgt_lambda = MagicMock()
        mock_tgt_client.return_value = mock_tgt_lambda
        target_arn = f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:function:my-func"
        mock_tgt_lambda.create_function.return_value = {"FunctionArn": target_arn}

        func = _make_lambda(is_lex_codehook=False)
        result = replicate_lambda_function(func, TARGET_REGION)

        assert result == target_arn
        create_kwargs = mock_tgt_lambda.create_function.call_args[1]
        assert create_kwargs["FunctionName"] == "my-func"

    @patch("replication.lambda_replication.requests")
    @patch("replication.lambda_replication.create_target_client")
    @patch("replication.lambda_replication.create_source_client")
    def test_lex_codehook_lambda_skips_connect_association(self, mock_src_client, mock_tgt_client, mock_requests):
        """Lex codehook Lambdas should NOT be associated with Connect — they are invoked by Lex."""
        mock_src_lambda = MagicMock()
        mock_src_client.return_value = mock_src_lambda
        mock_src_lambda.get_function.return_value = {
            "Code": {"Location": "https://example.com/code.zip"}
        }
        mock_response = MagicMock()
        mock_response.content = b"zip"
        mock_response.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_response

        mock_tgt_lambda = MagicMock()
        mock_tgt_client.return_value = mock_tgt_lambda
        mock_tgt_lambda.create_function.return_value = {
            "FunctionArn": f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:function:my-func"
        }

        func = _make_lambda(is_lex_codehook=True)
        replicate_lambda_function(func, TARGET_REGION, instance_id="test-instance-id")

        # Should only have called create_target_client for "lambda", never "connect"
        connect_calls = [c for c in mock_tgt_client.call_args_list if c[0][0] == "connect"]
        assert len(connect_calls) == 0, "Lex codehook Lambda should NOT be associated with Connect"


# ---------------------------------------------------------------------------
# replicate_lambda_function — error handling
# ---------------------------------------------------------------------------


class TestReplicateLambdaErrors:
    @patch("replication.lambda_replication.requests")
    @patch("replication.lambda_replication.create_target_client")
    @patch("replication.lambda_replication.create_source_client")
    def test_function_already_exists_reuses_existing(self, mock_src_client, mock_tgt_client, mock_requests):
        mock_src_lambda = MagicMock()
        mock_src_client.return_value = mock_src_lambda
        mock_src_lambda.get_function.return_value = {
            "Code": {"Location": "https://example.com/code.zip"}
        }
        mock_response = MagicMock()
        mock_response.content = b"zip"
        mock_response.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_response

        target_arn = f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:function:my-func"
        mock_tgt_lambda = MagicMock()
        mock_tgt_client.return_value = mock_tgt_lambda
        mock_tgt_lambda.create_function.side_effect = _client_error(
            "ResourceConflictException", "Function already exists"
        )
        mock_tgt_lambda.get_function.return_value = {
            "Configuration": {"FunctionArn": target_arn}
        }

        func = _make_lambda()
        result = replicate_lambda_function(func, TARGET_REGION)

        assert result == target_arn
        mock_tgt_lambda.get_function.assert_called_once_with(FunctionName="my-func")

    @patch("replication.lambda_replication.requests")
    @patch("replication.lambda_replication.create_target_client")
    @patch("replication.lambda_replication.create_source_client")
    def test_function_already_exists_get_function_fails(self, mock_src_client, mock_tgt_client, mock_requests):
        mock_src_lambda = MagicMock()
        mock_src_client.return_value = mock_src_lambda
        mock_src_lambda.get_function.return_value = {
            "Code": {"Location": "https://example.com/code.zip"}
        }
        mock_response = MagicMock()
        mock_response.content = b"zip"
        mock_response.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_response

        mock_tgt_lambda = MagicMock()
        mock_tgt_client.return_value = mock_tgt_lambda
        mock_tgt_lambda.create_function.side_effect = _client_error(
            "ResourceConflictException", "Function already exists"
        )
        mock_tgt_lambda.get_function.side_effect = _client_error(
            "ServiceException", "boom"
        )

        func = _make_lambda()
        with pytest.raises(RuntimeError, match="exists but failed to retrieve"):
            replicate_lambda_function(func, TARGET_REGION)

    @patch("replication.lambda_replication.requests")
    @patch("replication.lambda_replication.create_target_client")
    @patch("replication.lambda_replication.create_source_client")
    def test_create_function_access_denied(self, mock_src_client, mock_tgt_client, mock_requests):
        mock_src_lambda = MagicMock()
        mock_src_client.return_value = mock_src_lambda
        mock_src_lambda.get_function.return_value = {
            "Code": {"Location": "https://example.com/code.zip"}
        }
        mock_response = MagicMock()
        mock_response.content = b"zip"
        mock_response.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_response

        mock_tgt_lambda = MagicMock()
        mock_tgt_client.return_value = mock_tgt_lambda
        mock_tgt_lambda.create_function.side_effect = _client_error(
            "AccessDeniedException", "no perms"
        )

        func = _make_lambda()
        with pytest.raises(PermissionError, match="Permission denied"):
            replicate_lambda_function(func, TARGET_REGION)

    @patch("replication.lambda_replication.requests")
    @patch("replication.lambda_replication.create_target_client")
    @patch("replication.lambda_replication.create_source_client")
    def test_create_function_unknown_error(self, mock_src_client, mock_tgt_client, mock_requests):
        mock_src_lambda = MagicMock()
        mock_src_client.return_value = mock_src_lambda
        mock_src_lambda.get_function.return_value = {
            "Code": {"Location": "https://example.com/code.zip"}
        }
        mock_response = MagicMock()
        mock_response.content = b"zip"
        mock_response.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_response

        mock_tgt_lambda = MagicMock()
        mock_tgt_client.return_value = mock_tgt_lambda
        mock_tgt_lambda.create_function.side_effect = _client_error(
            "ServiceException", "boom"
        )

        func = _make_lambda()
        with pytest.raises(RuntimeError, match="Failed to create Lambda function"):
            replicate_lambda_function(func, TARGET_REGION)

    @patch("replication.lambda_replication.create_source_client")
    def test_get_function_failure(self, mock_src_client):
        mock_src_lambda = MagicMock()
        mock_src_client.return_value = mock_src_lambda
        mock_src_lambda.get_function.side_effect = _client_error(
            "ResourceNotFoundException", "not found"
        )

        func = _make_lambda()
        with pytest.raises(RuntimeError, match="Failed to get function"):
            replicate_lambda_function(func, TARGET_REGION)

    @patch("replication.lambda_replication.create_source_client")
    def test_no_code_location(self, mock_src_client):
        mock_src_lambda = MagicMock()
        mock_src_client.return_value = mock_src_lambda
        mock_src_lambda.get_function.return_value = {"Code": {}}

        func = _make_lambda()
        with pytest.raises(RuntimeError, match="No code location"):
            replicate_lambda_function(func, TARGET_REGION)

    @patch("replication.lambda_replication.requests")
    @patch("replication.lambda_replication.create_source_client")
    def test_code_download_failure(self, mock_src_client, mock_requests):
        mock_src_lambda = MagicMock()
        mock_src_client.return_value = mock_src_lambda
        mock_src_lambda.get_function.return_value = {
            "Code": {"Location": "https://example.com/code.zip"}
        }

        import requests as real_requests
        mock_requests.get.side_effect = real_requests.RequestException("timeout")
        mock_requests.RequestException = real_requests.RequestException

        func = _make_lambda()
        with pytest.raises(RuntimeError, match="Failed to download code"):
            replicate_lambda_function(func, TARGET_REGION)


# ---------------------------------------------------------------------------
# _create_event_source_mappings — edge cases
# ---------------------------------------------------------------------------


class TestCreateEventSourceMappings:
    def test_skips_esm_with_no_source_arn(self):
        mock_client = MagicMock()
        func = _make_lambda(esm_triggers=[{"BatchSize": 10}])
        result = _create_event_source_mappings(
            mock_client, func, "arn:target:fn", TARGET_REGION
        )
        assert result == []
        mock_client.create_event_source_mapping.assert_not_called()

    def test_skips_esm_with_invalid_source_arn(self):
        mock_client = MagicMock()
        func = _make_lambda(esm_triggers=[{"EventSourceArn": "arn:bad"}])
        result = _create_event_source_mappings(
            mock_client, func, "arn:target:fn", TARGET_REGION
        )
        assert result == []
        mock_client.create_event_source_mapping.assert_not_called()

    def test_continues_on_esm_creation_failure(self):
        mock_client = MagicMock()
        mock_client.create_event_source_mapping.side_effect = [
            _client_error("InvalidParameterValueException", "bad"),
            {"UUID": "esm-2"},
        ]

        esm1 = {
            "EventSourceArn": f"arn:aws:dynamodb:{SOURCE_REGION}:{ACCOUNT_ID}:table/t1/stream/2024",
            "State": "Enabled",
        }
        esm2 = {
            "EventSourceArn": f"arn:aws:kinesis:{SOURCE_REGION}:{ACCOUNT_ID}:stream/s1",
            "State": "Enabled",
        }
        func = _make_lambda(esm_triggers=[esm1, esm2])
        result = _create_event_source_mappings(
            mock_client, func, "arn:target:fn", TARGET_REGION
        )

        assert len(result) == 1
        assert result[0]["UUID"] == "esm-2"
        assert mock_client.create_event_source_mapping.call_count == 2

    def test_disabled_esm_created_as_disabled(self):
        mock_client = MagicMock()
        mock_client.create_event_source_mapping.return_value = {"UUID": "esm-1"}

        esm = {
            "EventSourceArn": f"arn:aws:kinesis:{SOURCE_REGION}:{ACCOUNT_ID}:stream/s1",
            "State": "Disabled",
        }
        func = _make_lambda(esm_triggers=[esm])
        _create_event_source_mappings(
            mock_client, func, "arn:target:fn", TARGET_REGION
        )

        esm_kwargs = mock_client.create_event_source_mapping.call_args[1]
        assert esm_kwargs["Enabled"] is False
