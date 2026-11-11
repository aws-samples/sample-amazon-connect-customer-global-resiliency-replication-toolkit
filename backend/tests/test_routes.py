"""Unit tests for the validate-instance API endpoint."""

import logging
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)

VALID_ARN = "arn:aws:connect:us-west-2:123456789012:instance/abc-def-123"
VALID_INSTANCE_ID = "abc-def-123"


def _mock_describe_instance(
    instance_id="abc-def-123",
    alias="MyInstance",
    status="ACTIVE",
    identity_mgmt_type="CONNECT_MANAGED",
    replication_config=None,
):
    """Return a mock Connect DescribeInstance response."""
    resp = {
        "Instance": {
            "Id": instance_id,
            "Arn": f"arn:aws:connect:us-west-2:123456789012:instance/{instance_id}",
            "InstanceAlias": alias,
            "InstanceStatus": status,
            "IdentityManagementType": identity_mgmt_type,
        }
    }
    if replication_config is not None:
        resp["ReplicationConfiguration"] = replication_config
    return resp


class TestValidateInstanceSuccess:
    """Happy-path tests for POST /api/validate-instance."""

    @patch("api.routes.create_source_client")
    def test_returns_instance_details(self, mock_create_client):
        mock_client = MagicMock()
        mock_client.describe_instance.return_value = _mock_describe_instance()
        mock_client.list_traffic_distribution_groups.return_value = {
            "TrafficDistributionGroupSummaryList": []
        }
        mock_create_client.return_value = mock_client

        resp = client.post("/api/validate-instance", json={"instanceArn": VALID_ARN})

        assert resp.status_code == 200
        body = resp.json()
        assert body["instanceId"] == VALID_INSTANCE_ID
        assert body["instanceName"] == "MyInstance"
        assert body["instanceArn"] == VALID_ARN
        assert body["sourceRegion"] == "us-west-2"
        assert body["targetRegion"] == "us-east-1"
        assert body["status"] == "ACTIVE"
        assert body["identityManagementType"] == "CONNECT_MANAGED"
        assert body["isSaml"] is False
        assert body["hasReplica"] is False
        assert body["replicaArn"] is None

    @patch("api.routes.create_source_client")
    def test_calls_connect_with_correct_region(self, mock_create_client):
        mock_client = MagicMock()
        mock_client.describe_instance.return_value = _mock_describe_instance()
        mock_client.list_traffic_distribution_groups.return_value = {
            "TrafficDistributionGroupSummaryList": []
        }
        mock_create_client.return_value = mock_client

        client.post("/api/validate-instance", json={"instanceArn": VALID_ARN})

        mock_create_client.assert_called_once_with("connect", "us-west-2")
        mock_client.describe_instance.assert_called_once_with(InstanceId=VALID_INSTANCE_ID)

    @patch("api.routes.create_source_client")
    def test_eu_region_pair(self, mock_create_client):
        mock_client = MagicMock()
        mock_client.describe_instance.return_value = _mock_describe_instance()
        mock_client.list_traffic_distribution_groups.return_value = {
            "TrafficDistributionGroupSummaryList": []
        }
        mock_create_client.return_value = mock_client

        arn = "arn:aws:connect:eu-west-2:123456789012:instance/eu-inst-1"
        resp = client.post("/api/validate-instance", json={"instanceArn": arn})

        assert resp.status_code == 200
        body = resp.json()
        assert body["sourceRegion"] == "eu-west-2"
        assert body["targetRegion"] == "eu-central-1"

    @patch("api.routes.create_source_client")
    def test_ap_region_pair(self, mock_create_client):
        mock_client = MagicMock()
        mock_client.describe_instance.return_value = _mock_describe_instance()
        mock_client.list_traffic_distribution_groups.return_value = {
            "TrafficDistributionGroupSummaryList": []
        }
        mock_create_client.return_value = mock_client

        arn = "arn:aws:connect:ap-northeast-1:123456789012:instance/ap-inst-1"
        resp = client.post("/api/validate-instance", json={"instanceArn": arn})

        assert resp.status_code == 200
        assert resp.json()["targetRegion"] == "ap-northeast-3"


