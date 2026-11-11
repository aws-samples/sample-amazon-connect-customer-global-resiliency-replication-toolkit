"""Unit tests for IAM role replication."""

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from models.resources import IAMRoleResource
from replication.iam_replication import (
    replicate_iam_role,
    _rewrite_policy_arns,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TARGET_REGION = "us-east-1"
ACCOUNT_ID = "123456789012"


def _make_role(
    name: str = "my-role",
    assume_role_policy: dict | None = None,
    inline_policies: list[dict] | None = None,
    attached_policies: list[dict] | None = None,
) -> IAMRoleResource:
    return IAMRoleResource(
        id="iam-1",
        name=name,
        arn=f"arn:aws:iam::{ACCOUNT_ID}:role/{name}",
        assume_role_policy=assume_role_policy or {"Version": "2012-10-17", "Statement": []},
        inline_policies=inline_policies or [],
        attached_policies=attached_policies or [],
    )


def _client_error(code: str, message: str = "error") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "TestOperation",
    )


# ---------------------------------------------------------------------------
# _rewrite_policy_arns
# ---------------------------------------------------------------------------


class TestRewritePolicyArns:
    def test_rewrites_arn_string(self):
        result = _rewrite_policy_arns(
            "arn:aws:dynamodb:us-west-2:123456789012:table/my-table",
            "us-east-1",
        )
        assert result == "arn:aws:dynamodb:us-east-1:123456789012:table/my-table"

    def test_leaves_non_arn_string_unchanged(self):
        assert _rewrite_policy_arns("not-an-arn", "us-east-1") == "not-an-arn"

    def test_rewrites_arns_in_list(self):
        result = _rewrite_policy_arns(
            ["arn:aws:s3:us-west-2:123456789012:bucket/b", "plain-string"],
            "us-east-1",
        )
        assert result == ["arn:aws:s3:us-east-1:123456789012:bucket/b", "plain-string"]

    def test_rewrites_arns_in_nested_dict(self):
        doc = {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "dynamodb:*",
                    "Resource": "arn:aws:dynamodb:us-west-2:123456789012:table/t",
                }
            ]
        }
        result = _rewrite_policy_arns(doc, "us-east-1")
        assert result["Statement"][0]["Resource"] == (
            "arn:aws:dynamodb:us-east-1:123456789012:table/t"
        )

    def test_preserves_non_string_values(self):
        doc = {"Version": "2012-10-17", "Count": 42, "Enabled": True}
        result = _rewrite_policy_arns(doc, "us-east-1")
        assert result == doc

    def test_handles_wildcard_resource(self):
        """Wildcard '*' is not an ARN and should be left alone."""
        doc = {"Resource": "*"}
        result = _rewrite_policy_arns(doc, "us-east-1")
        assert result["Resource"] == "*"

    def test_handles_invalid_arn_gracefully(self):
        """An ARN-like string that fails parsing should be left unchanged."""
        result = _rewrite_policy_arns("arn:bad", "us-east-1")
        assert result == "arn:bad"


# ---------------------------------------------------------------------------
# replicate_iam_role — success path
# ---------------------------------------------------------------------------


