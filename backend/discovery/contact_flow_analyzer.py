"""Contact flow analysis for Connect ACGR Resource Replicator.

Discovers all contact flows from a Connect instance, parses their JSON
definitions to extract Lex bot ARNs and Lambda function ARNs, and
cross-references with the session resource inventory.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Literal

from pydantic import BaseModel

from aws.client_factory import create_source_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class FlowReference(BaseModel):
    """A resource ARN reference found in a contact flow definition."""

    arn: str
    reference_type: Literal["LEX_BOT", "LAMBDA"]
    in_inventory: bool = False


class ContactFlowAnalysis(BaseModel):
    """Analysis result for a single contact flow."""

    flow_name: str
    flow_type: str
    flow_arn: str
    lex_references: list[FlowReference] = []
    lambda_references: list[FlowReference] = []


class FlowAnalysisResult(BaseModel):
    """Complete analysis result for all flows in an instance."""

    instance_arn: str
    total_flows: int
    analyzed_flows: int = 0
    truncated: bool = False
    flows_with_lex: int
    flows_with_lambda: int
    flows: list[ContactFlowAnalysis] = []


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _list_contact_flows(connect_client: Any, instance_id: str) -> list[dict[str, Any]]:
    """Call Connect ListContactFlows with pagination to get all flows."""
    flows: list[dict[str, Any]] = []
    params: dict[str, Any] = {"InstanceId": instance_id}

    while True:
        response = connect_client.list_contact_flows(**params)
        flows.extend(response.get("ContactFlowSummaryList", []))
        next_token = response.get("NextToken")
        if not next_token:
            break
        params["NextToken"] = next_token

    return flows


def _describe_contact_flow(
    connect_client: Any, instance_id: str, flow_id: str
) -> dict[str, Any] | None:
    """Call Connect DescribeContactFlow to get the flow content.

    Returns the ContactFlow dict or ``None`` if the call fails.
    """
    try:
        response = connect_client.describe_contact_flow(
            InstanceId=instance_id, ContactFlowId=flow_id
        )
        return response.get("ContactFlow")
    except Exception:
        logger.warning("Failed to describe contact flow %s", flow_id, exc_info=True)
        return None


def _parse_flow_content(content_json: str) -> tuple[list[str], list[str]]:
    """Parse a contact flow JSON definition and extract Lex bot and Lambda ARNs.

    Returns a tuple of ``(lex_arns, lambda_arns)``.
    """
    lex_arns: list[str] = []
    lambda_arns: list[str] = []

    try:
        content = json.loads(content_json)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Malformed contact flow JSON content, skipping")
        return lex_arns, lambda_arns

    actions = content.get("Actions", [])
    for action in actions:
        action_type = action.get("Type", "")
        params = action.get("Parameters", {})

        if action_type in ("InvokeLexBot", "InvokeLexV2Bot"):
            # Lex bot ARN can be in Parameters.LexBot.AliasArn or
            # Parameters.LexV2Bot.AliasArn
            for key in ("LexBot", "LexV2Bot"):
                bot_info = params.get(key, {})
                if isinstance(bot_info, dict):
                    alias_arn = bot_info.get("AliasArn", "")
                    if alias_arn:
                        lex_arns.append(alias_arn)

        elif action_type == "InvokeLambdaFunction":
            lambda_arn = params.get("LambdaFunctionARN", "")
            if lambda_arn:
                lambda_arns.append(lambda_arn)

    return lex_arns, lambda_arns


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_contact_flows(
    instance_id: str,
    source_region: str,
    inventory_arns: set[str] | None = None,
    max_flows: int = 0,
    concurrency: int = 5,
    progress_callback: Callable[[int, int], None] | None = None,
    deadline: float | None = None,
) -> FlowAnalysisResult:
    """Discover and analyze all contact flows for a Connect instance.

    1. Call ``ListContactFlows`` to get all flows.
    2. Call ``DescribeContactFlow`` concurrently for each to get JSON content.
    3. Parse JSON to extract Lex bot ARNs and Lambda ARNs.
    4. Cross-reference with *inventory_arns* to mark ``in_inventory``.

    Individual flow failures are logged and skipped so that the remaining
    flows are still processed.

    Args:
        instance_id: The Connect instance ID (not the full ARN).
        source_region: The AWS region of the Connect instance.
        inventory_arns: Optional set of ARN strings from the session
            inventory, used to set ``in_inventory`` on each reference.
        max_flows: Maximum number of flows to describe in detail.
            Defaults to 0 (unlimited).  Set to a positive value to cap.
        concurrency: Number of concurrent ``DescribeContactFlow`` calls.
            Defaults to 5 to stay under Connect API throttle limits.
        progress_callback: Optional callable invoked after each flow is
            analyzed.  Receives ``(analyzed_so_far, total_to_analyze)``.
        deadline: Optional Unix timestamp.  If the current time exceeds
            this value, analysis stops early and returns partial results
            with ``truncated=True``.

    Returns:
        A :class:`FlowAnalysisResult` with per-flow analysis.
    """
    if inventory_arns is None:
        inventory_arns = set()

    connect_client = create_source_client("connect", source_region)

    # Step 1 — list all contact flows
    flow_summaries = _list_contact_flows(connect_client, instance_id)
    total_listed = len(flow_summaries)
    logger.info("Found %d contact flows for instance %s", total_listed, instance_id)

    # Limit the number of flows to describe if requested
    truncated = False
    if max_flows and len(flow_summaries) > max_flows:
        logger.warning(
            "Limiting analysis to %d of %d flows",
            max_flows, len(flow_summaries),
        )
        flow_summaries = flow_summaries[:max_flows]
        truncated = True

    analyses: list[ContactFlowAnalysis] = []
    flows_with_lex = 0
    flows_with_lambda = 0
    total_to_analyze = len(flow_summaries)

    # Step 2+3 — describe and parse each flow concurrently
    def _analyze_single(summary: dict) -> ContactFlowAnalysis | None:
        flow_id = summary.get("Id", "")
        flow_name = summary.get("Name", "")
        flow_type = summary.get("ContactFlowType", "")
        flow_arn = summary.get("Arn", "")

        flow_detail = _describe_contact_flow(connect_client, instance_id, flow_id)
        if flow_detail is None:
            logger.warning("Skipping flow %s (%s) — describe failed", flow_name, flow_id)
            return None

        content_json = flow_detail.get("Content", "")
        lex_arns, lambda_arns = _parse_flow_content(content_json)

        lex_refs = [
            FlowReference(
                arn=arn,
                reference_type="LEX_BOT",
                in_inventory=arn in inventory_arns,
            )
            for arn in lex_arns
        ]
        lambda_refs = [
            FlowReference(
                arn=arn,
                reference_type="LAMBDA",
                in_inventory=arn in inventory_arns,
            )
            for arn in lambda_arns
        ]

        return ContactFlowAnalysis(
            flow_name=flow_name,
            flow_type=flow_type,
            flow_arn=flow_arn,
            lex_references=lex_refs,
            lambda_references=lambda_refs,
        )

    analyzed_count = 0
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_to_summary = {
            executor.submit(_analyze_single, s): s for s in flow_summaries
        }
        for future in as_completed(future_to_summary):
            # Check deadline
            if deadline and time.time() > deadline:
                logger.warning("Deadline reached after %d of %d flows", analyzed_count, total_to_analyze)
                truncated = True
                # Cancel remaining futures
                for f in future_to_summary:
                    f.cancel()
                break

            try:
                analysis = future.result()
            except Exception:
                logger.warning("Flow analysis future raised", exc_info=True)
                analysis = None

            if analysis is not None:
                analyses.append(analysis)
                if analysis.lex_references:
                    flows_with_lex += 1
                if analysis.lambda_references:
                    flows_with_lambda += 1

            analyzed_count += 1
            if progress_callback:
                try:
                    progress_callback(analyzed_count, total_to_analyze)
                except Exception:
                    pass

    # Build the instance ARN from the first flow ARN or fall back
    instance_arn = ""
    if flow_summaries:
        first_arn = flow_summaries[0].get("Arn", "")
        parts = first_arn.split("/")
        if len(parts) >= 2:
            instance_arn = "/".join(parts[:2])

    return FlowAnalysisResult(
        instance_arn=instance_arn,
        total_flows=total_listed,
        analyzed_flows=len(analyses),
        truncated=truncated,
        flows_with_lex=flows_with_lex,
        flows_with_lambda=flows_with_lambda,
        flows=analyses,
    )
