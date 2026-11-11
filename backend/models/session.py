"""Session state models for tracking discovery and replication lifecycle."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from .resources import ResourceBase


class ReplicationProgress(BaseModel):
    """Tracks counts of resources in each replication state."""

    total: int
    completed: int
    failed: int
    blocked: int


class ReplicationJob(BaseModel):
    """Represents a single replication execution."""

    job_id: str  # UUID
    status: Literal["IN_PROGRESS", "COMPLETED", "FAILED"]
    selected_resource_ids: list[str]
    execution_order: list[str]  # topologically sorted
    progress: ReplicationProgress
    started_at: datetime
    completed_at: datetime | None = None


class Session(BaseModel):
    """Full session state for a user interaction lifecycle.

    Tracks the Connect instance, discovered inventory, and replication jobs.
    """

    session_id: str  # UUID
    instance_arn: str
    instance_name: str
    source_region: str
    target_region: str
    resource_tags: dict[str, str] = {}  # tags applied to all replicated resources
    inventory: dict[str, ResourceBase] = {}  # resource_id -> resource
    replication_jobs: list[ReplicationJob] = []
    association_results: list[dict] | None = None  # persisted results from last association run
    sfn_execution_arn: str | None = None  # Step Functions execution ARN for active replication
    created_at: datetime
    updated_at: datetime
