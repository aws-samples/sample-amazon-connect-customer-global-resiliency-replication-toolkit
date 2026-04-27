"""Unit tests for Lambda discovery module."""

from unittest.mock import MagicMock, patch

import pytest

from discovery.lambda_discovery import (
    _extract_dynamodb_table_arn_from_stream,
    _extract_kinesis_stream_arn,
    _generate_resource_id,
    discover_lambda_functions,
)


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

ACCOUNT = "123456789012"
REGION = "us-west-2"
INSTANCE_ID = "abc-def-123"


def _lambda_arn(name: str) -> str:
    return f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{name}"


def _role_arn(name: str) -> str:
    return f"arn:aws:iam::{ACCOUNT}:role/{name}"


def _ddb_stream_arn(table: str) -> str:
    return f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{table}/stream/2024-01-01T00:00:00.000"


def _kinesis_arn(stream: str) -> str:
    return f"arn:aws:kinesis:{REGION}:{ACCOUNT}:stream/{stream}"


def _make_get_function_response(
    name: str,
    role_name: str = "my-role",
    runtime: str = "python3.12",
    handler: str = "index.handler",
    memory: int = 256,
    timeout: int = 30,
    env: dict | None = None,
    layers: list[str] | None = None,
    vpc: dict | None = None,
) -> dict:
    config = {
        "FunctionName": name,
        "FunctionArn": _lambda_arn(name),
        "Runtime": runtime,
        "Handler": handler,
        "MemorySize": memory,
        "Timeout": timeout,
        "Role": _role_arn(role_name),
        "Environment": {"Variables": env or {}},
        "Layers": [{"Arn": l} for l in (layers or [])],
    }
    if vpc:
        config["VpcConfig"] = vpc
    return {"Configuration": config, "Code": {"Location": "https://example.com/code.zip"}}


def _make_role_response(name: str) -> dict:
    return {
        "Role": {
            "RoleName": name,
            "Arn": _role_arn(name),
            "AssumeRolePolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}}],
            },
        }
    }



# ---------------------------------------------------------------------------
# Tests for helper functions
# ---------------------------------------------------------------------------


class TestGenerateResourceId:
    def test_deterministic(self):
        arn = _lambda_arn("my-func")
        assert _generate_resource_id(arn) == _generate_resource_id(arn)

    def test_different_arns_produce_different_ids(self):
        assert _generate_resource_id(_lambda_arn("a")) != _generate_resource_id(_lambda_arn("b"))

    def test_returns_12_char_hex(self):
        rid = _generate_resource_id(_lambda_arn("test"))
        assert len(rid) == 12
        assert all(c in "0123456789abcdef" for c in rid)


class TestExtractDynamoDBTableArn:
    def test_valid_stream_arn(self):
        stream_arn = _ddb_stream_arn("MyTable")
        result = _extract_dynamodb_table_arn_from_stream(stream_arn)
        assert result == f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/MyTable"

    def test_non_dynamodb_arn(self):
        assert _extract_dynamodb_table_arn_from_stream(_kinesis_arn("my-stream")) is None

    def test_dynamodb_table_arn_without_stream(self):
        table_arn = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/MyTable"
        assert _extract_dynamodb_table_arn_from_stream(table_arn) is None

    def test_invalid_arn(self):
        assert _extract_dynamodb_table_arn_from_stream("not-an-arn") is None

    def test_empty_string(self):
        assert _extract_dynamodb_table_arn_from_stream("") is None


class TestExtractKinesisStreamArn:
    def test_valid_kinesis_arn(self):
        arn = _kinesis_arn("my-stream")
        assert _extract_kinesis_stream_arn(arn) == arn

    def test_non_kinesis_arn(self):
        assert _extract_kinesis_stream_arn(_ddb_stream_arn("MyTable")) is None

    def test_kinesis_firehose_arn(self):
        arn = f"arn:aws:firehose:{REGION}:{ACCOUNT}:deliverystream/my-firehose"
        assert _extract_kinesis_stream_arn(arn) is None

    def test_invalid_arn(self):
        assert _extract_kinesis_stream_arn("not-an-arn") is None

    def test_empty_string(self):
        assert _extract_kinesis_stream_arn("") is None


