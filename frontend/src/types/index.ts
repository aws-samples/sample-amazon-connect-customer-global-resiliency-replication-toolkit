/** AWS resource types that can be discovered and replicated. */
export enum ResourceType {
  LAMBDA = "LAMBDA",
  LEX_BOT = "LEX_BOT",
  KINESIS_STREAM = "KINESIS_STREAM",
  KINESIS_FIREHOSE = "KINESIS_FIREHOSE",
  KINESIS_VIDEO_STREAM = "KINESIS_VIDEO_STREAM",
  IAM_ROLE = "IAM_ROLE",
  S3_BUCKET = "S3_BUCKET",
}

/** Replication status of a resource. */
export enum ReplicationStatus {
  NOT_REPLICATED = "NOT_REPLICATED",
  IN_PROGRESS = "IN_PROGRESS",
  REPLICATED = "REPLICATED",
  FAILED = "FAILED",
  BLOCKED = "BLOCKED",
  SKIPPED = "SKIPPED",
}

/** A discovered AWS resource in the inventory. */
export interface Resource {
  id: string;
  name: string;
  arn: string;
  resource_type: ResourceType;
  status: ReplicationStatus;
  config_summary: Record<string, string>;
  dependencies: string[];
  replicated_arn?: string | null;
  error?: string | null;
}

/** VPC selection for AWS Lambda functions that need VPC configuration in the target region. */
export interface VpcSelection {
  vpcId: string;
  subnetIds: string[];
  securityGroupIds: string[];
}

// ---------------------------------------------------------------------------
// API Request types
// ---------------------------------------------------------------------------

export interface ValidateInstanceRequest {
  instanceArn: string;
}

export interface DiscoverRequest {
  instanceArn: string;
}

export interface ReplicateRequest {
  sessionId: string;
  resourceIds: string[];
  resourceTags?: Record<string, string>;
  dryRun?: boolean;
  concurrency?: number;
  vpcConfig?: Record<string, VpcSelection>;
}

export interface AddResourceRequest {
  arn: string;
}

// ---------------------------------------------------------------------------
// API Response types
// ---------------------------------------------------------------------------

export interface ValidateInstanceResponse {
  instanceId: string;
  instanceName: string;
  instanceArn: string;
  sourceRegion: string;
  targetRegion: string;
  status: string;
  identityManagementType: string;
  isSaml: boolean;
  hasReplica: boolean;
  replicaArn?: string | null;
  replicaRegion?: string | null;
  replicaStatus?: string | null;
  replicaAlias?: string | null;
}

export interface DiscoverResponse {
  sessionId: string;
  inventory: Resource[];
}

export interface InventoryResponse {
  sessionId: string;
  sourceRegion: string;
  targetRegion: string;
  instanceArn: string;
  inventory: Resource[];
}

export interface ReplicateResponse {
  jobId: string;
  sessionId: string;
  status: string;
  progress: ReplicationProgress;
}

export interface ReplicationProgress {
  total: number;
  completed: number;
  failed: number;
  blocked: number;
}

export interface ReplicateStatusResponse {
  jobId: string;
  status: "IN_PROGRESS" | "COMPLETED" | "FAILED";
  progress: ReplicationProgress;
  resources: Resource[];
}

export interface RetryResponse {
  resourceId: string;
  status: ReplicationStatus;
  replicatedArn?: string | null;
  error?: string | null;
  jobProgress: ReplicationProgress;
}

export interface RetryFailedResponse {
  jobId: string;
  sessionId: string;
  status: string;
  progress: ReplicationProgress;
  retriedResourceIds: string[];
}

export interface AuditRequest {
  resourceTags?: Record<string, string>;
}

export interface AuditResponse {
  sessionId: string;
  inventory: Resource[];
  auditSummary: Record<string, number>;
}

export interface AddResourceResponse {
  resourceId: string;
  resource: Resource;
}

/** Standard error body returned by the backend. */
export interface ApiError {
  detail: string;
}

// ---------------------------------------------------------------------------
// Cleanup types
// ---------------------------------------------------------------------------

export interface CleanupEntry {
  resourceId: string;
  resourceName: string;
  resourceType: string;
  deleted: boolean;
  error?: string | null;
}

export interface CleanupResponse {
  sessionId: string;
  deletedCount: number;
  failedCount: number;
  skippedCount: number;
  entries: CleanupEntry[];
}

// ---------------------------------------------------------------------------
// Diff types
// ---------------------------------------------------------------------------

export interface DiffEntry {
  resourceId: string;
  resourceName: string;
  resourceType: string;
  sourceConfig: Record<string, unknown>;
  targetConfig: Record<string, unknown>;
  existsInTarget: boolean;
  differences: string[];
}

export interface DiffRequest {
  resourceTags?: Record<string, string>;
}

export interface DiffResponse {
  sessionId: string;
  entries: DiffEntry[];
  totalDifferences: number;
}

// ---------------------------------------------------------------------------
// Association types
// ---------------------------------------------------------------------------

export interface AssociateResultEntry {
  resource: string;
  resource_type: string;
  replicated_arn?: string;
  status: "associated" | "already_associated" | "already_enabled" | "enabled" | "error" | "pending";
  message?: string;
  error?: string;
  retryable?: boolean;
}

export interface AssociateResponse {
  sessionId: string;
  results: AssociateResultEntry[];
  totalAssociated: number;
  totalAlreadyAssociated: number;
  totalErrors: number;
  totalEnabled: number;
  totalPending: number;
}