class TestValidateInstanceBadArn:
    """Tests for invalid ARN inputs — should return 400."""

    def test_invalid_arn_format(self):
        resp = client.post("/api/validate-instance", json={"instanceArn": "not-an-arn"})
        assert resp.status_code == 422

    def test_non_connect_service(self):
        arn = "arn:aws:lambda:us-west-2:123456789012:function:my-func"
        resp = client.post("/api/validate-instance", json={"instanceArn": arn})
        assert resp.status_code == 400
        assert "not a Connect resource" in resp.json()["detail"]

    def test_not_an_instance_resource(self):
        arn = "arn:aws:connect:us-west-2:123456789012:contact-flow/flow-123"
        resp = client.post("/api/validate-instance", json={"instanceArn": arn})
        assert resp.status_code == 400
        assert "not a Connect instance" in resp.json()["detail"]

    def test_unsupported_region(self):
        arn = "arn:aws:connect:ap-southeast-1:123456789012:instance/inst-1"
        resp = client.post("/api/validate-instance", json={"instanceArn": arn})
        assert resp.status_code == 400
        assert "Unsupported ACGR region" in resp.json()["detail"]

    def test_missing_instance_arn_field(self):
        resp = client.post("/api/validate-instance", json={})
        assert resp.status_code == 422  # Pydantic validation error


class TestValidateInstanceReplicaDetection:
    """Tests for replica instance detection via ReplicationConfiguration and TDG fallback."""

    @patch("api.routes.create_source_client")
    def test_saml_instance_detected(self, mock_create_client):
        mock_client = MagicMock()
        mock_client.describe_instance.return_value = _mock_describe_instance(
            identity_mgmt_type="SAML_2_0"
        )
        mock_client.list_traffic_distribution_groups.return_value = {
            "TrafficDistributionGroupSummaryList": []
        }
        mock_create_client.return_value = mock_client

        resp = client.post("/api/validate-instance", json={"instanceArn": VALID_ARN})

        assert resp.status_code == 200
        body = resp.json()
        assert body["identityManagementType"] == "SAML_2_0"
        assert body["isSaml"] is True

    @patch("api.routes.create_source_client")
    def test_replica_detected_via_replication_config(self, mock_create_client):
        mock_client = MagicMock()
        mock_client.describe_instance.return_value = _mock_describe_instance(
            replication_config={
                "SourceRegion": "us-west-2",
                "ReplicationStatusSummaryList": [
                    {
                        "Region": "us-west-2",
                        "ReplicationStatus": "INSTANCE_REPLICATION_COMPLETE",
                    },
                    {
                        "Region": "us-east-1",
                        "ReplicationStatus": "INSTANCE_REPLICATION_COMPLETE",
                    },
                ],
            }
        )
        mock_create_client.return_value = mock_client

        resp = client.post("/api/validate-instance", json={"instanceArn": VALID_ARN})

        assert resp.status_code == 200
        body = resp.json()
        assert body["hasReplica"] is True
        assert body["replicaRegion"] == "us-east-1"
        assert body["replicaStatus"] == "INSTANCE_REPLICATION_COMPLETE"
        assert "us-east-1" in body["replicaArn"]
        assert "abc-def-123" in body["replicaArn"]

    @patch("api.routes.create_source_client")
    def test_replica_detected_via_tdg_fallback(self, mock_create_client):
        mock_client = MagicMock()
        mock_client.describe_instance.return_value = _mock_describe_instance()
        mock_client.list_traffic_distribution_groups.return_value = {
            "TrafficDistributionGroupSummaryList": [
                {
                    "Id": "tdg-123",
                    "Arn": "arn:aws:connect:us-west-2:123456789012:traffic-distribution-group/tdg-123",
                    "Name": "default-tdg",
                    "Status": "ACTIVE",
                    "IsDefault": True,
                    "InstanceArn": VALID_ARN,
                }
            ]
        }
        mock_create_client.return_value = mock_client

        resp = client.post("/api/validate-instance", json={"instanceArn": VALID_ARN})

        assert resp.status_code == 200
        body = resp.json()
        assert body["hasReplica"] is True
        assert body["replicaRegion"] == "us-east-1"
        assert body["replicaStatus"] == "ACTIVE"

    @patch("api.routes.create_source_client")
    def test_no_replica_when_no_config_and_no_tdg(self, mock_create_client):
        mock_client = MagicMock()
        mock_client.describe_instance.return_value = _mock_describe_instance()
        mock_client.list_traffic_distribution_groups.return_value = {
            "TrafficDistributionGroupSummaryList": []
        }
        mock_create_client.return_value = mock_client

        resp = client.post("/api/validate-instance", json={"instanceArn": VALID_ARN})

        assert resp.status_code == 200
        body = resp.json()
        assert body["hasReplica"] is False
        assert body["replicaArn"] is None

    @patch("api.routes.create_source_client")
    def test_tdg_fallback_graceful_on_error(self, mock_create_client):
        """TDG fallback should not fail the request if the API call errors."""
        mock_client = MagicMock()
        mock_client.describe_instance.return_value = _mock_describe_instance()
        mock_client.list_traffic_distribution_groups.side_effect = Exception("Access denied")
        mock_create_client.return_value = mock_client

        resp = client.post("/api/validate-instance", json={"instanceArn": VALID_ARN})

        assert resp.status_code == 200
        body = resp.json()
        assert body["hasReplica"] is False


