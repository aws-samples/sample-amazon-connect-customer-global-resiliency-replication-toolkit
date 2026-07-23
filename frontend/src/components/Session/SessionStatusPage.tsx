import { useState, useCallback } from "react";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Checkbox from "@cloudscape-design/components/checkbox";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import {
  getSessionStatus,
  retryAssociation,
  retryFailed,
  retryResource,
  associateResources,
  selectiveCleanup,
  cleanupSession,
} from "../../api/client";
import type {
  SessionStatusResponse,
  SessionInventoryEntry,
  CleanupResponse,
} from "../../types";
import ErrorClassificationBadge from "./ErrorClassificationBadge";
import KmsInfoPanel from "./KmsInfoPanel";

function replicationBadge(status: string) {
  switch (status) {
    case "REPLICATED":
      return <StatusIndicator type="success">Replicated</StatusIndicator>;
    case "FAILED":
      return <StatusIndicator type="error">Failed</StatusIndicator>;
    case "BLOCKED":
      return <StatusIndicator type="warning">Blocked</StatusIndicator>;
    case "SKIPPED":
      return <StatusIndicator type="stopped">Skipped</StatusIndicator>;
    case "IN_PROGRESS":
      return <StatusIndicator type="in-progress">In progress</StatusIndicator>;
    default:
      return <StatusIndicator type="pending">Not replicated</StatusIndicator>;
  }
}

function associationBadge(status: string) {
  switch (status) {
    case "associated":
    case "enabled":
    case "created":
      return <StatusIndicator type="success">{status}</StatusIndicator>;
    case "already_associated":
    case "already_enabled":
      return <StatusIndicator type="info">Already done</StatusIndicator>;
    case "pending":
      return <StatusIndicator type="in-progress">Pending</StatusIndicator>;
    case "error":
      return <StatusIndicator type="error">Error</StatusIndicator>;
    case "skipped":
      return <StatusIndicator type="stopped">Skipped</StatusIndicator>;
    case "manual_setup_required":
      return <StatusIndicator type="warning">Manual setup</StatusIndicator>;
    default:
      return <StatusIndicator type="pending">Not attempted</StatusIndicator>;
  }
}

interface Props {
  initialSessionId?: string;
}