// ---------------------------------------------------------------------------
// Session status types
// ---------------------------------------------------------------------------

export interface ErrorClassification {
  error_type: "permission" | "not-found" | "timeout" | "conflict" | "quota" | "service-error" | "unknown";
  raw_message: string;
  guidance: string;
  iam_action: string | null;
  quota_name: string | null;
}

export interface KmsInfo {
  key_type: "CMK" | "AWS_MANAGED" | "NONE";
  key_alias_or_arn: string;
  action: "REUSED" | "BOOTSTRAPPED" | "SKIPPED";
  message: string;
}

export interface SessionInventoryEntry {
  id: string;
  name: string;
  resource_type: string;
  arn: string;
  replicated_arn: string | null;
  status: string;
  error: string | null;
  error_classification: ErrorClassification | null;
  kms_info: KmsInfo | null;
  association_status: string;
  association_error: string | null;
  association_message: string | null;
  retryable: boolean;
}

export interface SessionReplicationJob {
  jobId: string;
  status: string;
  progress: ReplicationProgress;
  startedAt: string | null;
  completedAt: string | null;
}

export interface SessionStatusResponse {
  sessionId: string;
  instanceArn: string;
  sourceRegion: string;
  targetRegion: string;
  createdAt: string;
  updatedAt: string;
  inventory: SessionInventoryEntry[];
  associationResults: AssociateResultEntry[] | null;
  replicationJobs: SessionReplicationJob[];
}

// ---------------------------------------------------------------------------
// Session list types
// ---------------------------------------------------------------------------

export interface SessionSummary {
  sessionId: string;
  instanceArn: string;
  sourceRegion: string;
  targetRegion: string;
  createdAt: string;
  updatedAt: string;
  resourceCount: number;
}

// ---------------------------------------------------------------------------
// Selective cleanup types
// ---------------------------------------------------------------------------

export interface SelectiveCleanupRequest {
  resourceIds: string[];
}


// ---------------------------------------------------------------------------
// Discovery-based association types
// ---------------------------------------------------------------------------

export interface DiscoverTargetRequest {
  instanceArn: string;
}

export interface DiscoveredResource {
  name: string;
  resource_type: string;
  arn: string;
  source_match: string;
  source_arn?: string;
  association_status: "already_associated" | "not_associated";
  storage_type?: string;
  storage_types?: string[];
  kvs_prefix?: string;
  retention_hours?: number;
}

export interface DiscoverTargetResponse {
  instanceArn: string;
  sourceRegion: string;
  targetRegion: string;
  resources: DiscoveredResource[];
  totalDiscovered: number;
  totalAlreadyAssociated: number;
  totalNotAssociated: number;
}

export interface AssociateDiscoveredRequest {
  instanceArn: string;
  resources: DiscoveredResource[];
}

export interface AssociateDiscoveredResponse {
  results: AssociateResultEntry[];
  totalAssociated: number;
  totalAlreadyAssociated: number;
  totalErrors: number;
}

// ---------------------------------------------------------------------------
// Contact Flow Analysis types
// ---------------------------------------------------------------------------

export interface ContactFlowAnalyzeRequest {
  instance_arn: string;
  session_id?: string;
}

export interface FlowReference {
  arn: string;
  reference_type: "LEX_BOT" | "LAMBDA";
  in_inventory: boolean;
}

export interface ContactFlowEntry {
  flow_name: string;
  flow_type: string;
  flow_arn: string;
  lex_references: FlowReference[];
  lambda_references: FlowReference[];
}

export interface ContactFlowAnalyzeAsyncResponse {
  job_id: string;
  status: string;
}

export interface ContactFlowAnalysisStatusResponse {
  job_id: string;
  status: string;
  total_flows: number;
  analyzed_flows: number;
  flows_with_lex: number;
  flows_with_lambda: number;
  truncated: boolean;
  error: string | null;
  flows: ContactFlowEntry[] | null;
}

/** @deprecated Use ContactFlowAnalysisStatusResponse instead */
export interface ContactFlowAnalyzeResponse {
  total_flows: number;
  analyzed_flows: number;
  truncated: boolean;
  flows_with_lex: number;
  flows_with_lambda: number;
  flows: ContactFlowEntry[];
}


// ---------------------------------------------------------------------------
// Step Functions async replication types
// ---------------------------------------------------------------------------

export interface ReplicateAsyncRequest {
  resourceIds: string[];
  resourceTags?: Record<string, string>;
}

export interface ReplicateAsyncResponse {
  jobId: string;
  sessionId: string;
  status: string;
  sfnExecutionArn: string;
  progress: ReplicationProgress;
}

export interface ExecutionResourceStatus {
  resource_id: string;
  resource_name: string;
  resource_type: string;
  level: number;
  status: string;
  replicated_arn?: string | null;
  error?: string | null;
  error_classification?: ErrorClassification | null;
}

export interface ExecutionStatusResponse {
  execution_arn: string;
  status: "RUNNING" | "SUCCEEDED" | "FAILED" | "TIMED_OUT" | "ABORTED";
  current_level: number | null;
  total_levels: number;
  resources: ExecutionResourceStatus[];
  started_at: string | null;
  completed_at: string | null;
  error: string | null;
  summary: {
    succeeded: number;
    failed: number;
    blocked: number;
    in_progress: number;
    total: number;
  };
}
