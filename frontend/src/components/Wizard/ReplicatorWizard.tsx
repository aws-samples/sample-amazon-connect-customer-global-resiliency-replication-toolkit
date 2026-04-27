import { useState, useCallback, useMemo } from "react";
import Wizard from "@cloudscape-design/components/wizard";
import Alert from "@cloudscape-design/components/alert";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Box from "@cloudscape-design/components/box";

import Modal from "@cloudscape-design/components/modal";
import Table from "@cloudscape-design/components/table";
import Toggle from "@cloudscape-design/components/toggle";
import Tabs from "@cloudscape-design/components/tabs";
import InstanceInput from "./InstanceInput";
import InstancePicker from "../InstancePicker";
import RegionConfirmation from "./RegionConfirmation";
import DiscoveryProgress from "./DiscoveryProgress";
import CategorySelector from "../Inventory/CategorySelector";
import ProgressTracker from "../Status/ProgressTracker";
import StatusBadge from "../Status/StatusBadge";
import { validateNamePrefix } from "../../utils/validation";
import {
  validateInstance,
  discover,
  replicate,
  replicateAsync,
  retryFailed,
  auditTarget,
  getReplicationStatus,
  getInventory,
  cleanupSession,
  diffSession,
  associateResources,
  getSessionStatus,
} from "../../api/client";
import type {
  ValidateInstanceResponse,
  AuditResponse,
  AssociateResponse,
  Resource,
  CleanupResponse,
  DiffResponse,
  DiffEntry,
} from "../../types";
import { ReplicationStatus } from "../../types";
import Button from "@cloudscape-design/components/button";

const RESOURCE_TYPE_INFO: Record<string, { label: string; info: string }> = {
  LAMBDA: {
    label: "Lambda Functions",
    info: "Replicated functions use the same code and configuration. Environment variables referencing the source region have been updated. Functions larger than 50 MB were staged via S3.",
  },
  LEX_BOT: {
    label: "Lex Bots",
    info: "Bots replicated via ALGR (Global Resiliency) retain real-time sync. Bots recreated via legacy path are independent copies and must be updated separately.",
  },
  KINESIS_STREAM: {
    label: "Kinesis Data Streams",
    info: "Streams are created with matching shard count and retention period.",
  },
  KINESIS_FIREHOSE: {
    label: "Kinesis Firehose Delivery Streams",
    info: "Delivery streams are recreated with matching destination configuration. S3 destination bucket ARNs and IAM role ARNs are rewritten to the target region.",
  },
  KINESIS_VIDEO_STREAM: {
    label: "Kinesis Video Streams (KVS)",
    info: "KVS streams are created in the target region and can be associated with the DR Connect instance using the Associate button below.",
  },
  IAM_ROLE: {
    label: "IAM Roles",
    info: "IAM roles are global but replicated role policies have region-specific ARNs rewritten to the target region.",
  },
  S3_BUCKET: {
    label: "S3 Buckets",
    info: "S3 buckets are created in the target region with matching configuration. Bucket data is not copied.",
  },
};

function ResourceResultsSummary({
  resources,
  instanceInfo,
}: {
  resources: Resource[];
  instanceInfo: ValidateInstanceResponse | null;
}) {
  const grouped = useMemo(() => {
    const map: Record<string, Resource[]> = {};
    for (const r of resources) {
      const key = r.resource_type;
      if (!map[key]) map[key] = [];
      map[key].push(r);
    }
    return map;
  }, [resources]);

  return (
    <SpaceBetween size="l">
      {Object.entries(grouped).map(([type, items]) => {
        const meta = RESOURCE_TYPE_INFO[type] ?? { label: type, info: "" };
        const success = items.filter((r) => r.status === "REPLICATED").length;
        const failed = items.filter((r) => r.status === "FAILED").length;
        const skipped = items.filter((r) => r.status === "SKIPPED").length;

        return (
          <ExpandableSection
            key={type}
            headerText={`${meta.label} — ${success} replicated, ${failed} failed, ${skipped} skipped`}
            defaultExpanded={failed > 0 || skipped > 0}
          >
            <SpaceBetween size="s">
              {meta.info && <Alert type="info">{meta.info}</Alert>}
              {items.map((r) => (
                <Box key={r.id} padding={{ vertical: "xs" }}>
                  <SpaceBetween size="xxs">
                    <SpaceBetween direction="horizontal" size="xs">
                      <StatusBadge status={r.status} />
                      <Box variant="code">{r.name}</Box>
                    </SpaceBetween>
                    <Box fontSize="body-s" color="text-body-secondary">
                      Source: {r.arn}
                    </Box>
                    {r.replicated_arn && (
                      <Box fontSize="body-s" color="text-body-secondary">
                        Replicated: {r.replicated_arn}
                      </Box>
                    )}
                    {r.error && (
                      <Box fontSize="body-s" color="text-status-error">
                        Error: {r.error}
                      </Box>
                    )}
                  </SpaceBetween>
                </Box>
              ))}
            </SpaceBetween>
          </ExpandableSection>
        );
      })}

      {/* 4-case SAML/Replica messaging */}
      {instanceInfo?.isSaml && instanceInfo?.hasReplica && (
        <Alert type="success" header="Next Steps — Associate Resources">
          Replication complete. Use the "Associate with DR Instance" button below to
          enable required attributes and associate all replicated resources with your
          replica Connect instance in {instanceInfo.replicaRegion}.
        </Alert>
      )}
      {instanceInfo?.isSaml && !instanceInfo?.hasReplica && (
        <Alert type="warning" header="Next Steps — Create Replica Instance">
          Resources have been replicated to {instanceInfo.targetRegion}. Use the Connect
          ReplicateInstance API to create a replica instance, then use the "Associate
          with DR Instance" button to associate the replicated resources.
        </Alert>
      )}
      {!instanceInfo?.isSaml && instanceInfo?.hasReplica && (
        <Alert type="warning" header="Non-SAML Instance — Manual Association Required">
          Resources have been replicated. This instance uses{" "}
          {instanceInfo.identityManagementType} authentication. ACGR replica management
          requires SAML. You can still try the "Associate with DR Instance" button to
          associate resources with the existing replica in {instanceInfo.replicaRegion}.
        </Alert>
      )}
      {!instanceInfo?.isSaml && !instanceInfo?.hasReplica && (
        <Alert type="error" header="Non-SAML Instance — No Replica Available">
          Resources have been replicated to {instanceInfo?.targetRegion}, but a Connect
          replica cannot be created without SAML authentication. Migrate to SAML and
          create a replica instance before associating resources.
        </Alert>
      )}
    </SpaceBetween>
  );
}

