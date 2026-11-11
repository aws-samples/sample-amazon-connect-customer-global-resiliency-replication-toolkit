"""Contact flow analysis for Connect ACGR Resource Replicator.

Discovers all contact flows from a Connect instance, parses their JSON
definitions to extract Lex bot ARNs and Lambda function ARNs, and
cross-references with the session resource inventory.
"""

from __future__ import annotations

import json
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Literal

from botocore.exceptions import ClientError
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
    # Flows that could not be read (still failing after retries). They are NOT in
    # ``flows``, so any Lex/Lambda references inside them are unknown.
    skipped_flows: int = 0
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


# Connect rate-limits DescribeContactFlow; several concurrent callers get these.
_THROTTLE_CODES = frozenset({
    "TooManyRequestsException",
    "ThrottlingException",
    "Throttling",
    "RequestLimitExceeded",
})
_DESCRIBE_MAX_ATTEMPTS = 8


def _describe_contact_flow(
    connect_client: Any, instance_id: str, flow_id: str,
    max_attempts: int = _DESCRIBE_MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any] | None:
    """Call Connect DescribeContactFlow to get the flow content.

    Throttling is retried with exponential backoff and jitter (about 0.5 s up
    to 8 s between tries). Any other error is not retried. Returns ``None``
    when the flow could not be read; the caller counts it as skipped.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            response = connect_client.describe_contact_flow(
                InstanceId=instance_id, ContactFlowId=flow_id
            )
            return response.get("ContactFlow")
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _THROTTLE_CODES and attempt < max_attempts:
                delay = min(8.0, 0.5 * (2 ** (attempt - 1))) * (0.5 + random.random() / 2)
                sleep(delay)
                continue
            logger.warning(
                "Failed to describe contact flow %s (%s, attempt %d/%d)",
                flow_id, code or "ClientError", attempt, max_attempts,
            )
            return None
        except Exception:
            logger.warning("Failed to describe contact flow %s", flow_id, exc_info=True)
            return None
    return None


_LEX_ACTION_TYPES = ("ConnectParticipantWithLexBot", "InvokeLexBot", "InvokeLexV2Bot")


def _lex_ref_in_inventory(ref: str, inventory_arns: set[str]) -> bool:
    """True if a flow's Lex reference matches a bot in the inventory.

    The inventory holds bot ARNs (V2 ``...:bot/<ID>``, V1 ``...:bot:<Name>``)
    while flows reference a V2 alias ARN (``...:bot-alias/<ID>/<ALIAS>``) or a
    V1 bot name, so compare on the bot, not the exact string.
    """
    if ref in inventory_arns:
        return True
    if ":bot-alias/" in ref:
        prefix, rest = ref.split(":bot-alias/", 1)
        bot_id = rest.split("/", 1)[0]
        return f"{prefix}:bot/{bot_id}" in inventory_arns
    if ref.startswith("lex-v1:"):
        _, region, name, _alias = (ref.split(":", 3) + ["", "", "", ""])[:4]
        return any(
            a.endswith(f":bot:{name}") and (not region or f":lex:{region}:" in a)
            for a in inventory_arns
        )
    return False


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

        # Flow language action for "Get customer input" with a Lex bot is
        # ConnectParticipantWithLexBot: Parameters.LexV2Bot.AliasArn (V2) or
        # Parameters.LexBot {Name, Region, Alias} (V1). The two Invoke* names
        # are kept for older content and are harmless if never present.
        if action_type in _LEX_ACTION_TYPES:
            v2 = params.get("LexV2Bot", {})
            if isinstance(v2, dict) and v2.get("AliasArn"):
                lex_arns.append(v2["AliasArn"])
            v1 = params.get("LexBot", {})
            if isinstance(v1, dict):
                if v1.get("AliasArn"):
                    lex_arns.append(v1["AliasArn"])
                elif v1.get("Name"):
                    lex_arns.append(
                        f"lex-v1:{v1.get('Region', '')}:{v1['Name']}:{v1.get('Alias', '')}"
                    )

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
    skipped_flows = 0
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
                in_inventory=_lex_ref_in_inventory(arn, inventory_arns),
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

            if analysis is None:
                skipped_flows += 1
            else:
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
        skipped_flows=skipped_flows,
        flows_with_lex=flows_with_lex,
        flows_with_lambda=flows_with_lambda,
        flows=analyses,
    )