class TestReplicateIamRoleSuccess:
    @patch("replication.iam_replication.create_target_client")
    def test_creates_role_with_assume_policy(self, mock_create_client):
        mock_iam = MagicMock()
        mock_create_client.return_value = mock_iam
        mock_iam.create_role.return_value = {
            "Role": {"Arn": f"arn:aws:iam::{ACCOUNT_ID}:role/my-role"}
        }

        role = _make_role()
        result = replicate_iam_role(role, TARGET_REGION)

        mock_create_client.assert_called_once_with("iam", TARGET_REGION)
        mock_iam.create_role.assert_called_once_with(
            RoleName="my-role",
            AssumeRolePolicyDocument=json.dumps(role.assume_role_policy),
            Description=f"Replicated from {role.arn}",
        )
        assert result == f"arn:aws:iam::{ACCOUNT_ID}:role/my-role"

    @patch("replication.iam_replication.create_target_client")
    def test_puts_inline_policies_with_arn_rewriting(self, mock_create_client):
        mock_iam = MagicMock()
        mock_create_client.return_value = mock_iam
        mock_iam.create_role.return_value = {
            "Role": {"Arn": f"arn:aws:iam::{ACCOUNT_ID}:role/my-role"}
        }

        inline_policy = {
            "PolicyName": "ddb-access",
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "dynamodb:*",
                        "Resource": "arn:aws:dynamodb:us-west-2:123456789012:table/t",
                    }
                ],
            },
        }
        role = _make_role(inline_policies=[inline_policy])
        replicate_iam_role(role, TARGET_REGION)

        mock_iam.put_role_policy.assert_called_once()
        call_kwargs = mock_iam.put_role_policy.call_args[1]
        assert call_kwargs["RoleName"] == "my-role"
        assert call_kwargs["PolicyName"] == "ddb-access"

        put_doc = json.loads(call_kwargs["PolicyDocument"])
        assert put_doc["Statement"][0]["Resource"] == (
            "arn:aws:dynamodb:us-east-1:123456789012:table/t"
        )

    @patch("replication.iam_replication.create_target_client")
    def test_attaches_managed_policies(self, mock_create_client):
        mock_iam = MagicMock()
        mock_create_client.return_value = mock_iam
        mock_iam.create_role.return_value = {
            "Role": {"Arn": f"arn:aws:iam::{ACCOUNT_ID}:role/my-role"}
        }

        managed = {"PolicyArn": "arn:aws:iam::aws:policy/AmazonDynamoDBReadOnlyAccess"}
        role = _make_role(attached_policies=[managed])
        replicate_iam_role(role, TARGET_REGION)

        mock_iam.attach_role_policy.assert_called_once_with(
            RoleName="my-role",
            PolicyArn="arn:aws:iam::aws:policy/AmazonDynamoDBReadOnlyAccess",
        )

    @patch("replication.iam_replication.create_target_client")
    def test_full_replication_with_all_policy_types(self, mock_create_client):
        mock_iam = MagicMock()
        mock_create_client.return_value = mock_iam
        mock_iam.create_role.return_value = {
            "Role": {"Arn": f"arn:aws:iam::{ACCOUNT_ID}:role/full-role"}
        }

        role = _make_role(
            name="full-role",
            inline_policies=[
                {"PolicyName": "p1", "PolicyDocument": {"Statement": []}},
                {"PolicyName": "p2", "PolicyDocument": {"Statement": []}},
            ],
            attached_policies=[
                {"PolicyArn": "arn:aws:iam::aws:policy/ReadOnlyAccess"},
                {"PolicyArn": "arn:aws:iam::aws:policy/CloudWatchLogsFullAccess"},
            ],
        )
        result = replicate_iam_role(role, TARGET_REGION)

        assert result == f"arn:aws:iam::{ACCOUNT_ID}:role/full-role"
        assert mock_iam.put_role_policy.call_count == 2
        assert mock_iam.attach_role_policy.call_count == 2

    @patch("replication.iam_replication.create_target_client")
    def test_role_with_no_policies(self, mock_create_client):
        mock_iam = MagicMock()
        mock_create_client.return_value = mock_iam
        mock_iam.create_role.return_value = {
            "Role": {"Arn": f"arn:aws:iam::{ACCOUNT_ID}:role/bare-role"}
        }

        role = _make_role(name="bare-role")
        result = replicate_iam_role(role, TARGET_REGION)

        assert result == f"arn:aws:iam::{ACCOUNT_ID}:role/bare-role"
        mock_iam.put_role_policy.assert_not_called()
        mock_iam.attach_role_policy.assert_not_called()


# ---------------------------------------------------------------------------
# replicate_iam_role — error handling
# ---------------------------------------------------------------------------