export default function ReplicatorWizard() {
  const [activeStep, setActiveStep] = useState(0);

  // Step 1 state
  const [instanceArn, setInstanceArn] = useState("");
  const [validateError, setValidateError] = useState<string | null>(null);
  const [validating, setValidating] = useState(false);

  // Step 2 state
  const [instanceInfo, setInstanceInfo] = useState<ValidateInstanceResponse | null>(null);

  // Step 3 state
  const [isDiscovering, setIsDiscovering] = useState(false);
  const [discoveryError, setDiscoveryError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);

  // Step 4 state
  const [resources, setResources] = useState<Resource[]>([]);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  // Step 5 state
  const [jobId, setJobId] = useState<string | null>(null);
  const [replicateSessionId, setReplicateSessionId] = useState<string | null>(null);
  const [replicateError, setReplicateError] = useState<string | null>(null);
  const [replicating, setReplicating] = useState(false);
  const [, setFinalResources] = useState<Resource[]>([]);
  const [resourceTags, setResourceTags] = useState<Array<{key: string; value: string}>>([]);
  const [newTagKey, setNewTagKey] = useState("");
  const [newTagValue, setNewTagValue] = useState("");
  const [namePrefix, setNamePrefix] = useState("");
  const prefixValidation = useMemo(() => validateNamePrefix(namePrefix), [namePrefix]);

  // Bulk retry state
  const [retryingAll, setRetryingAll] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);

  // Audit state
  const [auditing, setAuditing] = useState(false);
  const [auditError, setAuditError] = useState<string | null>(null);
  const [auditResult, setAuditResult] = useState<AuditResponse | null>(null);

  // Dry run state
  const [dryRun, setDryRun] = useState(false);

  // Concurrency state — removed parallel replication option

  // Async mode state
  const [asyncMode, setAsyncMode] = useState<"sync" | "async">("sync");

  // Cleanup state
  const [cleaningUp, setCleaningUp] = useState(false);
  const [cleanupError, setCleanupError] = useState<string | null>(null);
  const [cleanupResult, setCleanupResult] = useState<CleanupResponse | null>(null);
  const [showCleanupModal, setShowCleanupModal] = useState(false);

  // Diff state
  const [diffing, setDiffing] = useState(false);
  const [diffError, setDiffError] = useState<string | null>(null);
  const [diffResult, setDiffResult] = useState<DiffResponse | null>(null);

  // Association state
  const [associating, setAssociating] = useState(false);
  const [associateError, setAssociateError] = useState<string | null>(null);
  const [associateResult, setAssociateResult] = useState<AssociateResponse | null>(null);

  // Session resume state
  const [resumeSessionId, setResumeSessionId] = useState("");
  const [resuming, setResuming] = useState(false);
  const [resumeError, setResumeError] = useState<string | null>(null);

  async function handleValidate() {
    setValidating(true);
    setValidateError(null);
    try {
      const info = await validateInstance({ instanceArn });
      setInstanceInfo(info);
      sessionStorage.setItem("replicator.wizard.instanceArn", info.instanceArn);
      setActiveStep(1);
    } catch (err) {
      setValidateError(err instanceof Error ? err.message : "Validation failed");
    } finally {
      setValidating(false);
    }
  }

  async function handleResumeSession() {
    if (!resumeSessionId.trim()) return;
    setResuming(true);
    setResumeError(null);
    try {
      const result = await getInventory(resumeSessionId.trim());
      setSessionId(result.sessionId);
      setReplicateSessionId(result.sessionId);
      setResources(result.inventory);
      setSelectedIds(new Set(result.inventory.map((r) => r.id)));
      // Build a minimal instanceInfo from session data
      setInstanceInfo({
        instanceId: "",
        instanceName: "",
        instanceArn: result.instanceArn,
        sourceRegion: result.sourceRegion,
        targetRegion: result.targetRegion,
        status: "ACTIVE",
        identityManagementType: "",
        isSaml: false,
        hasReplica: false,
      });
      setInstanceArn(result.instanceArn);
      // Check if any resources were replicated — if so, show results step
      const hasReplicated = result.inventory.some(
        (r) => r.status === "REPLICATED" || r.status === "FAILED" || r.status === "BLOCKED",
      );
      if (hasReplicated) {
        setCompletedResources(result.inventory);
        setReplicationDone(true);
        setJobId("resumed");
        setActiveStep(4);
      } else {
        setActiveStep(3);
      }
    } catch (err) {
      setResumeError(err instanceof Error ? err.message : "Failed to load session");
    } finally {
      setResuming(false);
    }
  }

  async function handleDiscover() {
    setIsDiscovering(true);
    setDiscoveryError(null);
    try {
      const result = await discover({ instanceArn });
      setSessionId(result.sessionId);
      setResources(result.inventory);
      setSelectedIds(new Set(result.inventory.map((r) => r.id)));
      setActiveStep(3);
    } catch (err) {
      setDiscoveryError(err instanceof Error ? err.message : "Discovery failed");
    } finally {
      setIsDiscovering(false);
    }
  }

  async function handleAudit() {
    if (!sessionId) return;
    setAuditing(true);
    setAuditError(null);
    try {
      const result = await auditTarget(sessionId, {
        resourceTags: Object.fromEntries(resourceTags.map(t => [t.key, t.value])),
      });
      setAuditResult(result);
      // Update resources with audit results
      setResources(result.inventory);
      // Auto-deselect REPLICATED resources, keep only NOT_REPLICATED selected
      const notReplicated = new Set(
        result.inventory
          .filter((r) => r.status !== "REPLICATED")
          .map((r) => r.id),
      );
      setSelectedIds(notReplicated);
    } catch (err) {
      setAuditError(err instanceof Error ? err.message : "Audit failed");
    } finally {
      setAuditing(false);
    }
  }

  async function handleReplicate() {
      if (!sessionId || selectedIds.size === 0) return;
      if (!prefixValidation.valid) return;
      setReplicateError(null);
      setReplicating(true);
      try {
        const useAsync = selectedIds.size > 5 || asyncMode === "async";
        const tagsObj = resourceTags.length > 0
          ? Object.fromEntries(resourceTags.map(t => [t.key, t.value]))
          : undefined;

        if (useAsync && !dryRun) {
          // Use Step Functions async replication
          const result = await replicateAsync(sessionId, {
            resourceIds: Array.from(selectedIds),
            ...(tagsObj ? { resourceTags: tagsObj } : {}),
          });
          setJobId(result.jobId);
          setReplicateSessionId(result.sessionId);
          setActiveStep(4);
        } else {
          // Use synchronous replication (or dry run)
          const result = await replicate({
            sessionId,
            resourceIds: Array.from(selectedIds),
            ...(tagsObj ? { resourceTags: tagsObj } : {}),
            dryRun,
          });
          setJobId(result.jobId);
          setReplicateSessionId(result.sessionId);
          if (dryRun) {
            // Dry run completes immediately — poll once to get results
            const status = await getReplicationStatus(result.jobId, result.sessionId);
            setCompletedResources(status.resources);
            setReplicationDone(true);
          }
          setActiveStep(4);
        }
      } catch (err) {
        setReplicateError(err instanceof Error ? err.message : "Replication failed");
      } finally {
        setReplicating(false);
      }
    }

  const [replicationDone, setReplicationDone] = useState(false);
  const [completedResources, setCompletedResources] = useState<Resource[]>([]);

  const handleReplicationComplete = useCallback((completed: Resource[]) => {
    setFinalResources(completed);
    setCompletedResources(completed);
    setReplicationDone(true);
  }, []);

  async function handleRetryAllFailed() {
    if (!replicateSessionId) return;
    setRetryingAll(true);
    setRetryError(null);
    setReplicationDone(false);
    try {
      const retryResult = await retryFailed(replicateSessionId);
      const retryJobId = retryResult.jobId;

      // Poll until the retry job completes
      const poll = async (): Promise<Resource[]> => {
        const status = await getReplicationStatus(retryJobId, replicateSessionId);
        if (status.status === "IN_PROGRESS") {
          await new Promise((resolve) => setTimeout(resolve, 2000));
          return poll();
        }
        return status.resources;
      };

      const updatedResources = await poll();
      setCompletedResources(updatedResources);
      setFinalResources(updatedResources);
      setReplicationDone(true);
    } catch (err) {
      setRetryError(err instanceof Error ? err.message : "Retry failed");
      setReplicationDone(true);
    } finally {
      setRetryingAll(false);
    }
  }

  async function handleCleanup() {
    if (!replicateSessionId) return;
    setCleaningUp(true);
    setCleanupError(null);
    setCleanupResult(null);
    setShowCleanupModal(false);
    try {
      const result = await cleanupSession(replicateSessionId);
      setCleanupResult(result);
    } catch (err) {
      setCleanupError(err instanceof Error ? err.message : "Cleanup failed");
    } finally {
      setCleaningUp(false);
    }
  }

  async function handleDiff() {
    if (!sessionId) return;
    setDiffing(true);
    setDiffError(null);
    try {
      const result = await diffSession(sessionId, {
        resourceTags: Object.fromEntries(resourceTags.map(t => [t.key, t.value])),
      });
      setDiffResult(result);
    } catch (err) {
      setDiffError(err instanceof Error ? err.message : "Diff failed");
    } finally {
      setDiffing(false);
    }
  }

  async function handleAssociate() {
    if (!replicateSessionId) return;
    setAssociating(true);
    setAssociateError(null);
    setAssociateResult(null);
    try {
      await associateResources(replicateSessionId);
      // Association is now async — poll session status for results
      const maxPolls = 30;
      const pollInterval = 3000;
      for (let i = 0; i < maxPolls; i++) {
        await new Promise((r) => setTimeout(r, pollInterval));
        try {
          const status = await getSessionStatus(replicateSessionId);
          const hasResults = status.inventory.some(
            (item) => item.association_status && item.association_status !== "not_attempted",
          );
          if (hasResults) {
            // Build a summary from the polled status
            const totalAssociated = status.inventory.filter((i) => i.association_status === "associated").length;
            const totalAlready = status.inventory.filter((i) => i.association_status === "already_associated" || i.association_status === "already_enabled").length;
            const totalErrors = status.inventory.filter((i) => i.association_status === "error").length;
            const totalEnabled = status.inventory.filter((i) => i.association_status === "enabled").length;
            setAssociateResult({
              sessionId: replicateSessionId,
              results: [],
              totalAssociated,
              totalAlreadyAssociated: totalAlready,
              totalErrors,
              totalEnabled,
              totalPending: 0,
            });
            break;
          }
        } catch {
          // Ignore transient poll errors
        }
      }
    } catch (err) {
      setAssociateError(err instanceof Error ? err.message : "Association failed");
    } finally {
      setAssociating(false);
    }
  }

  function handleStartOver() {
    setActiveStep(0);
    setInstanceArn("");
    setValidateError(null);
    setInstanceInfo(null);
    setIsDiscovering(false);
    setDiscoveryError(null);
    setSessionId(null);
    setResources([]);
    setSelectedIds(new Set());
    setJobId(null);
    setReplicateSessionId(null);
    setReplicateError(null);
    setFinalResources([]);
    setResourceTags([]);
    setNewTagKey("");
    setNewTagValue("");
    setNamePrefix("");
    setReplicationDone(false);
    setRetryingAll(false);
    setRetryError(null);
    setAuditing(false);
    setAuditError(null);
    setAuditResult(null);
    setDryRun(false);
    setAsyncMode("sync");
    setCleaningUp(false);
    setCleanupError(null);
    setCleanupResult(null);
    setShowCleanupModal(false);
    setDiffing(false);
    setDiffError(null);
    setDiffResult(null);
    setAssociating(false);
    setAssociateError(null);
    setAssociateResult(null);
    setResumeSessionId("");
    setResuming(false);
    setResumeError(null);
  }

  async function handleSubmit() {
    if (activeStep === 0) {
      await handleValidate();
    } else if (activeStep === 1) {
      if (!instanceInfo?.isSaml) return;
      setActiveStep(2);
      await handleDiscover();
    } else if (activeStep === 3) {
      await handleReplicate();
    } else if (activeStep === 4) {
      handleStartOver();
    }
  }

  const selectedCount = selectedIds.size;

  return (
    <Wizard
      activeStepIndex={activeStep}
      onNavigate={({ detail }) => {
        const idx = detail.requestedStepIndex;
        if (idx > activeStep) {
          if (activeStep === 1 && !instanceInfo?.isSaml) return;
          if (activeStep === 3 && !prefixValidation.valid) return;
          handleSubmit();
        } else {
          setActiveStep(idx);
        }
      }}
      onSubmit={handleSubmit}
      isLoadingNextStep={validating || isDiscovering || replicating || (activeStep === 1 && !instanceInfo?.isSaml)}
      allowSkipTo={false}
      i18nStrings={{
        stepNumberLabel: (stepNumber) => `Step ${stepNumber}`,
        collapsedStepsLabel: (stepNumber, stepsCount) =>
          `Step ${stepNumber} of ${stepsCount}`,
        skipToButtonLabel: (step) => `Skip to ${step.title}`,
        navigationAriaLabel: "Steps",
        cancelButton: "Cancel",
        previousButton: "Previous",
        nextButton:
          activeStep === 3
            ? `Replicate ${selectedCount} Resource${selectedCount !== 1 ? "s" : ""}`
            : activeStep === 4
              ? "Start Over"
              : "Next",
        submitButton:
          activeStep === 3
            ? `Replicate ${selectedCount} Resource${selectedCount !== 1 ? "s" : ""}`
            : activeStep === 4
              ? "Start Over"
              : "Next",
        optional: "optional",
      }}
      steps={[
        {
          title: "Enter Instance ARN",
          description: "Provide your Amazon Connect instance ARN or resume a previous session",
          content: (
            <SpaceBetween size="l">
              <Tabs
                tabs={[
                  {
                    label: "Browse Instances",
                    id: "browse",
                    content: (
                      <Box padding={{ top: "m" }}>
                        <InstancePicker
                          onValidated={(validated) => {
                            setInstanceArn(validated.instanceArn);
                            setInstanceInfo(validated);
                            sessionStorage.setItem("replicator.wizard.instanceArn", validated.instanceArn);
                            setActiveStep(1);
                          }}
                        />
                      </Box>
                    ),
                  },
                  {
                    label: "Manual ARN Entry",
                    id: "manual",
                    content: (
                      <Box padding={{ top: "m" }}>
                        <InstanceInput
                          instanceArn={instanceArn}
                          onChange={setInstanceArn}
                          error={validateError}
                        />
                      </Box>
                    ),
                  },
                ]}
              />
              <ExpandableSection headerText="Resume Previous Session" variant="footer">
                <SpaceBetween size="s">
                  <Alert type="info">
                    Enter a session ID from a previous discovery or replication run to resume
                    where you left off. You can use this to retry failed resources, run cleanup,
                    or associate resources with the DR instance.
                  </Alert>
                  <FormField label="Session ID">
                    <Input
                      value={resumeSessionId}
                      onChange={({ detail }) => setResumeSessionId(detail.value)}
                      placeholder="e.g. 2c0dcbe7-128d-4db6-821b-9b563bd47b71"
                    />
                  </FormField>
                  {resumeError && (
                    <Alert
                      type="error"
                      header="Resume Error"
                      dismissible
                      onDismiss={() => setResumeError(null)}
                    >
                      {resumeError}
                    </Alert>
                  )}
                  <Button
                    variant="primary"
                    loading={resuming}
                    onClick={handleResumeSession}
                    disabled={!resumeSessionId.trim()}
                  >
                    Resume Session
                  </Button>
                  {resumeSessionId.trim() && (
                    <Box fontSize="body-s" color="text-body-secondary">
                      Or{" "}
                      <Button
                        variant="inline-link"
                        onClick={() => {
                          window.location.hash = `#/session/${encodeURIComponent(resumeSessionId.trim())}`;
                        }}
                      >
                        view session status
                      </Button>
                    </Box>
                  )}
                </SpaceBetween>
              </ExpandableSection>
            </SpaceBetween>
          ),
        },
        {
          title: "Confirm Region Pair",
          description: "Review the source and target regions",
          content: instanceInfo ? (
            <SpaceBetween size="l">
              <RegionConfirmation instanceInfo={instanceInfo} />
              {!instanceInfo.isSaml && !instanceInfo.hasReplica && (
                <Alert type="error">
                  This instance is not eligible for ACGR resource replication. It requires
                  SAML-based authentication and a replica instance in the target region.
                  Please configure SAML and create a replica instance before using this tool.
                </Alert>
              )}
              {!instanceInfo.isSaml && instanceInfo.hasReplica && (
                <Alert type="error">
                  This instance uses {instanceInfo.identityManagementType} identity management.
                  Amazon Connect Global Resiliency (ACGR) requires SAML-based authentication.
                  Please configure SAML for this instance before proceeding with cross-region
                  resource replication.
                </Alert>
              )}
              {instanceInfo.isSaml && !instanceInfo.hasReplica && (
                <Alert type="warning">
                  No replica instance was detected in the target region ({instanceInfo.targetRegion}).
                  A replica instance is required for resource association after replication. You may
                  proceed with resource replication, but you will need to create a replica instance
                  before associating resources.
                </Alert>
              )}
            </SpaceBetween>
          ) : (
            <Alert type="info">Please validate an instance first.</Alert>
          ),
        },
        {
          title: "Discover Resources",
          description: "Scanning for associated AWS resources",
          content: (
            <DiscoveryProgress
              isDiscovering={isDiscovering}
              error={discoveryError}
            />
          ),
        },
        {
          title: "Select Resources",
          description: "Choose which resources to replicate by category",
          content: (
            <SpaceBetween size="l">
              {sessionId && (
                <Alert type="info" header="Session ID">
                  <Box variant="code">{sessionId}</Box>
                  <Box fontSize="body-s" color="text-body-secondary" padding={{ top: "xxs" }}>
                    Save this ID to resume this session later.
                  </Box>
                </Alert>
              )}
              <FormField
                label="Resource name prefix"
                description="Optional. Applied to replicated resource names. 1–32 characters: letters, digits, and hyphens."
                errorText={prefixValidation.errorMessage}
                constraintText="Leave empty to reuse source names."
              >
                <Input
                  value={namePrefix}
                  onChange={({ detail }) => setNamePrefix(detail.value)}
                  placeholder="e.g. dr-"
                  invalid={!prefixValidation.valid}
                />
              </FormField>
              <FormField
                label="Resource tags"
                description="Optional tags applied to all replicated resources. S3 buckets always get a '-dr' suffix; all other resources use the same name as source."
              >
                <SpaceBetween size="xs">
                  <SpaceBetween direction="horizontal" size="xs">
                    <Input
                      value={newTagKey}
                      onChange={({ detail }) => setNewTagKey(detail.value)}
                      placeholder="Key"
                    />
                    <Input
                      value={newTagValue}
                      onChange={({ detail }) => setNewTagValue(detail.value)}
                      placeholder="Value"
                    />
                    <Button
                      variant="normal"
                      onClick={() => {
                        if (newTagKey.trim()) {
                          setResourceTags([...resourceTags, { key: newTagKey.trim(), value: newTagValue.trim() }]);
                          setNewTagKey("");
                          setNewTagValue("");
                        }
                      }}
                      disabled={!newTagKey.trim()}
                    >
                      Add tag
                    </Button>
                  </SpaceBetween>
                  {resourceTags.map((tag, idx) => (
                    <SpaceBetween key={idx} direction="horizontal" size="xs">
                      <Box variant="code">{tag.key} = {tag.value}</Box>
                      <Button
                        variant="icon"
                        iconName="close"
                        onClick={() => setResourceTags(resourceTags.filter((_, i) => i !== idx))}
                      />
                    </SpaceBetween>
                  ))}
                </SpaceBetween>
              </FormField>

              {/* Dry Run Toggle */}
              <Toggle
                checked={dryRun}
                onChange={({ detail }) => setDryRun(detail.checked)}
                description="Preview what would be created without making any AWS changes. Simulated target ARNs will be shown."
              >
                Dry run mode
              </Toggle>

              {/* Audit Target Region */}
              <SpaceBetween size="s">
                <Alert type="info">
                  Run an audit to check which resources already exist in the target region.
                  Resources found in DR will be auto-deselected so only missing resources are replicated.
                </Alert>
                <Button
                  variant="normal"
                  loading={auditing}
                  onClick={handleAudit}
                  disabled={!sessionId}
                >
                  Audit Target Region
                </Button>
                {auditError && (
                  <Alert
                    type="error"
                    header="Audit Error"
                    dismissible
                    onDismiss={() => setAuditError(null)}
                  >
                    {auditError}
                  </Alert>
                )}
                {auditResult && (() => {
                  const total = auditResult.inventory.length;
                  const replicated = auditResult.inventory.filter(
                    (r) => r.status === "REPLICATED",
                  ).length;
                  const missing = total - replicated;
                  const errored = auditResult.inventory.filter(
                    (r) => r.error && r.status !== "REPLICATED",
                  ).length;
                  const alertType = replicated === total ? "success" : missing === total ? "warning" : "info";
                  return (
                    <SpaceBetween size="s">
                      <Alert type={alertType} header="Audit Complete">
                        {replicated} of {total} resources already exist in DR, {missing} need
                        replication
                        {errored > 0 && ` (${errored} had audit errors)`}
                      </Alert>
                      <SpaceBetween size="xs">
                        {auditResult.inventory.map((r) => (
                          <Box key={r.id} padding={{ vertical: "xxs" }}>
                            <SpaceBetween direction="horizontal" size="xs">
                              <StatusBadge
                                status={
                                  r.status === "REPLICATED"
                                    ? ReplicationStatus.REPLICATED
                                    : r.error
                                      ? ReplicationStatus.BLOCKED
                                      : ReplicationStatus.NOT_REPLICATED
                                }
                              />
                              <Box variant="code">{r.name}</Box>
                              <Box fontSize="body-s" color="text-body-secondary">
                                ({r.resource_type.replace(/_/g, " ")})
                                {r.resource_type === "KINESIS_FIREHOSE" && r.config_summary?.destination_type
                                  ? ` → ${r.config_summary.destination_type}${r.config_summary.s3_bucket ? ` (${r.config_summary.s3_bucket.split(":::")[1] ?? r.config_summary.s3_bucket})` : ""}`
                                  : ""}
                              </Box>
                            </SpaceBetween>
                          </Box>
                        ))}
                      </SpaceBetween>
                    </SpaceBetween>
                  );
                })()}
              </SpaceBetween>

              {/* Resource Diff View */}
              <SpaceBetween size="s">
                <Alert type="info">
                  Compare source resource configurations with what currently exists in the target region.
                </Alert>
                <Button
                  variant="normal"
                  loading={diffing}
                  onClick={handleDiff}
                  disabled={!sessionId}
                >
                  Compare Source vs Target
                </Button>
                {diffError && (
                  <Alert
                    type="error"
                    header="Diff Error"
                    dismissible
                    onDismiss={() => setDiffError(null)}
                  >
                    {diffError}
                  </Alert>
                )}
                {diffResult && (
                  <ExpandableSection
                    headerText={`Config Comparison — ${diffResult.entries.filter((e) => e.existsInTarget).length} found in target, ${diffResult.totalDifferences} differences`}
                    defaultExpanded={diffResult.totalDifferences > 0}
                  >
                    <Table
                      items={diffResult.entries}
                      columnDefinitions={[
                        {
                          id: "name",
                          header: "Resource",
                          cell: (item: DiffEntry) => item.resourceName,
                          width: 200,
                        },
                        {
                          id: "type",
                          header: "Type",
                          cell: (item: DiffEntry) => item.resourceType.replace(/_/g, " "),
                          width: 140,
                        },
                        {
                          id: "exists",
                          header: "In Target",
                          cell: (item: DiffEntry) => (
                            <StatusBadge
                              status={
                                item.existsInTarget
                                  ? ReplicationStatus.REPLICATED
                                  : ReplicationStatus.NOT_REPLICATED
                              }
                            />
                          ),
                          width: 100,
                        },
                        {
                          id: "sourceInfo",
                          header: "Source Config",
                          cell: (item: DiffEntry) => {
                            const cfg = item.sourceConfig;
                            if (!cfg || Object.keys(cfg).length === 0) return "—";
                            // Show key fields based on type
                            const parts: string[] = [];
                            if (cfg.DestinationType) parts.push(`Dest: ${cfg.DestinationType}`);
                            if (cfg.S3Bucket) {
                              const bucket = String(cfg.S3Bucket);
                              parts.push(`S3: ${bucket.startsWith("arn:") ? bucket.split(":::")[1] ?? bucket : bucket}`);
                            }
                            if (cfg.Runtime) parts.push(`Runtime: ${cfg.Runtime}`);
                            if (cfg.ShardCount) parts.push(`Shards: ${cfg.ShardCount}`);
                            if (cfg.BotId) parts.push(`Bot: ${cfg.BotId}`);
                            if (parts.length > 0) return parts.join(", ");
                            return Object.entries(cfg).slice(0, 3).map(([k, v]) => `${k}: ${v}`).join(", ");
                          },
                        },
                        {
                          id: "diffs",
                          header: "Differences",
                          cell: (item: DiffEntry) =>
                            item.differences.length > 0
                              ? item.differences.join("; ")
                              : item.existsInTarget
                                ? "No differences"
                                : "Not in target",
                        },
                      ]}
                      variant="embedded"
                      empty="No resources to compare"
                    />
                  </ExpandableSection>
                )}
              </SpaceBetween>

              <CategorySelector
                resources={resources}
                selectedIds={selectedIds}
                onSelectionChange={setSelectedIds}
              />
              {replicateError && (
                <Alert type="error" header="Replication Error" dismissible onDismiss={() => setReplicateError(null)}>
                  {replicateError}
                </Alert>
              )}
            </SpaceBetween>
          ),
        },
        {
          title: "Replication Results",
          description: "Monitor replication progress",
          content: jobId ? (
            <SpaceBetween size="l">
              {replicateSessionId && (
                <SpaceBetween direction="horizontal" size="xs">
                  <Box fontSize="body-s" color="text-body-secondary">
                    Session: <Box variant="code" display="inline">{replicateSessionId}</Box>
                  </Box>
                  <Button
                    variant="inline-link"
                    onClick={() => { window.location.hash = `#/session/${encodeURIComponent(replicateSessionId)}`; }}
                  >
                    View Session Dashboard
                  </Button>
                </SpaceBetween>
              )}
              {dryRun && (
                <Alert type="info" header="Dry Run Mode">
                  This was a dry run. No resources were actually created. The ARNs shown
                  are simulated target ARNs to preview what would be created.
                </Alert>
              )}
              {!dryRun && (
                <ProgressTracker
                  jobId={jobId}
                  sessionId={replicateSessionId ?? undefined}
                  onComplete={handleReplicationComplete}
                />
              )}
              {replicationDone && (
                <>
                  <ResourceResultsSummary
                    resources={completedResources}
                    instanceInfo={instanceInfo}
                  />
                  {!dryRun && completedResources.some(
                    (r) => r.status === "FAILED" || r.status === "BLOCKED"
                  ) && (
                    <SpaceBetween size="s">
                      <Alert type="warning">
                        Some resources failed to replicate. You can retry all failed resources
                        at once. Previously successful resources will not be affected.
                      </Alert>
                      {retryError && (
                        <Alert
                          type="error"
                          header="Retry Error"
                          dismissible
                          onDismiss={() => setRetryError(null)}
                        >
                          {retryError}
                        </Alert>
                      )}
                      <Button
                        variant="primary"
                        loading={retryingAll}
                        onClick={handleRetryAllFailed}
                      >
                        Retry All Failed
                      </Button>
                    </SpaceBetween>
                  )}
                  {!dryRun && completedResources.some(
                    (r) => r.status === "REPLICATED"
                  ) && (
                    <SpaceBetween size="s">
                      {/* Association section */}
                      <ExpandableSection headerText="Associate with DR Instance" variant="footer" defaultExpanded>
                        <SpaceBetween size="s">
                          <Alert type="info">
                            Enable required instance attributes (contact flow logs, Contact Lens, early media)
                            and associate replicated Lambda functions, Lex bots, Kinesis streams, Firehose delivery
                            streams, KVS streams, and S3 buckets with the DR Connect instance.
                          </Alert>
                          {associateError && (
                            <Alert
                              type="error"
                              header="Association Error"
                              dismissible
                              onDismiss={() => setAssociateError(null)}
                            >
                              {associateError}
                            </Alert>
                          )}
                          {associateResult && (
                            <SpaceBetween size="s">
                              <Alert
                                type={associateResult.totalErrors > 0 ? "warning" : "success"}
                                header="Association Complete"
                              >
                                {associateResult.totalAssociated} associated,{" "}
                                {associateResult.totalAlreadyAssociated} already associated,{" "}
                                {associateResult.totalEnabled} enabled
                                {associateResult.totalErrors > 0 && `, ${associateResult.totalErrors} errors`}
                              </Alert>
                              {associateResult.results.map((entry, idx) => (
                                <Box key={idx} padding={{ vertical: "xxs" }}>
                                  <SpaceBetween direction="horizontal" size="xs">
                                    <StatusBadge
                                      status={
                                        entry.status === "associated" || entry.status === "enabled"
                                          ? ReplicationStatus.REPLICATED
                                          : entry.status === "already_associated" || entry.status === "already_enabled"
                                            ? ReplicationStatus.SKIPPED
                                            : ReplicationStatus.FAILED
                                      }
                                    />
                                    <Box variant="code">{entry.resource}</Box>
                                    <Box fontSize="body-s" color="text-body-secondary">
                                      {entry.message || entry.status}
                                    </Box>
                                  </SpaceBetween>
                                  {entry.error && (
                                    <Box fontSize="body-s" color="text-status-error">
                                      {entry.error}
                                    </Box>
                                  )}
                                </Box>
                              ))}
                            </SpaceBetween>
                          )}
                          <Button
                            variant="primary"
                            loading={associating}
                            onClick={handleAssociate}
                          >
                            Associate with DR Instance
                          </Button>
                        </SpaceBetween>
                      </ExpandableSection>

                      {/* Cleanup section */}
                      <ExpandableSection headerText="Rollback / Cleanup" variant="footer">
                        <SpaceBetween size="s">
                          <Alert type="warning">
                            This will permanently delete all replicated resources from the
                            target region. This action cannot be undone. Resources are deleted
                            in reverse dependency order.
                          </Alert>
                          {cleanupError && (
                            <Alert
                              type="error"
                              header="Cleanup Error"
                              dismissible
                              onDismiss={() => setCleanupError(null)}
                            >
                              {cleanupError}
                            </Alert>
                          )}
                          {cleanupResult && (
                            <Alert
                              type={cleanupResult.failedCount > 0 ? "warning" : "success"}
                              header="Cleanup Complete"
                            >
                              {cleanupResult.deletedCount} deleted, {cleanupResult.failedCount} failed
                              {cleanupResult.skippedCount > 0 && `, ${cleanupResult.skippedCount} skipped`}
                            </Alert>
                          )}
                          <Button
                            variant="normal"
                            loading={cleaningUp}
                            onClick={() => setShowCleanupModal(true)}
                          >
                            Delete DR Resources
                          </Button>
                        </SpaceBetween>
                      </ExpandableSection>
                    </SpaceBetween>
                  )}
                </>
              )}
              {/* Cleanup confirmation modal */}
              <Modal
                visible={showCleanupModal}
                onDismiss={() => setShowCleanupModal(false)}
                header="Confirm Cleanup"
                footer={
                  <Box float="right">
                    <SpaceBetween direction="horizontal" size="xs">
                      <Button variant="link" onClick={() => setShowCleanupModal(false)}>
                        Cancel
                      </Button>
                      <Button variant="primary" onClick={handleCleanup}>
                        Delete All DR Resources
                      </Button>
                    </SpaceBetween>
                  </Box>
                }
              >
                <SpaceBetween size="s">
                  <Alert type="error">
                    This will permanently delete all replicated resources from the target
                    region ({instanceInfo?.targetRegion}). This cannot be undone.
                  </Alert>
                  <Box>
                    {completedResources.filter((r) => r.status === "REPLICATED").length} resources
                    will be deleted.
                  </Box>
                </SpaceBetween>
              </Modal>
            </SpaceBetween>
          ) : (
            <Alert type="info">Replication has not started yet.</Alert>
          ),
          isOptional: false,
        },
      ]}
    />
  );
}