# ---------------------------------------------------------------------------
# Tests for discover_lambda_functions
# ---------------------------------------------------------------------------


class TestDiscoverLambdaFunctions:
    """Tests for the main discover_lambda_functions orchestration."""

    @patch("discovery.lambda_discovery.create_source_client")
    def test_no_lambda_functions(self, mock_create_client):
        """When Connect returns no Lambda functions, all lists should be empty."""
        mock_connect = MagicMock()
        mock_connect.list_lambda_functions.return_value = {"LambdaFunctions": []}
        mock_lambda = MagicMock()
        mock_iam = MagicMock()

        def side_effect(service, region):
            return {"connect": mock_connect, "lambda": mock_lambda, "iam": mock_iam}[service]

        mock_create_client.side_effect = side_effect

        lambdas, roles, ddb_arns, kinesis_arns = discover_lambda_functions(INSTANCE_ID, REGION)

        assert lambdas == []
        assert roles == []
        assert ddb_arns == []
        assert kinesis_arns == []
        mock_connect.list_lambda_functions.assert_called_once()

    @patch("discovery.lambda_discovery.create_source_client")
    def test_single_lambda_no_esm(self, mock_create_client):
        """Discover a single Lambda with no ESM triggers."""
        func_name = "my-connect-func"
        role_name = "my-role"

        mock_connect = MagicMock()
        mock_connect.list_lambda_functions.return_value = {
            "LambdaFunctions": [_lambda_arn(func_name)]
        }

        mock_lambda = MagicMock()
        mock_lambda.get_function.return_value = _make_get_function_response(
            func_name, role_name=role_name
        )
        mock_lambda.list_event_source_mappings.return_value = {"EventSourceMappings": []}

        mock_iam = MagicMock()
        mock_iam.get_role.return_value = _make_role_response(role_name)
        # Set up paginators
        attached_paginator = MagicMock()
        attached_paginator.paginate.return_value = [{"AttachedPolicies": [{"PolicyArn": "arn:aws:iam::aws:policy/AWSLambdaBasicExecutionRole", "PolicyName": "AWSLambdaBasicExecutionRole"}]}]
        inline_paginator = MagicMock()
        inline_paginator.paginate.return_value = [{"PolicyNames": []}]

        def get_paginator(name):
            if name == "list_attached_role_policies":
                return attached_paginator
            return inline_paginator

        mock_iam.get_paginator.side_effect = get_paginator

        def side_effect(service, region):
            return {"connect": mock_connect, "lambda": mock_lambda, "iam": mock_iam}[service]

        mock_create_client.side_effect = side_effect

        lambdas, roles, ddb_arns, kinesis_arns = discover_lambda_functions(INSTANCE_ID, REGION)

        assert len(lambdas) == 1
        assert lambdas[0].name == func_name
        assert lambdas[0].runtime == "python3.12"
        assert lambdas[0].handler == "index.handler"
        assert lambdas[0].memory_size == 256
        assert lambdas[0].timeout == 30
        assert lambdas[0].execution_role_arn == _role_arn(role_name)
        assert lambdas[0].esm_triggers == []

        assert len(roles) == 1
        assert roles[0].name == role_name
        assert roles[0].arn == _role_arn(role_name)
        assert len(roles[0].attached_policies) == 1

        assert ddb_arns == []
        assert kinesis_arns == []


    @patch("discovery.lambda_discovery.create_source_client")
    def test_lambda_with_dynamodb_esm(self, mock_create_client):
        """Lambda with a DynamoDB stream ESM should extract the table ARN."""
        func_name = "ddb-processor"
        role_name = "ddb-role"
        table_name = "ContactRecords"

        mock_connect = MagicMock()
        mock_connect.list_lambda_functions.return_value = {
            "LambdaFunctions": [_lambda_arn(func_name)]
        }

        mock_lambda = MagicMock()
        mock_lambda.get_function.return_value = _make_get_function_response(
            func_name, role_name=role_name
        )
        mock_lambda.list_event_source_mappings.return_value = {
            "EventSourceMappings": [
                {
                    "UUID": "esm-1",
                    "EventSourceArn": _ddb_stream_arn(table_name),
                    "BatchSize": 100,
                    "StartingPosition": "LATEST",
                    "State": "Enabled",
                }
            ]
        }

        mock_iam = MagicMock()
        mock_iam.get_role.return_value = _make_role_response(role_name)
        attached_paginator = MagicMock()
        attached_paginator.paginate.return_value = [{"AttachedPolicies": []}]
        inline_paginator = MagicMock()
        inline_paginator.paginate.return_value = [{"PolicyNames": []}]
        mock_iam.get_paginator.side_effect = lambda n: attached_paginator if n == "list_attached_role_policies" else inline_paginator

        def side_effect(service, region):
            return {"connect": mock_connect, "lambda": mock_lambda, "iam": mock_iam}[service]

        mock_create_client.side_effect = side_effect

        lambdas, roles, ddb_arns, kinesis_arns = discover_lambda_functions(INSTANCE_ID, REGION)

        assert len(lambdas) == 1
        assert len(lambdas[0].esm_triggers) == 1
        assert lambdas[0].esm_triggers[0]["EventSourceArn"] == _ddb_stream_arn(table_name)

        expected_table_arn = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{table_name}"
        assert ddb_arns == [expected_table_arn]
        assert kinesis_arns == []

    @patch("discovery.lambda_discovery.create_source_client")
    def test_lambda_with_kinesis_esm(self, mock_create_client):
        """Lambda with a Kinesis stream ESM should extract the stream ARN."""
        func_name = "kinesis-processor"
        role_name = "kinesis-role"
        stream_name = "ContactEvents"

        mock_connect = MagicMock()
        mock_connect.list_lambda_functions.return_value = {
            "LambdaFunctions": [_lambda_arn(func_name)]
        }

        mock_lambda = MagicMock()
        mock_lambda.get_function.return_value = _make_get_function_response(
            func_name, role_name=role_name
        )
        mock_lambda.list_event_source_mappings.return_value = {
            "EventSourceMappings": [
                {
                    "UUID": "esm-2",
                    "EventSourceArn": _kinesis_arn(stream_name),
                    "BatchSize": 500,
                    "StartingPosition": "TRIM_HORIZON",
                    "State": "Enabled",
                }
            ]
        }

        mock_iam = MagicMock()
        mock_iam.get_role.return_value = _make_role_response(role_name)
        attached_paginator = MagicMock()
        attached_paginator.paginate.return_value = [{"AttachedPolicies": []}]
        inline_paginator = MagicMock()
        inline_paginator.paginate.return_value = [{"PolicyNames": []}]
        mock_iam.get_paginator.side_effect = lambda n: attached_paginator if n == "list_attached_role_policies" else inline_paginator

        def side_effect(service, region):
            return {"connect": mock_connect, "lambda": mock_lambda, "iam": mock_iam}[service]

        mock_create_client.side_effect = side_effect

        lambdas, roles, ddb_arns, kinesis_arns = discover_lambda_functions(INSTANCE_ID, REGION)

        assert len(lambdas) == 1
        assert kinesis_arns == [_kinesis_arn(stream_name)]
        assert ddb_arns == []

    @patch("discovery.lambda_discovery.create_source_client")
    def test_multiple_lambdas_shared_role(self, mock_create_client):
        """Two Lambdas sharing the same IAM role should produce only one IAMRoleResource."""
        role_name = "shared-role"

        mock_connect = MagicMock()
        mock_connect.list_lambda_functions.return_value = {
            "LambdaFunctions": [_lambda_arn("func-a"), _lambda_arn("func-b")]
        }

        mock_lambda = MagicMock()
        mock_lambda.get_function.side_effect = [
            _make_get_function_response("func-a", role_name=role_name),
            _make_get_function_response("func-b", role_name=role_name),
        ]
        mock_lambda.list_event_source_mappings.return_value = {"EventSourceMappings": []}

        mock_iam = MagicMock()
        mock_iam.get_role.return_value = _make_role_response(role_name)
        attached_paginator = MagicMock()
        attached_paginator.paginate.return_value = [{"AttachedPolicies": []}]
        inline_paginator = MagicMock()
        inline_paginator.paginate.return_value = [{"PolicyNames": []}]
        mock_iam.get_paginator.side_effect = lambda n: attached_paginator if n == "list_attached_role_policies" else inline_paginator

        def side_effect(service, region):
            return {"connect": mock_connect, "lambda": mock_lambda, "iam": mock_iam}[service]

        mock_create_client.side_effect = side_effect

        lambdas, roles, ddb_arns, kinesis_arns = discover_lambda_functions(INSTANCE_ID, REGION)

        assert len(lambdas) == 2
        # Only one IAM role resource despite two Lambdas sharing it
        assert len(roles) == 1
        assert roles[0].name == role_name

        # Both Lambdas should reference the same role resource ID
        assert lambdas[0].dependencies == lambdas[1].dependencies

    @patch("discovery.lambda_discovery.create_source_client")
    def test_duplicate_esm_arns_deduplicated(self, mock_create_client):
        """Two Lambdas with ESMs pointing to the same DynamoDB table should deduplicate."""
        role_name_a = "role-a"
        role_name_b = "role-b"
        table_name = "SharedTable"

        mock_connect = MagicMock()
        mock_connect.list_lambda_functions.return_value = {
            "LambdaFunctions": [_lambda_arn("func-a"), _lambda_arn("func-b")]
        }

        mock_lambda = MagicMock()
        mock_lambda.get_function.side_effect = [
            _make_get_function_response("func-a", role_name=role_name_a),
            _make_get_function_response("func-b", role_name=role_name_b),
        ]
        mock_lambda.list_event_source_mappings.side_effect = [
            {"EventSourceMappings": [{"UUID": "e1", "EventSourceArn": _ddb_stream_arn(table_name), "State": "Enabled"}]},
            {"EventSourceMappings": [{"UUID": "e2", "EventSourceArn": _ddb_stream_arn(table_name), "State": "Enabled"}]},
        ]

        mock_iam = MagicMock()
        mock_iam.get_role.side_effect = [_make_role_response(role_name_a), _make_role_response(role_name_b)]
        attached_paginator = MagicMock()
        attached_paginator.paginate.return_value = [{"AttachedPolicies": []}]
        inline_paginator = MagicMock()
        inline_paginator.paginate.return_value = [{"PolicyNames": []}]
        mock_iam.get_paginator.side_effect = lambda n: attached_paginator if n == "list_attached_role_policies" else inline_paginator

        def side_effect(service, region):
            return {"connect": mock_connect, "lambda": mock_lambda, "iam": mock_iam}[service]

        mock_create_client.side_effect = side_effect

        lambdas, roles, ddb_arns, kinesis_arns = discover_lambda_functions(INSTANCE_ID, REGION)

        assert len(lambdas) == 2
        assert len(roles) == 2
        # Same table referenced by two ESMs should appear only once
        assert len(ddb_arns) == 1

    @patch("discovery.lambda_discovery.create_source_client")
    def test_get_function_failure_skips_lambda(self, mock_create_client):
        """If GetFunction fails for a Lambda, it should be skipped gracefully."""
        mock_connect = MagicMock()
        mock_connect.list_lambda_functions.return_value = {
            "LambdaFunctions": [_lambda_arn("good-func"), _lambda_arn("bad-func")]
        }

        mock_lambda = MagicMock()
        mock_lambda.get_function.side_effect = [
            _make_get_function_response("good-func"),
            RuntimeError("Access denied"),
        ]
        mock_lambda.list_event_source_mappings.return_value = {"EventSourceMappings": []}

        mock_iam = MagicMock()
        mock_iam.get_role.return_value = _make_role_response("my-role")
        attached_paginator = MagicMock()
        attached_paginator.paginate.return_value = [{"AttachedPolicies": []}]
        inline_paginator = MagicMock()
        inline_paginator.paginate.return_value = [{"PolicyNames": []}]
        mock_iam.get_paginator.side_effect = lambda n: attached_paginator if n == "list_attached_role_policies" else inline_paginator

        def side_effect(service, region):
            return {"connect": mock_connect, "lambda": mock_lambda, "iam": mock_iam}[service]

        mock_create_client.side_effect = side_effect

        lambdas, roles, ddb_arns, kinesis_arns = discover_lambda_functions(INSTANCE_ID, REGION)

        # Only the good function should be in results
        assert len(lambdas) == 1
        assert lambdas[0].name == "good-func"

    @patch("discovery.lambda_discovery.create_source_client")
    def test_pagination_of_lambda_functions(self, mock_create_client):
        """Connect ListLambdaFunctions pagination should be handled."""
        mock_connect = MagicMock()
        mock_connect.list_lambda_functions.side_effect = [
            {"LambdaFunctions": [_lambda_arn("func-1")], "NextToken": "token1"},
            {"LambdaFunctions": [_lambda_arn("func-2")]},
        ]

        mock_lambda = MagicMock()
        mock_lambda.get_function.side_effect = [
            _make_get_function_response("func-1", role_name="role-1"),
            _make_get_function_response("func-2", role_name="role-2"),
        ]
        mock_lambda.list_event_source_mappings.return_value = {"EventSourceMappings": []}

        mock_iam = MagicMock()
        mock_iam.get_role.side_effect = [_make_role_response("role-1"), _make_role_response("role-2")]
        attached_paginator = MagicMock()
        attached_paginator.paginate.return_value = [{"AttachedPolicies": []}]
        inline_paginator = MagicMock()
        inline_paginator.paginate.return_value = [{"PolicyNames": []}]
        mock_iam.get_paginator.side_effect = lambda n: attached_paginator if n == "list_attached_role_policies" else inline_paginator

        def side_effect(service, region):
            return {"connect": mock_connect, "lambda": mock_lambda, "iam": mock_iam}[service]

        mock_create_client.side_effect = side_effect

        lambdas, roles, ddb_arns, kinesis_arns = discover_lambda_functions(INSTANCE_ID, REGION)

        assert len(lambdas) == 2
        assert {l.name for l in lambdas} == {"func-1", "func-2"}

    @patch("discovery.lambda_discovery.create_source_client")
    def test_lambda_with_vpc_config(self, mock_create_client):
        """Lambda with VPC config should include it in the resource."""
        vpc = {"SubnetIds": ["subnet-abc"], "SecurityGroupIds": ["sg-123"], "VpcId": "vpc-xyz"}

        mock_connect = MagicMock()
        mock_connect.list_lambda_functions.return_value = {
            "LambdaFunctions": [_lambda_arn("vpc-func")]
        }

        mock_lambda = MagicMock()
        mock_lambda.get_function.return_value = _make_get_function_response(
            "vpc-func", vpc=vpc
        )
        mock_lambda.list_event_source_mappings.return_value = {"EventSourceMappings": []}

        mock_iam = MagicMock()
        mock_iam.get_role.return_value = _make_role_response("my-role")
        attached_paginator = MagicMock()
        attached_paginator.paginate.return_value = [{"AttachedPolicies": []}]
        inline_paginator = MagicMock()
        inline_paginator.paginate.return_value = [{"PolicyNames": []}]
        mock_iam.get_paginator.side_effect = lambda n: attached_paginator if n == "list_attached_role_policies" else inline_paginator

        def side_effect(service, region):
            return {"connect": mock_connect, "lambda": mock_lambda, "iam": mock_iam}[service]

        mock_create_client.side_effect = side_effect

        lambdas, roles, ddb_arns, kinesis_arns = discover_lambda_functions(INSTANCE_ID, REGION)

        assert len(lambdas) == 1
        assert lambdas[0].vpc_config is not None
        assert lambdas[0].vpc_config["SubnetIds"] == ["subnet-abc"]
        assert lambdas[0].config_summary.get("vpc") == "Yes"

    @patch("discovery.lambda_discovery.create_source_client")
    def test_lambda_with_layers_and_env_vars(self, mock_create_client):
        """Lambda with layers and env vars should include them in the resource."""
        mock_connect = MagicMock()
        mock_connect.list_lambda_functions.return_value = {
            "LambdaFunctions": [_lambda_arn("layered-func")]
        }

        mock_lambda = MagicMock()
        mock_lambda.get_function.return_value = _make_get_function_response(
            "layered-func",
            layers=["arn:aws:lambda:us-west-2:123456789012:layer:my-layer:1"],
            env={"TABLE_NAME": "MyTable", "REGION": "us-west-2"},
        )
        mock_lambda.list_event_source_mappings.return_value = {"EventSourceMappings": []}

        mock_iam = MagicMock()
        mock_iam.get_role.return_value = _make_role_response("my-role")
        attached_paginator = MagicMock()
        attached_paginator.paginate.return_value = [{"AttachedPolicies": []}]
        inline_paginator = MagicMock()
        inline_paginator.paginate.return_value = [{"PolicyNames": []}]
        mock_iam.get_paginator.side_effect = lambda n: attached_paginator if n == "list_attached_role_policies" else inline_paginator

        def side_effect(service, region):
            return {"connect": mock_connect, "lambda": mock_lambda, "iam": mock_iam}[service]

        mock_create_client.side_effect = side_effect

        lambdas, roles, ddb_arns, kinesis_arns = discover_lambda_functions(INSTANCE_ID, REGION)

        assert len(lambdas) == 1
        assert len(lambdas[0].layers) == 1
        assert lambdas[0].environment == {"TABLE_NAME": "MyTable", "REGION": "us-west-2"}
        assert lambdas[0].config_summary["layers"] == "1"

    @patch("discovery.lambda_discovery.create_source_client")
    def test_inline_policies_retrieved(self, mock_create_client):
        """IAM role with inline policies should have them in the resource."""
        role_name = "inline-role"

        mock_connect = MagicMock()
        mock_connect.list_lambda_functions.return_value = {
            "LambdaFunctions": [_lambda_arn("func")]
        }

        mock_lambda = MagicMock()
        mock_lambda.get_function.return_value = _make_get_function_response("func", role_name=role_name)
        mock_lambda.list_event_source_mappings.return_value = {"EventSourceMappings": []}

        mock_iam = MagicMock()
        mock_iam.get_role.return_value = _make_role_response(role_name)

        attached_paginator = MagicMock()
        attached_paginator.paginate.return_value = [{"AttachedPolicies": []}]
        inline_paginator = MagicMock()
        inline_paginator.paginate.return_value = [{"PolicyNames": ["DDBAccess"]}]
        mock_iam.get_paginator.side_effect = lambda n: attached_paginator if n == "list_attached_role_policies" else inline_paginator

        mock_iam.get_role_policy.return_value = {
            "PolicyName": "DDBAccess",
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Action": "dynamodb:*", "Resource": "*"}],
            },
        }

        def side_effect(service, region):
            return {"connect": mock_connect, "lambda": mock_lambda, "iam": mock_iam}[service]

        mock_create_client.side_effect = side_effect

        lambdas, roles, ddb_arns, kinesis_arns = discover_lambda_functions(INSTANCE_ID, REGION)

        assert len(roles) == 1
        assert len(roles[0].inline_policies) == 1
        assert roles[0].inline_policies[0]["PolicyName"] == "DDBAccess"
        assert roles[0].config_summary["inline_policies"] == "1"