class TestReplicateIamRoleErrors:
    @patch("replication.iam_replication.create_target_client")
    def test_role_already_exists_reuses_existing(self, mock_create_client):
        mock_iam = MagicMock()
        mock_create_client.return_value = mock_iam
        mock_iam.create_role.side_effect = _client_error("EntityAlreadyExists")
        mock_iam.get_role.return_value = {
            "Role": {"Arn": f"arn:aws:iam::{ACCOUNT_ID}:role/my-role"}
        }

        role = _make_role()
        result = replicate_iam_role(role, TARGET_REGION)

        assert result == f"arn:aws:iam::{ACCOUNT_ID}:role/my-role"
        mock_iam.get_role.assert_called_once_with(RoleName="my-role")

    @patch("replication.iam_replication.create_target_client")
    def test_role_already_exists_still_applies_policies(self, mock_create_client):
        mock_iam = MagicMock()
        mock_create_client.return_value = mock_iam
        mock_iam.create_role.side_effect = _client_error("EntityAlreadyExists")
        mock_iam.get_role.return_value = {
            "Role": {"Arn": f"arn:aws:iam::{ACCOUNT_ID}:role/my-role"}
        }

        role = _make_role(
            inline_policies=[{"PolicyName": "p1", "PolicyDocument": {"Statement": []}}],
            attached_policies=[{"PolicyArn": "arn:aws:iam::aws:policy/ReadOnly"}],
        )
        replicate_iam_role(role, TARGET_REGION)

        mock_iam.put_role_policy.assert_called_once()
        mock_iam.attach_role_policy.assert_called_once()

    @patch("replication.iam_replication.create_target_client")
    def test_role_already_exists_get_role_fails(self, mock_create_client):
        mock_iam = MagicMock()
        mock_create_client.return_value = mock_iam
        mock_iam.create_role.side_effect = _client_error("EntityAlreadyExists")
        mock_iam.get_role.side_effect = _client_error("ServiceException", "boom")

        role = _make_role()
        with pytest.raises(RuntimeError, match="exists but failed to retrieve"):
            replicate_iam_role(role, TARGET_REGION)

    @patch("replication.iam_replication.create_target_client")
    def test_create_role_access_denied_raises_permission_error(self, mock_create_client):
        mock_iam = MagicMock()
        mock_create_client.return_value = mock_iam
        mock_iam.create_role.side_effect = _client_error("AccessDenied", "no perms")

        role = _make_role()
        with pytest.raises(PermissionError, match="Permission denied"):
            replicate_iam_role(role, TARGET_REGION)

    @patch("replication.iam_replication.create_target_client")
    def test_create_role_unknown_error_raises_runtime_error(self, mock_create_client):
        mock_iam = MagicMock()
        mock_create_client.return_value = mock_iam
        mock_iam.create_role.side_effect = _client_error("ServiceException", "boom")

        role = _make_role()
        with pytest.raises(RuntimeError, match="Failed to create IAM role"):
            replicate_iam_role(role, TARGET_REGION)

    @patch("replication.iam_replication.create_target_client")
    def test_put_inline_policy_access_denied(self, mock_create_client):
        mock_iam = MagicMock()
        mock_create_client.return_value = mock_iam
        mock_iam.create_role.return_value = {
            "Role": {"Arn": f"arn:aws:iam::{ACCOUNT_ID}:role/my-role"}
        }
        mock_iam.put_role_policy.side_effect = _client_error("AccessDenied")

        role = _make_role(
            inline_policies=[{"PolicyName": "p1", "PolicyDocument": {}}]
        )
        with pytest.raises(PermissionError, match="Permission denied putting policy"):
            replicate_iam_role(role, TARGET_REGION)

    @patch("replication.iam_replication.create_target_client")
    def test_put_inline_policy_unknown_error(self, mock_create_client):
        mock_iam = MagicMock()
        mock_create_client.return_value = mock_iam
        mock_iam.create_role.return_value = {
            "Role": {"Arn": f"arn:aws:iam::{ACCOUNT_ID}:role/my-role"}
        }
        mock_iam.put_role_policy.side_effect = _client_error("MalformedPolicyDocument")

        role = _make_role(
            inline_policies=[{"PolicyName": "p1", "PolicyDocument": {}}]
        )
        with pytest.raises(RuntimeError, match="Failed to put inline policy"):
            replicate_iam_role(role, TARGET_REGION)

    @patch("replication.iam_replication.create_target_client")
    def test_attach_managed_policy_access_denied(self, mock_create_client):
        mock_iam = MagicMock()
        mock_create_client.return_value = mock_iam
        mock_iam.create_role.return_value = {
            "Role": {"Arn": f"arn:aws:iam::{ACCOUNT_ID}:role/my-role"}
        }
        mock_iam.attach_role_policy.side_effect = _client_error("AccessDenied")

        role = _make_role(
            attached_policies=[{"PolicyArn": "arn:aws:iam::aws:policy/ReadOnly"}]
        )
        with pytest.raises(PermissionError, match="Permission denied attaching"):
            replicate_iam_role(role, TARGET_REGION)

    @patch("replication.iam_replication.create_target_client")
    def test_attach_managed_policy_unknown_error(self, mock_create_client):
        mock_iam = MagicMock()
        mock_create_client.return_value = mock_iam
        mock_iam.create_role.return_value = {
            "Role": {"Arn": f"arn:aws:iam::{ACCOUNT_ID}:role/my-role"}
        }
        mock_iam.attach_role_policy.side_effect = _client_error("NoSuchEntity")

        role = _make_role(
            attached_policies=[{"PolicyArn": "arn:aws:iam::aws:policy/Missing"}]
        )
        with pytest.raises(RuntimeError, match="Failed to attach policy"):
            replicate_iam_role(role, TARGET_REGION)
