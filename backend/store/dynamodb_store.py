"""DynamoDB-backed session store for serverless deployment."""

from __future__ import annotations

import base64
import gzip
import json
import time
from datetime import datetime, timezone

import boto3

from models.resources import (
    ApprovedOriginResource,
    IAMRoleResource,
    KinesisFirehoseResource,
    KinesisStreamResource,
    KinesisVideoResource,
    LambdaResource,
    LexBotResource,
    ResourceBase,
    S3BucketResource,
)
from models.enums import ResourceType
from models.session import ReplicationJob, Session

from .base import SessionStore, SessionSummary

# 24-hour TTL for session cleanup
_TTL_SECONDS = 24 * 60 * 60

# Mapping from resource_type enum value to the correct subclass
_RESOURCE_TYPE_MAP: dict[str, type[ResourceBase]] = {
    ResourceType.LAMBDA: LambdaResource,
    ResourceType.LEX_BOT: LexBotResource,
    ResourceType.KINESIS_STREAM: KinesisStreamResource,
    ResourceType.KINESIS_FIREHOSE: KinesisFirehoseResource,
    ResourceType.KINESIS_VIDEO_STREAM: KinesisVideoResource,
    ResourceType.IAM_ROLE: IAMRoleResource,
    ResourceType.S3_BUCKET: S3BucketResource,
    ResourceType.APPROVED_ORIGIN: ApprovedOriginResource,
}


def _deserialize_resource(data: dict) -> ResourceBase:
    """Deserialize a resource dict into the correct subclass based on resource_type."""
    resource_type = data.get("resource_type", "")
    model_class = _RESOURCE_TYPE_MAP.get(resource_type, ResourceBase)
    return model_class.model_validate(data)


class DynamoDBSessionStore(SessionStore):
    """DynamoDB single-table session store.

    Table schema:
        Partition key: session_id (S)
        TTL attribute: ttl (N)
    """

    def __init__(self, table_name: str = "ReplicatorSessions", region_name: str | None = None) -> None:
        self._table_name = table_name
        kwargs: dict = {}
        if region_name:
            kwargs["region_name"] = region_name
        self._table = boto3.resource("dynamodb", **kwargs).Table(table_name)

    async def get_session(self, session_id: str) -> Session | None:
        resp = self._table.get_item(Key={"session_id": session_id})
        item = resp.get("Item")
        if not item:
            return None
        return _deserialize_session(item)

    async def save_session(self, session: Session) -> None:
        item = _serialize_session(session)
        self._table.put_item(Item=item)

    async def update_resource_status(
        self,
        session_id: str,
        resource_id: str,
        status: str,
        replicated_arn: str | None = None,
        error: str | None = None,
        error_classification: dict | None = None,
    ) -> None:
        """Atomically update one resource's status in the ``repl_status`` map.

        Uses a DynamoDB ``SET repl_status.#rid = :val`` update. Concurrent
        updates to *different* map keys are applied atomically by DynamoDB and
        do NOT clobber each other — unlike the whole-item ``put_item`` used by
        save_session. This is what makes parallel Step Functions replication
        persist every resource's status correctly.
        """
        value = {
            "status": status,
            "replicated_arn": replicated_arn,
            "error": error,
            # JSON-encode to sidestep DynamoDB float/Decimal constraints.
            "error_classification": json.dumps(error_classification) if error_classification else None,
        }
        try:
            self._table.update_item(
                Key={"session_id": session_id},
                UpdateExpression="SET repl_status.#rid = :val, updated_at = :ua",
                ExpressionAttributeNames={"#rid": resource_id},
                ExpressionAttributeValues={":val": value, ":ua": datetime.now(timezone.utc).isoformat()},
                ConditionExpression="attribute_exists(session_id) AND attribute_exists(repl_status)",
            )
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            # repl_status map not initialized yet (legacy item) — create it,
            # then retry the targeted key update.
            self._table.update_item(
                Key={"session_id": session_id},
                UpdateExpression="SET repl_status = if_not_exists(repl_status, :empty)",
                ExpressionAttributeValues={":empty": {}},
                ConditionExpression="attribute_exists(session_id)",
            )
            self._table.update_item(
                Key={"session_id": session_id},
                UpdateExpression="SET repl_status.#rid = :val, updated_at = :ua",
                ExpressionAttributeNames={"#rid": resource_id},
                ExpressionAttributeValues={":val": value, ":ua": datetime.now(timezone.utc).isoformat()},
            )

    async def delete_session(self, session_id: str) -> None:
        self._table.delete_item(Key={"session_id": session_id})

    async def list_recent_sessions(self, limit: int = 4) -> list[SessionSummary]:
        """Scan the table for recent sessions, sorted by created_at desc.

        DynamoDB Scan is fine here — the table is small (dozens of items).
        We fetch lightweight projections to avoid deserializing full inventories.
        """
        import logging
        _logger = logging.getLogger(__name__)

        try:
            resp = self._table.scan(
                ProjectionExpression="session_id, instance_arn, source_region, target_region, created_at, updated_at, resource_count",
            )
            items = resp.get("Items", [])

            # Handle pagination (unlikely with small table, but correct)
            while "LastEvaluatedKey" in resp:
                resp = self._table.scan(
                    ProjectionExpression="session_id, instance_arn, source_region, target_region, created_at, updated_at, resource_count",
                    ExclusiveStartKey=resp["LastEvaluatedKey"],
                )
                items.extend(resp.get("Items", []))

            # Sort by created_at descending and take top N
            items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            items = items[:limit]

            return [
                SessionSummary(
                    session_id=item["session_id"],
                    instance_arn=item.get("instance_arn", ""),
                    source_region=item.get("source_region", ""),
                    target_region=item.get("target_region", ""),
                    created_at=item.get("created_at", ""),
                    updated_at=item.get("updated_at", ""),
                    resource_count=int(item.get("resource_count", 0)),
                )
                for item in items
            ]
        except Exception:
            _logger.warning("Failed to list recent sessions", exc_info=True)
            return []