class TestValidateInstanceNotFound:
    """Tests for instance not found — should return 404."""

    @patch("api.routes.create_source_client")
    def test_instance_not_found(self, mock_create_client):
        mock_client = MagicMock()
        # Simulate ResourceNotFoundException
        error_response = {"Error": {"Code": "ResourceNotFoundException", "Message": "Not found"}}
        mock_client.exceptions.ResourceNotFoundException = type(
            "ResourceNotFoundException", (Exception,), {}
        )
        mock_client.describe_instance.side_effect = (
            mock_client.exceptions.ResourceNotFoundException("Not found")
        )
        mock_create_client.return_value = mock_client

        resp = client.post("/api/validate-instance", json={"instanceArn": VALID_ARN})

        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]


class TestValidateInstanceNotActive:
    """Tests for instance that exists but is not ACTIVE — should return 400."""

    @patch("api.routes.create_source_client")
    def test_instance_creation_in_progress(self, mock_create_client):
        mock_client = MagicMock()
        mock_client.describe_instance.return_value = _mock_describe_instance(
            status="CREATION_IN_PROGRESS"
        )
        mock_create_client.return_value = mock_client

        resp = client.post("/api/validate-instance", json={"instanceArn": VALID_ARN})

        assert resp.status_code == 400
        assert "not ACTIVE" in resp.json()["detail"]
        assert "CREATION_IN_PROGRESS" in resp.json()["detail"]

    @patch("api.routes.create_source_client")
    def test_instance_creation_failed(self, mock_create_client):
        mock_client = MagicMock()
        mock_client.describe_instance.return_value = _mock_describe_instance(
            status="CREATION_FAILED"
        )
        mock_create_client.return_value = mock_client

        resp = client.post("/api/validate-instance", json={"instanceArn": VALID_ARN})

        assert resp.status_code == 400
        assert "not ACTIVE" in resp.json()["detail"]


class TestValidateInstanceServerError:
    """Tests for unexpected AWS errors — should return 500."""

    @patch("api.routes.create_source_client")
    def test_unexpected_aws_error(self, mock_create_client):
        mock_client = MagicMock()
        mock_client.exceptions.ResourceNotFoundException = type(
            "ResourceNotFoundException", (Exception,), {}
        )
        mock_client.describe_instance.side_effect = RuntimeError("Connection timeout")
        mock_create_client.return_value = mock_client

        resp = client.post("/api/validate-instance", json={"instanceArn": VALID_ARN})

        assert resp.status_code == 500
        assert "Error validating Connect instance" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Task 6.1: API request validation tests
# ---------------------------------------------------------------------------


