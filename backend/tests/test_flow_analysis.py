"""Tests for async contact flow analysis endpoints and store."""

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from store.flow_analysis_store import InMemoryFlowAnalysisStore

client = TestClient(app)

VALID_INSTANCE_ARN = "arn:aws:connect:us-east-1:123456789012:instance/abc-def-123"


class TestInMemoryFlowAnalysisStore:
    """Unit tests for the in-memory flow analysis store."""

    def test_create_and_get_job(self):
        store = InMemoryFlowAnalysisStore()
        store.create_job("job-1", VALID_INSTANCE_ARN)
        job = store.get_job("job-1")
        assert job is not None
        assert job["status"] == "PENDING"
        assert job["instance_arn"] == VALID_INSTANCE_ARN

    def test_get_nonexistent_job(self):
        store = InMemoryFlowAnalysisStore()
        assert store.get_job("nope") is None

    def test_update_progress(self):
        store = InMemoryFlowAnalysisStore()
        store.create_job("job-1", VALID_INSTANCE_ARN)
        store.update_progress("job-1", 50, 200)
        job = store.get_job("job-1")
        assert job["status"] == "IN_PROGRESS"
        assert job["analyzed_flows"] == 50
        assert job["total_flows"] == 200

    def test_complete_job(self):
        store = InMemoryFlowAnalysisStore()
        store.create_job("job-1", VALID_INSTANCE_ARN)
        result = {"total_flows": 100, "flows": []}
        store.complete_job("job-1", result)
        job = store.get_job("job-1")
        assert job["status"] == "COMPLETED"
        assert job["result"] == result

    def test_fail_job(self):
        store = InMemoryFlowAnalysisStore()
        store.create_job("job-1", VALID_INSTANCE_ARN)
        store.fail_job("job-1", "Something broke")
        job = store.get_job("job-1")
        assert job["status"] == "FAILED"
        assert job["error_message"] == "Something broke"

    def test_fail_job_truncates_long_error(self):
        store = InMemoryFlowAnalysisStore()
        store.create_job("job-1", VALID_INSTANCE_ARN)
        store.fail_job("job-1", "x" * 2000)
        job = store.get_job("job-1")
        assert len(job["error_message"]) == 1000


