"""Store for flow analysis job state — DynamoDB-backed with TTL."""

from __future__ import annotations

import base64
import gzip
import json
import logging
import os
import time
from typing import Any

import boto3

logger = logging.getLogger(__name__)

# Jobs auto-expire after 1 hour
_TTL_SECONDS = 3600


def _compress_json(obj: object) -> str:
    raw = json.dumps(obj).encode("utf-8")
    compressed = gzip.compress(raw)
    return base64.b64encode(compressed).decode("ascii")


def _decompress_json(data: str) -> object:
    compressed = base64.b64decode(data)
    raw = gzip.decompress(compressed)
    return json.loads(raw)


class FlowAnalysisStore:
    """Lightweight DynamoDB store for flow analysis jobs.

    Table schema:
        Partition key: job_id (S)
        TTL attribute: ttl (N)
    """

    def __init__(self, table_name: str | None = None, region_name: str | None = None) -> None:
        self._table_name = table_name or os.environ.get(
            "FLOW_ANALYSIS_TABLE_NAME", "FlowAnalysisJobs"
        )
        kwargs: dict = {}
        if region_name:
            kwargs["region_name"] = region_name
        self._table = boto3.resource("dynamodb", **kwargs).Table(self._table_name)

    def create_job(self, job_id: str, instance_arn: str) -> None:
        """Create a new pending job record."""
        self._table.put_item(Item={
            "job_id": job_id,
            "status": "PENDING",
            "instance_arn": instance_arn,
            "total_flows": 0,
            "analyzed_flows": 0,
            "created_at": int(time.time()),
            "ttl": int(time.time()) + _TTL_SECONDS,
        })

    def update_progress(self, job_id: str, analyzed: int, total: int) -> None:
        """Update the progress counters for a running job."""
        self._table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #s = :s, analyzed_flows = :a, total_flows = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "IN_PROGRESS",
                ":a": analyzed,
                ":t": total,
            },
        )

    def complete_job(self, job_id: str, result: dict[str, Any]) -> None:
        """Mark a job as completed and store the (compressed) result."""
        self._table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #s = :s, result_data = :r",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "COMPLETED",
                ":r": _compress_json(result),
            },
        )

    def fail_job(self, job_id: str, error: str) -> None:
        """Mark a job as failed with an error message."""
        self._table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #s = :s, error_message = :e",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "FAILED",
                ":e": error[:1000],
            },
        )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Retrieve a job record.  Decompresses result_data if present."""
        resp = self._table.get_item(Key={"job_id": job_id})
        item = resp.get("Item")
        if not item:
            return None
        # Decompress result if present
        if "result_data" in item and isinstance(item["result_data"], str):
            item["result"] = _decompress_json(item["result_data"])
            del item["result_data"]
        return item


class InMemoryFlowAnalysisStore:
    """In-memory store for local development / tests."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}

    def create_job(self, job_id: str, instance_arn: str) -> None:
        self._jobs[job_id] = {
            "job_id": job_id,
            "status": "PENDING",
            "instance_arn": instance_arn,
            "total_flows": 0,
            "analyzed_flows": 0,
        }

    def update_progress(self, job_id: str, analyzed: int, total: int) -> None:
        if job_id in self._jobs:
            self._jobs[job_id]["status"] = "IN_PROGRESS"
            self._jobs[job_id]["analyzed_flows"] = analyzed
            self._jobs[job_id]["total_flows"] = total

    def complete_job(self, job_id: str, result: dict[str, Any]) -> None:
        if job_id in self._jobs:
            self._jobs[job_id]["status"] = "COMPLETED"
            self._jobs[job_id]["result"] = result

    def fail_job(self, job_id: str, error: str) -> None:
        if job_id in self._jobs:
            self._jobs[job_id]["status"] = "FAILED"
            self._jobs[job_id]["error_message"] = error[:1000]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self._jobs.get(job_id)


def create_flow_analysis_store() -> FlowAnalysisStore | InMemoryFlowAnalysisStore:
    """Factory: DynamoDB in Lambda, in-memory locally."""
    mode = os.environ.get("DEPLOYMENT_MODE", "local").lower()
    if mode == "lambda":
        return FlowAnalysisStore()
    return InMemoryFlowAnalysisStore()