class TestRequestValidation:
    """Tests for ARN format validation on request models (Task 6.1)."""

    def test_validate_instance_empty_arn(self):
        resp = client.post("/api/validate-instance", json={"instanceArn": ""})
        assert resp.status_code == 422

    def test_validate_instance_whitespace_arn(self):
        resp = client.post("/api/validate-instance", json={"instanceArn": "   "})
        assert resp.status_code == 422

    def test_validate_instance_malformed_arn(self):
        resp = client.post("/api/validate-instance", json={"instanceArn": "arn:aws:connect"})
        assert resp.status_code == 422

    def test_validate_instance_missing_body(self):
        resp = client.post("/api/validate-instance", json={})
        assert resp.status_code == 422

    def test_discover_empty_arn(self):
        resp = client.post("/api/discover", json={"instanceArn": ""})
        assert resp.status_code == 422

    def test_discover_malformed_arn(self):
        resp = client.post("/api/discover", json={"instanceArn": "not-valid"})
        assert resp.status_code == 422

    def test_replicate_empty_session_id(self):
        resp = client.post(
            "/api/replicate",
            json={"sessionId": "  ", "resourceIds": ["r1"]},
        )
        assert resp.status_code == 422

    def test_replicate_empty_resource_ids(self):
        resp = client.post(
            "/api/replicate",
            json={"sessionId": "some-session", "resourceIds": []},
        )
        assert resp.status_code == 422

    def test_add_resource_empty_arn(self):
        resp = client.post(
            "/api/inventory/some-session/resources",
            json={"arn": ""},
        )
        assert resp.status_code == 422

    def test_add_resource_malformed_arn(self):
        resp = client.post(
            "/api/inventory/some-session/resources",
            json={"arn": "bad-arn"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Task 6.5: Audit logging middleware tests
# ---------------------------------------------------------------------------


class TestAuditLoggingMiddleware:
    """Tests for the audit logging middleware (Task 6.5)."""

    def test_audit_log_emitted_on_request(self, caplog):
        with caplog.at_level(logging.INFO):
            client.get("/api/sessions/recent")

        audit_logs = [r for r in caplog.records if "AUDIT" in r.message]
        assert len(audit_logs) >= 1
        log_msg = audit_logs[0].message
        assert "method=GET" in log_msg
        assert "path=/api/sessions/recent" in log_msg
        assert "status=200" in log_msg
        assert "timestamp=" in log_msg

    def test_audit_log_on_bad_request(self, caplog):
        with caplog.at_level(logging.INFO):
            client.post("/api/validate-instance", json={})

        audit_logs = [r for r in caplog.records if "AUDIT" in r.message]
        assert len(audit_logs) >= 1
        log_msg = audit_logs[0].message
        assert "method=POST" in log_msg
        assert "status=422" in log_msg


# ---------------------------------------------------------------------------
# Task 6.7: Manual resource addition endpoint tests
# ---------------------------------------------------------------------------


class TestAddResourceToInventory:
    """Tests for POST /api/inventory/{session_id}/resources (Task 6.7)."""

    def _create_session_in_store(self, session_id="test-session"):
        """Helper to create a session directly in the in-memory store."""
        from datetime import datetime, timezone
        from models.session import Session

        session = Session(
            session_id=session_id,
            instance_arn="arn:aws:connect:us-west-2:123456789012:instance/test-inst",
            instance_name="TestInstance",
            source_region="us-west-2",
            target_region="us-east-1",
            inventory={},
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        # Access the in-memory store directly
        from api.routes import _session_store
        _session_store._sessions[session_id] = session
        return session

    def test_add_lambda_resource(self):
        self._create_session_in_store("sess-add-1")
        arn = "arn:aws:lambda:us-west-2:123456789012:function:my-func"

        resp = client.post(
            "/api/inventory/sess-add-1/resources",
            json={"arn": arn},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["resource"]["arn"] == arn
        assert body["resource"]["resource_type"] == "LAMBDA"
        assert body["resource"]["name"] == "my-func"
        assert "resourceId" in body

    def test_add_dynamodb_resource(self):
        self._create_session_in_store("sess-add-2")
        arn = "arn:aws:kinesis:us-west-2:123456789012:stream/my-stream"

        resp = client.post(
            "/api/inventory/sess-add-2/resources",
            json={"arn": arn},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["resource"]["resource_type"] == "KINESIS_STREAM"
        assert body["resource"]["name"] == "my-stream"

    def test_add_resource_session_not_found(self):
        resp = client.post(
            "/api/inventory/nonexistent-session/resources",
            json={"arn": "arn:aws:lambda:us-west-2:123456789012:function:f"},
        )
        assert resp.status_code == 404
        assert "Session not found" in resp.json()["detail"]

    def test_add_resource_unsupported_service(self):
        self._create_session_in_store("sess-add-3")
        arn = "arn:aws:ec2:us-west-2:123456789012:instance/i-1234567890abcdef0"

        resp = client.post(
            "/api/inventory/sess-add-3/resources",
            json={"arn": arn},
        )

        assert resp.status_code == 400
        assert "Unsupported service" in resp.json()["detail"]

    def test_add_resource_invalid_arn(self):
        resp = client.post(
            "/api/inventory/some-session/resources",
            json={"arn": "not-an-arn"},
        )
        assert resp.status_code == 422

    def test_add_duplicate_arn(self):
        self._create_session_in_store("sess-add-4")
        arn = "arn:aws:lambda:us-west-2:123456789012:function:dup-func"

        # First add should succeed
        resp1 = client.post(
            "/api/inventory/sess-add-4/resources",
            json={"arn": arn},
        )
        assert resp1.status_code == 200

        # Second add of same ARN should fail with 409
        resp2 = client.post(
            "/api/inventory/sess-add-4/resources",
            json={"arn": arn},
        )
        assert resp2.status_code == 409
        assert "already exists" in resp2.json()["detail"]

    def test_add_iam_role_resource(self):
        self._create_session_in_store("sess-add-5")
        arn = "arn:aws:iam:us-west-2:123456789012:role/my-role"

        resp = client.post(
            "/api/inventory/sess-add-5/resources",
            json={"arn": arn},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["resource"]["resource_type"] == "IAM_ROLE"
        assert body["resource"]["name"] == "my-role"


class TestAuditEndpoint:
    """Tests for POST /api/audit/{session_id}."""

    def _create_session_with_inventory(self, session_id="audit-session"):
        """Helper to create a session with resources in the in-memory store."""
        from datetime import datetime, timezone
        from models.enums import ReplicationStatus, ResourceType
        from models.resources import ResourceBase
        from models.session import Session

        resources = {
            "lambda-1": ResourceBase(
                id="lambda-1",
                name="my-func",
                arn="arn:aws:lambda:us-west-2:123456789012:function:my-func",
                resource_type=ResourceType.LAMBDA,
                status=ReplicationStatus.NOT_REPLICATED,
            ),
            "dynamo-1": ResourceBase(
                id="dynamo-1",
                name="my-stream",
                arn="arn:aws:kinesis:us-west-2:123456789012:stream/my-stream",
                resource_type=ResourceType.KINESIS_STREAM,
                status=ReplicationStatus.NOT_REPLICATED,
            ),
        }

        session = Session(
            session_id=session_id,
            instance_arn="arn:aws:connect:us-west-2:123456789012:instance/test-inst",
            instance_name="TestInstance",
            source_region="us-west-2",
            target_region="us-east-1",
            inventory=resources,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        from api.routes import _session_store
        _session_store._sessions[session_id] = session
        return session

    def test_audit_session_not_found(self):
        resp = client.post(
            "/api/audit/nonexistent-session",
            json={"resourceTags": {}},
        )
        assert resp.status_code == 404
        assert "Session not found" in resp.json()["detail"]

    @patch("api.routes.audit_target_region")
    def test_audit_with_valid_session(self, mock_audit):
        from audit.target_region_audit import AuditResult, ResourceAuditEntry

        mock_audit.return_value = AuditResult(
            entries={
                "lambda-1": ResourceAuditEntry(
                    resource_id="lambda-1",
                    exists_in_target=True,
                    target_arn="arn:aws:lambda:us-east-1:123456789012:function:my-func-dr",
                ),
                "dynamo-1": ResourceAuditEntry(
                    resource_id="dynamo-1",
                    exists_in_target=False,
                ),
            },
            already_replicated_count=1,
            missing_count=1,
            error_count=0,
        )

        self._create_session_with_inventory("audit-valid")

        resp = client.post(
            "/api/audit/audit-valid",
            json={"resourceTags": {}},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["sessionId"] == "audit-valid"
        assert body["auditSummary"]["alreadyReplicated"] == 1
        assert body["auditSummary"]["missing"] == 1
        assert body["auditSummary"]["errors"] == 0
        assert len(body["inventory"]) == 2

        # Verify the audit function was called with correct args
        mock_audit.assert_called_once()

    @patch("api.routes.audit_target_region")
    def test_audit_updates_resource_statuses(self, mock_audit):
        from audit.target_region_audit import AuditResult, ResourceAuditEntry
        from models.enums import ReplicationStatus

        mock_audit.return_value = AuditResult(
            entries={
                "lambda-1": ResourceAuditEntry(
                    resource_id="lambda-1",
                    exists_in_target=True,
                    target_arn="arn:aws:lambda:us-east-1:123456789012:function:my-func-dr",
                ),
                "dynamo-1": ResourceAuditEntry(
                    resource_id="dynamo-1",
                    exists_in_target=False,
                ),
            },
            already_replicated_count=1,
            missing_count=1,
            error_count=0,
        )

        session = self._create_session_with_inventory("audit-status")

        resp = client.post(
            "/api/audit/audit-status",
            json={"resourceTags": {}},
        )

        assert resp.status_code == 200

        # Verify session inventory was updated
        assert session.inventory["lambda-1"].status == ReplicationStatus.REPLICATED
        assert session.inventory["lambda-1"].replicated_arn == "arn:aws:lambda:us-east-1:123456789012:function:my-func-dr"
        assert session.inventory["dynamo-1"].status == ReplicationStatus.NOT_REPLICATED

    @patch("api.routes.audit_target_region")
    def test_audit_stores_resource_tags(self, mock_audit):
        from audit.target_region_audit import AuditResult

        mock_audit.return_value = AuditResult(
            entries={},
            already_replicated_count=0,
            missing_count=0,
            error_count=0,
        )

        session = self._create_session_with_inventory("audit-prefix")

        resp = client.post(
            "/api/audit/audit-prefix",
            json={"resourceTags": {"env": "dr", "team": "acgr"}},
        )

        assert resp.status_code == 200
        assert session.resource_tags == {"env": "dr", "team": "acgr"}

    @patch("api.routes.audit_target_region")
    def test_audit_with_error_entries(self, mock_audit):
        from audit.target_region_audit import AuditResult, ResourceAuditEntry
        from models.enums import ReplicationStatus

        mock_audit.return_value = AuditResult(
            entries={
                "lambda-1": ResourceAuditEntry(
                    resource_id="lambda-1",
                    exists_in_target=False,
                    audit_error="Access denied",
                ),
                "dynamo-1": ResourceAuditEntry(
                    resource_id="dynamo-1",
                    exists_in_target=False,
                ),
            },
            already_replicated_count=0,
            missing_count=1,
            error_count=1,
        )

        session = self._create_session_with_inventory("audit-errors")

        resp = client.post(
            "/api/audit/audit-errors",
            json={"resourceTags": {}},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["auditSummary"]["errors"] == 1

        # Resource with audit error should be NOT_REPLICATED with error set
        assert session.inventory["lambda-1"].status == ReplicationStatus.NOT_REPLICATED
        assert session.inventory["lambda-1"].error == "Access denied"
        # Resource without error should be NOT_REPLICATED with no error
        assert session.inventory["dynamo-1"].status == ReplicationStatus.NOT_REPLICATED


# ---------------------------------------------------------------------------
# Task 12: Tests for session status endpoint and association retry endpoint
# ---------------------------------------------------------------------------


class TestSessionStatusEndpoint:
    """Tests for GET /session/{session_id}/status."""

    def _create_session_with_replication(self, session_id="status-session"):
        """Helper to create a session with replicated resources and a replication job."""
        from datetime import datetime, timezone
        from models.enums import ReplicationStatus, ResourceType
        from models.resources import ResourceBase
        from models.session import Session, ReplicationJob, ReplicationProgress

        resources = {
            "lambda-1": ResourceBase(
                id="lambda-1",
                name="my-func",
                arn="arn:aws:lambda:us-west-2:123456789012:function:my-func",
                resource_type=ResourceType.LAMBDA,
                status=ReplicationStatus.REPLICATED,
                replicated_arn="arn:aws:lambda:us-east-1:123456789012:function:my-func",
            ),
            "lex-1": ResourceBase(
                id="lex-1",
                name="my-bot",
                arn="arn:aws:lex:us-west-2:123456789012:bot/BOTID",
                resource_type=ResourceType.LEX_BOT,
                status=ReplicationStatus.REPLICATED,
                replicated_arn="arn:aws:lex:us-east-1:123456789012:bot/BOTID",
            ),
        }

        now = datetime.now(timezone.utc)
        job = ReplicationJob(
            job_id="job-123",
            status="COMPLETED",
            selected_resource_ids=["lambda-1", "lex-1"],
            execution_order=["lambda-1", "lex-1"],
            progress=ReplicationProgress(total=2, completed=2, failed=0, blocked=0),
            started_at=now,
            completed_at=now,
        )

        session = Session(
            session_id=session_id,
            instance_arn="arn:aws:connect:us-west-2:123456789012:instance/test-inst",
            instance_name="TestInstance",
            source_region="us-west-2",
            target_region="us-east-1",
            inventory=resources,
            replication_jobs=[job],
            created_at=now,
            updated_at=now,
        )
        from api.routes import _session_store
        _session_store._sessions[session_id] = session
        return session

    def test_session_not_found(self):
        resp = client.get("/api/session/nonexistent/status")
        assert resp.status_code == 404
        assert "Session not found" in resp.json()["detail"]

    def test_returns_session_status(self):
        self._create_session_with_replication("status-1")
        resp = client.get("/api/session/status-1/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["sessionId"] == "status-1"
        assert body["sourceRegion"] == "us-west-2"
        assert body["targetRegion"] == "us-east-1"
        assert len(body["inventory"]) == 2
        assert len(body["replicationJobs"]) == 1
        assert body["replicationJobs"][0]["jobId"] == "job-123"
        assert body["replicationJobs"][0]["status"] == "COMPLETED"

    def test_inventory_includes_association_status(self):
        session = self._create_session_with_replication("status-2")
        session.association_results = [
            {"resource": "my-func", "resource_type": "LAMBDA", "status": "associated", "message": "OK"},
            {"resource": "my-bot", "resource_type": "LEX_BOT", "status": "pending", "message": "Replicating", "retryable": True},
        ]

        resp = client.get("/api/session/status-2/status")
        assert resp.status_code == 200
        body = resp.json()

        inv_map = {item["name"]: item for item in body["inventory"]}
        assert inv_map["my-func"]["association_status"] == "associated"
        assert inv_map["my-bot"]["association_status"] == "pending"
        assert inv_map["my-bot"]["retryable"] is True

    def test_no_association_results(self):
        self._create_session_with_replication("status-3")
        resp = client.get("/api/session/status-3/status")
        assert resp.status_code == 200
        body = resp.json()
        # All should be "not_attempted"
        for item in body["inventory"]:
            assert item["association_status"] == "not_attempted"


class TestAssociationRetryEndpoint:
    """Tests for POST /associate/{session_id}/retry/{resource_id}."""

    def _create_session_with_replicated_resource(self, session_id="retry-session"):
        from datetime import datetime, timezone
        from models.enums import ReplicationStatus, ResourceType
        from models.resources import ResourceBase
        from models.session import Session

        resources = {
            "lambda-1": ResourceBase(
                id="lambda-1",
                name="my-func",
                arn="arn:aws:lambda:us-west-2:123456789012:function:my-func",
                resource_type=ResourceType.LAMBDA,
                status=ReplicationStatus.REPLICATED,
                replicated_arn="arn:aws:lambda:us-east-1:123456789012:function:my-func",
            ),
        }

        session = Session(
            session_id=session_id,
            instance_arn="arn:aws:connect:us-west-2:123456789012:instance/test-inst",
            instance_name="TestInstance",
            source_region="us-west-2",
            target_region="us-east-1",
            inventory=resources,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        from api.routes import _session_store
        _session_store._sessions[session_id] = session
        return session

    def test_session_not_found(self):
        resp = client.post("/api/associate/nonexistent/retry/lambda-1")
        assert resp.status_code == 404
        assert "Session not found" in resp.json()["detail"]

    @patch("association.resource_association.associate_single_resource")
    def test_successful_retry(self, mock_assoc):
        mock_assoc.return_value = {
            "resource": "my-func",
            "resource_type": "LAMBDA",
            "status": "associated",
            "message": "Lambda function associated with DR instance",
        }
        self._create_session_with_replicated_resource("retry-1")

        resp = client.post("/api/associate/retry-1/retry/lambda-1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "associated"
        assert body["resource"] == "my-func"

    @patch("association.resource_association.associate_single_resource")
    def test_retry_persists_results(self, mock_assoc):
        mock_assoc.return_value = {
            "resource": "my-func",
            "resource_type": "LAMBDA",
            "status": "associated",
            "message": "OK",
        }
        session = self._create_session_with_replicated_resource("retry-2")
        session.association_results = [
            {"resource": "my-func", "resource_type": "LAMBDA", "status": "error", "error": "timeout"},
        ]

        resp = client.post("/api/associate/retry-2/retry/lambda-1")
        assert resp.status_code == 200

        # Verify the association_results were updated
        assert session.association_results is not None
        updated = [r for r in session.association_results if r["resource"] == "my-func"]
        assert len(updated) == 1
        assert updated[0]["status"] == "associated"

    @patch("association.resource_association.associate_single_resource")
    def test_retry_error(self, mock_assoc):
        mock_assoc.side_effect = Exception("Connection timeout")
        self._create_session_with_replicated_resource("retry-3")

        resp = client.post("/api/associate/retry-3/retry/lambda-1")
        assert resp.status_code == 500
        assert "Association retry failed" in resp.json()["detail"]
