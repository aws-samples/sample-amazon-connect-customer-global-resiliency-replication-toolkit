/**
 * API client for the Connect ACGR Resource Replicator backend.
 *
 * All functions throw an Error with the backend detail message on non-2xx responses.
 */

import type {
  AddResourceRequest,
  AddResourceResponse,
  ApiError,
  AuditRequest,
  AuditResponse,
  CleanupResponse,
  DiffRequest,
  DiffResponse,
  DiscoverRequest,
  DiscoverResponse,
  HealthResponse,
  InventoryResponse,
  ReplicateRequest,
  ReplicateResponse,
  ReplicateStatusResponse,
  RetryFailedResponse,
  RetryResponse,
  ValidateInstanceRequest,
  ValidateInstanceResponse,
} from "../types";

const BASE_URL = "/api";

/** Shared fetch wrapper that throws on non-2xx with the backend error detail. */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });

  if (!res.ok) {
    let message = `Request failed: ${res.status}`;
    try {
      const text = await res.text();
      try {
        const body: ApiError = JSON.parse(text);
        if (body.detail) {
          message = body.detail;
        }
      } catch {
        // Response is not JSON (e.g. API Gateway timeout HTML page)
        if (res.status === 502 || res.status === 504) {
          message = `Request timed out (${res.status}). The operation may still be running — please wait and check status.`;
        } else {
          message = `Request failed: ${res.status} ${res.statusText}`;
        }
      }
    } catch {
      // ignore read errors
    }
    throw new Error(message);
  }

  const text = await res.text();
  if (!text) {
    return {} as T;
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error(
      `Invalid response from server: expected JSON but received unexpected content`,
    );
  }
}

// ---------------------------------------------------------------------------
// Instance validation
// ---------------------------------------------------------------------------