export default function SessionStatusPage({ initialSessionId }: Props) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<SessionStatusResponse | null>(null);
  const [retryingId, setRetryingId] = useState<string | null>(null);
  const [retryingReplication, setRetryingReplication] = useState(false);
  const [associating, setAssociating] = useState(false);
  const [associateError, setAssociateError] = useState<string | null>(null);

  // Cleanup state
  const [selectedForCleanup, setSelectedForCleanup] = useState<Set<string>>(new Set());
  const [cleaningUp, setCleaningUp] = useState(false);
  const [cleanupError, setCleanupError] = useState<string | null>(null);
  const [cleanupResult, setCleanupResult] = useState<CleanupResponse | null>(null);
  const [showCleanupModal, setShowCleanupModal] = useState(false);

  const fetchStatus = useCallback(async (sid?: string, retryFailed?: boolean) => {
    const id = sid ?? initialSessionId;
    if (!id) return;
    setLoading(true);
    setError(null);
    try {
      const result = await getSessionStatus(id);
      setData(result);

      if (retryFailed && result) {
        const retryableItems = result.inventory.filter(
          (item: SessionInventoryEntry) =>
            item.association_status === "error" || item.association_status === "pending"
        );
        for (const item of retryableItems) {
          try {
            await retryAssociation(id, item.id);
          } catch {
            // Individual retry errors are non-fatal
          }
        }
        if (retryableItems.length > 0) {
          const updated = await getSessionStatus(id);
          setData(updated);
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load session");
    } finally {
      setLoading(false);
    }
  }, [initialSessionId]);

  // Auto-load on mount
  useState(() => {
    if (initialSessionId) {
      fetchStatus(initialSessionId);
    }
  });

  async function handleRetry(resourceId: string) {
    if (!data) return;
    setRetryingId(resourceId);
    try {
      await retryAssociation(data.sessionId, resourceId);
      await fetchStatus(data.sessionId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Retry failed");
    } finally {
      setRetryingId(null);
    }
  }

  async function handleRetryFailedReplication() {
    if (!data) return;
    setRetryingReplication(true);
    setError(null);
    try {
      await retryFailed(data.sessionId);
      // Poll for updated status
      const maxPolls = 30;
      const pollInterval = 3000;
      for (let i = 0; i < maxPolls; i++) {
        await new Promise((r) => setTimeout(r, pollInterval));
        try {
          const updated = await getSessionStatus(data.sessionId);
          setData(updated);
          const stillInProgress = updated.inventory.some(
            (item: SessionInventoryEntry) => item.status === "IN_PROGRESS"
          );
          if (!stillInProgress) break;
        } catch {
          // Ignore transient poll errors
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Retry replication failed");
    } finally {
      setRetryingReplication(false);
    }
  }

  async function handleAssociateAll() {
    if (!data) return;
    setAssociating(true);
    setAssociateError(null);
    try {
      await associateResources(data.sessionId);
      const maxPolls = 30;
      const pollInterval = 3000;
      for (let i = 0; i < maxPolls; i++) {
        await new Promise((r) => setTimeout(r, pollInterval));
        try {
          const updated = await getSessionStatus(data.sessionId);
          setData(updated);
          const hasResults = updated.inventory.some(
            (item: SessionInventoryEntry) =>
              item.association_status && item.association_status !== "not_attempted",
          );
          if (hasResults) break;
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

  function toggleCleanupSelection(resourceId: string) {
    setSelectedForCleanup((prev) => {
      const next = new Set(prev);
      if (next.has(resourceId)) {
        next.delete(resourceId);
      } else {
        next.add(resourceId);
      }
      return next;
    });
  }

  function selectAllForCleanup() {
    if (!data) return;
    const replicatedIds = data.inventory
      .filter((item) => item.status === "REPLICATED" && item.replicated_arn)
      .map((item) => item.id);
    setSelectedForCleanup(new Set(replicatedIds));
  }

  function clearCleanupSelection() {
    setSelectedForCleanup(new Set());
  }

  async function handleCleanup() {
    if (!data) return;
    setCleaningUp(true);
    setCleanupError(null);
    setCleanupResult(null);
    setShowCleanupModal(false);
    try {
      let result: CleanupResponse;
      if (selectedForCleanup.size === 0) {
        // Full cleanup
        result = await cleanupSession(data.sessionId);
      } else {
        // Selective cleanup
        result = await selectiveCleanup(data.sessionId, Array.from(selectedForCleanup));
      }
      setCleanupResult(result);
      setSelectedForCleanup(new Set());
      // Refresh to show updated statuses
      await fetchStatus(data.sessionId);
    } catch (err) {
      setCleanupError(err instanceof Error ? err.message : "Cleanup failed");
    } finally {
      setCleaningUp(false);
    }
  }

  const replicatedItems = data?.inventory.filter(
    (item) => item.status === "REPLICATED" && item.replicated_arn
  ) ?? [];

  return (
    <SpaceBetween size="l">
      {/* Error banner */}
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      {/* Loading state */}
      {loading && !data && (
        <Container>
          <StatusIndicator type="loading">Loading session...</StatusIndicator>
        </Container>
      )}

      {/* Session summary */}
      {data && (
        <Container
          header={
            <Header
              variant="h2"
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    iconName="refresh"
                    loading={loading}
                    onClick={() => fetchStatus(data.sessionId, true)}
                  >
                    Refresh
                  </Button>
                  {data.inventory.some(
                    (item: SessionInventoryEntry) =>
                      item.status === "FAILED" || item.status === "BLOCKED"
                  ) && (
                    <Button
                      variant="normal"
                      loading={retryingReplication}
                      onClick={handleRetryFailedReplication}
                    >
                      Retry Failed Replication
                    </Button>
                  )}
                  <Button
                    variant="primary"
                    loading={associating}
                    onClick={handleAssociateAll}
                  >
                    Associate All
                  </Button>
                </SpaceBetween>
              }
            >
              Session: {data.sessionId}
            </Header>
          }
        >
          <SpaceBetween size="s">
            <Box>
              <SpaceBetween direction="horizontal" size="l">
                <Box>
                  <Box variant="awsui-key-label">Instance ARN</Box>
                  <Box variant="code">{data.instanceArn}</Box>
                </Box>
                <Box>
                  <Box variant="awsui-key-label">Source Region</Box>
                  <Box>{data.sourceRegion}</Box>
                </Box>
                <Box>
                  <Box variant="awsui-key-label">Target Region</Box>
                  <Box>{data.targetRegion}</Box>
                </Box>
                <Box>
                  <Box variant="awsui-key-label">Created</Box>
                  <Box>{new Date(data.createdAt).toLocaleString()}</Box>
                </Box>
                <Box>
                  <Box variant="awsui-key-label">Last Updated</Box>
                  <Box>{new Date(data.updatedAt).toLocaleString()}</Box>
                </Box>
              </SpaceBetween>
            </Box>
            {associateError && (
              <Alert type="error" dismissible onDismiss={() => setAssociateError(null)}>
                {associateError}
              </Alert>
            )}
          </SpaceBetween>
        </Container>
      )}

      {/* Resource table */}
      {data && (
        <Table
          header={
            <Header variant="h2" counter={`(${data.inventory.length})`}>
              Resources
            </Header>
          }
          items={data.inventory}
          columnDefinitions={[
            {
              id: "name",
              header: "Name",
              cell: (item: SessionInventoryEntry) => (
                <Box variant="code">{item.name}</Box>
              ),
              width: 220,
            },
            {
              id: "type",
              header: "Type",
              cell: (item: SessionInventoryEntry) =>
                item.resource_type.replace(/_/g, " "),
              width: 140,
            },
            {
              id: "replication",
              header: "Replication",
              cell: (item: SessionInventoryEntry) =>
                replicationBadge(item.status),
              width: 130,
            },
            {
              id: "association",
              header: "Association",
              cell: (item: SessionInventoryEntry) =>
                associationBadge(item.association_status),
              width: 130,
            },
            {
              id: "details",
              header: "Details",
              cell: (item: SessionInventoryEntry) => {
                const hasError = item.error_classification || item.association_error || item.association_message || item.error;
                const hasKms = !!item.kms_info;

                if (!hasError && !hasKms) return "—";

                return (
                  <SpaceBetween size="xs">
                    {item.error_classification && (
                      <ErrorClassificationBadge classification={item.error_classification} />
                    )}
                    {!item.error_classification && item.association_error && (
                      <Box>{item.association_error}</Box>
                    )}
                    {!item.error_classification && !item.association_error && item.association_message && (
                      <Box>{item.association_message}</Box>
                    )}
                    {!item.error_classification && !item.association_error && !item.association_message && item.error && (
                      <Box>{item.error}</Box>
                    )}
                    {item.kms_info && <KmsInfoPanel kmsInfo={item.kms_info} />}
                  </SpaceBetween>
                );
              },
            },
            {
              id: "actions",
              header: "Actions",
              cell: (item: SessionInventoryEntry) => {
                const canRetryAssociation =
                  item.association_status === "error" ||
                  item.association_status === "pending";
                const canRetryReplication =
                  item.status === "FAILED" || item.status === "BLOCKED";

                if (!canRetryAssociation && !canRetryReplication) return null;

                return (
                  <SpaceBetween direction="horizontal" size="xs">
                    {canRetryAssociation && (
                      <Button
                        variant="inline-link"
                        loading={retryingId === item.id}
                        onClick={() => handleRetry(item.id)}
                      >
                        Retry Association
                      </Button>
                    )}
                    {canRetryReplication && (
                      <Button
                        variant="inline-link"
                        loading={retryingId === `repl-${item.id}`}
                        onClick={async () => {
                          if (!data) return;
                          const job = data.replicationJobs?.[data.replicationJobs.length - 1];
                          if (!job) return;
                          setRetryingId(`repl-${item.id}`);
                          try {
                            await retryResource(job.jobId, item.id, data.sessionId);
                            await fetchStatus(data.sessionId);
                          } catch (err) {
                            setError(err instanceof Error ? err.message : "Retry failed");
                          } finally {
                            setRetryingId(null);
                          }
                        }}
                      >
                        Retry Replication
                      </Button>
                    )}
                  </SpaceBetween>
                );
              },
              width: 180,
            },
          ]}
          variant="container"
          empty="No resources in this session"
          stickyHeader
        />
      )}

      {/* Cleanup section */}
      {data && replicatedItems.length > 0 && (
        <Container
          header={
            <Header
              variant="h2"
              description="Select resources to disassociate from the DR instance and delete. Resources are disassociated before deletion."
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button variant="normal" onClick={selectAllForCleanup}>
                    Select All
                  </Button>
                  <Button
                    variant="normal"
                    onClick={clearCleanupSelection}
                    disabled={selectedForCleanup.size === 0}
                  >
                    Clear
                  </Button>
                  <Button
                    variant="primary"
                    loading={cleaningUp}
                    onClick={() => setShowCleanupModal(true)}
                    disabled={selectedForCleanup.size === 0}
                  >
                    Clean Up Selected ({selectedForCleanup.size})
                  </Button>
                </SpaceBetween>
              }
            >
              Cleanup
            </Header>
          }
        >
          <SpaceBetween size="s">
            {cleanupError && (
              <Alert type="error" dismissible onDismiss={() => setCleanupError(null)}>
                {cleanupError}
              </Alert>
            )}
            {cleanupResult && (
              <Alert
                type={cleanupResult.failedCount > 0 ? "warning" : "success"}
                header="Cleanup Complete"
                dismissible
                onDismiss={() => setCleanupResult(null)}
              >
                {cleanupResult.deletedCount} deleted, {cleanupResult.failedCount} failed
                {cleanupResult.skippedCount > 0 && `, ${cleanupResult.skippedCount} skipped`}
              </Alert>
            )}
            <SpaceBetween size="xs">
              {replicatedItems.map((item) => (
                <Box key={item.id} padding={{ vertical: "xxs" }}>
                  <Checkbox
                    checked={selectedForCleanup.has(item.id)}
                    onChange={() => toggleCleanupSelection(item.id)}
                  >
                    <SpaceBetween direction="horizontal" size="xs">
                      <Box variant="code">{item.name}</Box>
                      <Box fontSize="body-s" color="text-body-secondary">
                        ({item.resource_type.replace(/_/g, " ")})
                      </Box>
                      {item.replicated_arn && (
                        <Box fontSize="body-s" color="text-body-secondary">
                          → {item.replicated_arn}
                        </Box>
                      )}
                    </SpaceBetween>
                  </Checkbox>
                </Box>
              ))}
            </SpaceBetween>
          </SpaceBetween>
        </Container>
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
                Disassociate &amp; Delete ({selectedForCleanup.size})
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="s">
          <Alert type="error">
            This will disassociate the selected resources from the DR Connect instance
            and then permanently delete them from {data?.targetRegion}. This cannot be undone.
          </Alert>
          <Box>
            {selectedForCleanup.size} resource{selectedForCleanup.size !== 1 ? "s" : ""} will
            be disassociated and deleted.
          </Box>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}