class TestAnalyzeContactFlowsEndpoint:
    """Tests for POST /api/contact-flows/analyze (async)."""

    @patch("api.routes._invoke_flow_analysis_async")
    def test_returns_job_id(self, mock_invoke):
        resp = client.post("/api/contact-flows/analyze", json={
            "instance_arn": VALID_INSTANCE_ARN,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert "job_id" in body
        assert body["status"] == "PENDING"
        mock_invoke.assert_called_once()

    @patch("api.routes._invoke_flow_analysis_async")
    def test_with_session_id(self, mock_invoke):
        resp = client.post("/api/contact-flows/analyze", json={
            "instance_arn": VALID_INSTANCE_ARN,
            "session_id": "sess-123",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert "job_id" in body
        # Verify session_id was passed through
        call_kwargs = mock_invoke.call_args
        assert call_kwargs[1]["session_id"] == "sess-123" or call_kwargs[0][2] == "sess-123"

    def test_invalid_arn_rejected(self):
        resp = client.post("/api/contact-flows/analyze", json={
            "instance_arn": "not-an-arn",
        })
        assert resp.status_code == 422 or resp.status_code == 400

    @patch("api.routes._invoke_flow_analysis_async")
    def test_non_instance_arn_rejected(self, mock_invoke):
        resp = client.post("/api/contact-flows/analyze", json={
            "instance_arn": "arn:aws:connect:us-east-1:123456789012:contact-flow/flow-1",
        })
        assert resp.status_code == 400
        assert "not a Connect instance" in resp.json()["detail"]


class TestFlowAnalysisStatusEndpoint:
    """Tests for GET /api/contact-flows/analyze/{job_id}/status."""

    def test_job_not_found(self):
        resp = client.get("/api/contact-flows/analyze/nonexistent-job/status")
        assert resp.status_code == 404

    @patch("api.routes._get_flow_analysis_store")
    def test_pending_job(self, mock_store_fn):
        store = InMemoryFlowAnalysisStore()
        store.create_job("job-1", VALID_INSTANCE_ARN)
        mock_store_fn.return_value = store

        resp = client.get("/api/contact-flows/analyze/job-1/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "PENDING"
        assert body["total_flows"] == 0
        assert body["flows"] is None

    @patch("api.routes._get_flow_analysis_store")
    def test_in_progress_job(self, mock_store_fn):
        store = InMemoryFlowAnalysisStore()
        store.create_job("job-1", VALID_INSTANCE_ARN)
        store.update_progress("job-1", 50, 200)
        mock_store_fn.return_value = store

        resp = client.get("/api/contact-flows/analyze/job-1/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "IN_PROGRESS"
        assert body["analyzed_flows"] == 50
        assert body["total_flows"] == 200

    @patch("api.routes._get_flow_analysis_store")
    def test_completed_job(self, mock_store_fn):
        store = InMemoryFlowAnalysisStore()
        store.create_job("job-1", VALID_INSTANCE_ARN)
        result = {
            "total_flows": 100,
            "analyzed_flows": 100,
            "truncated": False,
            "flows_with_lex": 5,
            "flows_with_lambda": 10,
            "flows": [{"flow_name": "Test", "flow_type": "CONTACT_FLOW"}],
        }
        store.complete_job("job-1", result)
        mock_store_fn.return_value = store

        resp = client.get("/api/contact-flows/analyze/job-1/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "COMPLETED"
        assert body["flows_with_lex"] == 5
        assert body["flows_with_lambda"] == 10
        assert len(body["flows"]) == 1

    @patch("api.routes._get_flow_analysis_store")
    def test_failed_job(self, mock_store_fn):
        store = InMemoryFlowAnalysisStore()
        store.create_job("job-1", VALID_INSTANCE_ARN)
        store.fail_job("job-1", "Boom")
        mock_store_fn.return_value = store

        resp = client.get("/api/contact-flows/analyze/job-1/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "FAILED"
        assert body["error"] == "Boom"


class TestContactFlowAnalyzerConcurrency:
    """Tests for the concurrent contact flow analyzer."""

    @patch("discovery.contact_flow_analyzer.create_source_client")
    def test_concurrent_analysis(self, mock_create_client):
        from discovery.contact_flow_analyzer import analyze_contact_flows

        mock_client = MagicMock()
        mock_client.list_contact_flows.return_value = {
            "ContactFlowSummaryList": [
                {"Id": f"flow-{i}", "Name": f"Flow {i}", "ContactFlowType": "CONTACT_FLOW",
                 "Arn": f"arn:aws:connect:us-east-1:123456789012:instance/inst-1/contact-flow/flow-{i}"}
                for i in range(10)
            ]
        }
        mock_client.describe_contact_flow.return_value = {
            "ContactFlow": {
                "Content": json.dumps({
                    "Actions": [
                        {"Type": "InvokeLambdaFunction", "Parameters": {"LambdaFunctionARN": "arn:aws:lambda:us-east-1:123456789012:function:my-func"}}
                    ]
                })
            }
        }
        mock_create_client.return_value = mock_client

        progress_calls = []
        def on_progress(analyzed, total):
            progress_calls.append((analyzed, total))

        result = analyze_contact_flows(
            instance_id="inst-1",
            source_region="us-east-1",
            concurrency=3,
            progress_callback=on_progress,
        )

        assert result.total_flows == 10
        assert result.analyzed_flows == 10
        assert result.flows_with_lambda == 10
        assert not result.truncated
        assert len(progress_calls) == 10

    @patch("discovery.contact_flow_analyzer.create_source_client")
    def test_deadline_stops_early(self, mock_create_client):
        from discovery.contact_flow_analyzer import analyze_contact_flows

        mock_client = MagicMock()
        mock_client.list_contact_flows.return_value = {
            "ContactFlowSummaryList": [
                {"Id": f"flow-{i}", "Name": f"Flow {i}", "ContactFlowType": "CONTACT_FLOW",
                 "Arn": f"arn:aws:connect:us-east-1:123456789012:instance/inst-1/contact-flow/flow-{i}"}
                for i in range(100)
            ]
        }

        def slow_describe(**kwargs):
            time.sleep(0.05)
            return {"ContactFlow": {"Content": json.dumps({"Actions": []})}}

        mock_client.describe_contact_flow.side_effect = slow_describe
        mock_create_client.return_value = mock_client

        # Set deadline to now (should stop almost immediately)
        result = analyze_contact_flows(
            instance_id="inst-1",
            source_region="us-east-1",
            concurrency=2,
            deadline=time.time(),
        )

        assert result.total_flows == 100
        assert result.truncated is True
        # Should have analyzed fewer than all 100
        assert result.analyzed_flows < 100