/** Validate a Connect instance ARN and resolve the ACGR region pair. */
export function validateInstance(
  data: ValidateInstanceRequest,
): Promise<ValidateInstanceResponse> {
  return request<ValidateInstanceResponse>("/validate-instance", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

// ---------------------------------------------------------------------------
// Discovery
// ---------------------------------------------------------------------------

/** Run full discovery for a Connect instance. */
export function discover(data: DiscoverRequest): Promise<DiscoverResponse> {
  return request<DiscoverResponse>("/discover", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

/** Retrieve the resource inventory for an existing session. */
export function getInventory(sessionId: string): Promise<InventoryResponse> {
  return request<InventoryResponse>(`/inventory/${encodeURIComponent(sessionId)}`);
}

// ---------------------------------------------------------------------------
// Manual resource addition
// ---------------------------------------------------------------------------

/** Add a resource ARN to an existing session's inventory. */
export function addResource(
  sessionId: string,
  data: AddResourceRequest,
): Promise<AddResourceResponse> {
  return request<AddResourceResponse>(
    `/inventory/${encodeURIComponent(sessionId)}/resources`,
    { method: "POST", body: JSON.stringify(data) },
  );
}

// ---------------------------------------------------------------------------
// Replication
// ---------------------------------------------------------------------------

/** Start replication for selected resources. */
export function replicate(data: ReplicateRequest): Promise<ReplicateResponse> {
  return request<ReplicateResponse>("/replicate", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

/** Poll replication progress for a job. */
export function getReplicationStatus(
  jobId: string,
  sessionId?: string,
): Promise<ReplicateStatusResponse> {
  const params = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
  return request<ReplicateStatusResponse>(
    `/replicate/${encodeURIComponent(jobId)}/status${params}`,
  );
}

/** Retry a single failed resource within a replication job. */
export function retryResource(
  jobId: string,
  resourceId: string,
  sessionId?: string,
): Promise<RetryResponse> {
  const params = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
  return request<RetryResponse>(
    `/replicate/${encodeURIComponent(jobId)}/retry/${encodeURIComponent(resourceId)}${params}`,
    { method: "POST" },
  );
}

// ---------------------------------------------------------------------------
// Bulk retry
// ---------------------------------------------------------------------------

/** Retry all FAILED and BLOCKED resources in a session. */
export function retryFailed(sessionId: string): Promise<RetryFailedResponse> {
  return request<RetryFailedResponse>(
    `/retry-failed/${encodeURIComponent(sessionId)}`,
    { method: "POST" },
  );
}

// ---------------------------------------------------------------------------
// Audit
// ---------------------------------------------------------------------------

/** Audit the target region to check which resources already exist. */
export function auditTarget(
  sessionId: string,
  data: AuditRequest,
): Promise<AuditResponse> {
  return request<AuditResponse>(
    `/audit/${encodeURIComponent(sessionId)}`,
    { method: "POST", body: JSON.stringify(data) },
  );
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

/** Health check with permission validation. */
export function healthCheck(): Promise<HealthResponse> {
  return request<HealthResponse>("/health");
}

// ---------------------------------------------------------------------------
// Cleanup
// ---------------------------------------------------------------------------

/** Delete all replicated resources from the target region. */
export function cleanupSession(sessionId: string): Promise<CleanupResponse> {
  return request<CleanupResponse>(
    `/cleanup/${encodeURIComponent(sessionId)}`,
    { method: "POST" },
  );
}

// ---------------------------------------------------------------------------
// Diff
// ---------------------------------------------------------------------------

/** Compare source vs target resource configurations. */
export function diffSession(
  sessionId: string,
  data: DiffRequest,
): Promise<DiffResponse> {
  return request<DiffResponse>(
    `/diff/${encodeURIComponent(sessionId)}`,
    { method: "POST", body: JSON.stringify(data) },
  );
}

// ---------------------------------------------------------------------------
// Association
// ---------------------------------------------------------------------------

/** Trigger async association for replicated resources with the DR Connect instance. */
export function associateResources(
  sessionId: string,
): Promise<{ sessionId: string; status: string }> {
  return request<{ sessionId: string; status: string }>(
    `/associate/${encodeURIComponent(sessionId)}`,
    { method: "POST" },
  );
}


// ---------------------------------------------------------------------------
// Session status
// ---------------------------------------------------------------------------

import type { SessionStatusResponse } from "../types";

/** Get comprehensive session status including replication and association state. */
export function getSessionStatus(sessionId: string): Promise<SessionStatusResponse> {
  return request<SessionStatusResponse>(`/session/${encodeURIComponent(sessionId)}/status`);
}

// ---------------------------------------------------------------------------
// Per-resource association retry
// ---------------------------------------------------------------------------

/** Retry association for a single resource. */
export function retryAssociation(
  sessionId: string,
  resourceId: string,
): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(
    `/associate/${encodeURIComponent(sessionId)}/retry/${encodeURIComponent(resourceId)}`,
    { method: "POST" },
  );
}

// ---------------------------------------------------------------------------
// Recent sessions
// ---------------------------------------------------------------------------

import type { SessionSummary, CleanupResponse as CleanupResponseType } from "../types";

/** List the most recent sessions. */
export function listRecentSessions(limit: number = 4): Promise<SessionSummary[]> {
  return request<SessionSummary[]>(`/sessions/recent?limit=${limit}`);
}

// ---------------------------------------------------------------------------
// Selective cleanup
// ---------------------------------------------------------------------------

/** Delete specific replicated resources (disassociate + delete). */
export function selectiveCleanup(
  sessionId: string,
  resourceIds: string[],
): Promise<CleanupResponseType> {
  return request<CleanupResponseType>(
    `/cleanup/${encodeURIComponent(sessionId)}/selective`,
    { method: "POST", body: JSON.stringify({ resourceIds }) },
  );
}


// ---------------------------------------------------------------------------
// Quota Comparison
// ---------------------------------------------------------------------------

export interface QuotaCompareRequest {
  instance_arn: string;
  target_region?: string;
}

export interface QuotaEntry {
  service: string;
  quota_name: string;
  quota_code: string;
  source_region: string;
  source_value: number | null;
  source_applied: boolean;
  target_region: string;
  target_value: number | null;
  target_applied: boolean;
  adjustable: boolean;
  match: boolean | null;
  discrepancy: string | null;
  source_error: string | null;
  target_error: string | null;
}

export interface AcgrStatus {
  instance_id: string;
  instance_status: string;
  acgr_enabled: boolean;
  target_region?: string;
  tdg?: { name: string; arn: string; status: string };
  error?: string;
}

export interface QuotaCompareResponse {
  acgr_status: AcgrStatus;
  source_region: string;
  target_region: string;
  comparison: QuotaEntry[];
  discrepancies: QuotaEntry[];
  total_quotas_checked: number;
  total_discrepancies: number;
  error?: string;
}

/** Compare service quotas between source and target ACGR regions. */
export function compareQuotas(
  data: QuotaCompareRequest,
): Promise<QuotaCompareResponse> {
  return request<QuotaCompareResponse>("/quota-compare", {
    method: "POST",
    body: JSON.stringify(data),
  });
}


// ---------------------------------------------------------------------------
// Discovery-based association
// ---------------------------------------------------------------------------

import type {
  DiscoverTargetRequest,
  DiscoverTargetResponse,
  AssociateDiscoveredRequest,
  AssociateDiscoveredResponse,
} from "../types";

/** Discover existing resources in the target region that can be associated. */
export function discoverTarget(
  data: DiscoverTargetRequest,
): Promise<DiscoverTargetResponse> {
  return request<DiscoverTargetResponse>("/discover-target", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

/** Associate selected discovered resources with the DR Connect instance. */
export function associateDiscovered(
  data: AssociateDiscoveredRequest,
): Promise<AssociateDiscoveredResponse> {
  return request<AssociateDiscoveredResponse>("/associate-discovered", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

// ---------------------------------------------------------------------------
// Contact Flow Analysis
// ---------------------------------------------------------------------------

import type {
  ContactFlowAnalyzeRequest,
  ContactFlowAnalyzeAsyncResponse,
  ContactFlowAnalysisStatusResponse,
} from "../types";

/** Start async contact flow analysis. Returns a job_id to poll. */
export function analyzeContactFlows(
  data: ContactFlowAnalyzeRequest,
): Promise<ContactFlowAnalyzeAsyncResponse> {
  return request<ContactFlowAnalyzeAsyncResponse>("/contact-flows/analyze", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

/** Poll the status of an async contact flow analysis job. */
export function getFlowAnalysisStatus(
  jobId: string,
): Promise<ContactFlowAnalysisStatusResponse> {
  return request<ContactFlowAnalysisStatusResponse>(
    `/contact-flows/analyze/${encodeURIComponent(jobId)}/status`,
  );
}


// ---------------------------------------------------------------------------
// Step Functions async replication
// ---------------------------------------------------------------------------

import type {
  ReplicateAsyncRequest,
  ReplicateAsyncResponse,
  ExecutionStatusResponse,
} from "../types";

/** Start async replication via Step Functions for a session. */
export function replicateAsync(
  sessionId: string,
  data: ReplicateAsyncRequest,
): Promise<ReplicateAsyncResponse> {
  return request<ReplicateAsyncResponse>(
    `/sessions/${encodeURIComponent(sessionId)}/replicate-async`,
    { method: "POST", body: JSON.stringify(data) },
  );
}

/** Poll Step Functions execution status for a session. */
export function getExecutionStatus(
  sessionId: string,
): Promise<ExecutionStatusResponse> {
  return request<ExecutionStatusResponse>(
    `/sessions/${encodeURIComponent(sessionId)}/execution-status`,
  );
}


// ---------------------------------------------------------------------------
// Instance listing
// ---------------------------------------------------------------------------

export interface ListInstancesResponse {
  instances: Array<{
    instanceId: string;
    instanceAlias: string;
    instanceArn: string;
  }>;
}

/** List Connect instances in a given region. */
export function listInstances(region: string): Promise<ListInstancesResponse> {
  return request<ListInstancesResponse>(`/list-instances?region=${encodeURIComponent(region)}`);
}