def _compress_json(obj: object) -> str:
    """JSON-serialize, gzip-compress, and base64-encode an object.

    DynamoDB has a 400KB item size limit. Inventory payloads with full Lex
    bot definitions (intents, slots, utterances, slot type values) can easily
    exceed that. Gzip typically achieves 5-10x compression on JSON text.
    """
    raw = json.dumps(obj).encode("utf-8")
    compressed = gzip.compress(raw)
    return base64.b64encode(compressed).decode("ascii")


def _decompress_json(data: str) -> object:
    """Reverse of _compress_json: base64-decode, gunzip, JSON-parse."""
    compressed = base64.b64decode(data)
    raw = gzip.decompress(compressed)
    return json.loads(raw)


def _serialize_session(session: Session) -> dict:
    """Convert a Session to a DynamoDB item dict.

    The inventory and replication_jobs fields are gzip-compressed to stay
    within DynamoDB's 400KB item size limit.
    """
    inventory_obj = {rid: r.model_dump(mode="json") for rid, r in session.inventory.items()}
    jobs_obj = [j.model_dump(mode="json") for j in session.replication_jobs]

    item = {
        "session_id": session.session_id,
        "instance_arn": session.instance_arn,
        "instance_name": session.instance_name,
        "source_region": session.source_region,
        "target_region": session.target_region,
        "inventory": _compress_json(inventory_obj),
        "replication_jobs": _compress_json(jobs_obj),
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "ttl": int(time.time()) + _TTL_SECONDS,
        "_compressed": True,  # marker so deserializer knows the format
        "resource_count": len(session.inventory),
        # Uncompressed per-resource status map — the authoritative, atomically
        # updatable copy of each resource's replication status. Individual keys
        # are updated concurrency-safely via update_resource_status(); on read
        # this overlays the (possibly stale) compressed inventory blob.
        "repl_status": {
            rid: {
                "status": r.status.value if hasattr(r.status, "value") else str(r.status),
                "replicated_arn": r.replicated_arn,
                "error": r.error,
                "error_classification": json.dumps(r.error_classification) if r.error_classification else None,
            }
            for rid, r in session.inventory.items()
        },
    }

    # Optional fields — only write when present to avoid storing nulls
    if session.resource_tags:
        item["resource_tags"] = json.dumps(session.resource_tags)
    if session.association_results is not None:
        item["association_results"] = _compress_json(session.association_results)
    if session.sfn_execution_arn:
        item["sfn_execution_arn"] = session.sfn_execution_arn

    return item


def _deserialize_session(item: dict) -> Session:
    """Reconstruct a Session from a DynamoDB item dict.

    Supports both compressed (new) and plain JSON (legacy) formats.
    """
    is_compressed = item.get("_compressed", False)

    if is_compressed:
        inventory_raw: dict = _decompress_json(item["inventory"])
        jobs_raw: list = _decompress_json(item["replication_jobs"])
    else:
        inventory_raw = json.loads(item["inventory"])
        jobs_raw = json.loads(item["replication_jobs"])

    inventory = {
        rid: _deserialize_resource(data)
        for rid, data in inventory_raw.items()
    }

    # Overlay the authoritative per-resource status map (repl_status) onto the
    # inventory. This map is updated atomically per resource, so it reflects the
    # latest status even when the compressed inventory blob is stale (e.g. after
    # concurrent Step Functions replication). Legacy items without repl_status
    # keep the inventory's own status.
    repl_status = item.get("repl_status")
    if isinstance(repl_status, dict):
        from models.enums import ReplicationStatus as _RS
        for rid, st in repl_status.items():
            resource = inventory.get(rid)
            if resource is None or not isinstance(st, dict):
                continue
            status_val = st.get("status")
            if status_val:
                try:
                    resource.status = _RS(status_val)
                except ValueError:
                    pass
            resource.replicated_arn = st.get("replicated_arn")
            resource.error = st.get("error")
            ec = st.get("error_classification")
            if ec:
                try:
                    resource.error_classification = json.loads(ec) if isinstance(ec, str) else ec
                except (ValueError, TypeError):
                    resource.error_classification = None
            else:
                resource.error_classification = None

    jobs = [ReplicationJob.model_validate(j) for j in jobs_raw]

    # Optional fields
    resource_tags: dict[str, str] = {}
    if "resource_tags" in item:
        resource_tags = json.loads(item["resource_tags"]) if isinstance(item["resource_tags"], str) else item["resource_tags"]

    association_results: list[dict] | None = None
    if "association_results" in item:
        if is_compressed:
            association_results = _decompress_json(item["association_results"])
        else:
            association_results = json.loads(item["association_results"]) if isinstance(item["association_results"], str) else item["association_results"]

    sfn_execution_arn: str | None = item.get("sfn_execution_arn")

    return Session(
        session_id=item["session_id"],
        instance_arn=item["instance_arn"],
        instance_name=item["instance_name"],
        source_region=item["source_region"],
        target_region=item["target_region"],
        resource_tags=resource_tags,
        inventory=inventory,
        replication_jobs=jobs,
        association_results=association_results,
        sfn_execution_arn=sfn_execution_arn,
        created_at=datetime.fromisoformat(item["created_at"]),
        updated_at=datetime.fromisoformat(item["updated_at"]),
    )
