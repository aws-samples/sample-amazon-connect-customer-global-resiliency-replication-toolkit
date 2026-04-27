import { useEffect, useRef, useState } from "react";
import SpaceBetween from "@cloudscape-design/components/space-between";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import Header from "@cloudscape-design/components/header";
import Table from "@cloudscape-design/components/table";
import Button from "@cloudscape-design/components/button";
import Box from "@cloudscape-design/components/box";
import Alert from "@cloudscape-design/components/alert";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import StatusBadge from "./StatusBadge";
import ErrorDetail from "./ErrorDetail";
import { getReplicationStatus, retryResource } from "../../api/client";
import { ReplicationStatus, type ReplicateStatusResponse, type Resource } from "../../types";

interface ProgressTrackerProps {
  jobId: string;
  sessionId?: string;
  onComplete?: (resources: Resource[]) => void;
}

const CATEGORY_LABELS: Record<string, string> = {
  IAM_ROLE: "IAM Roles",
  LAMBDA: "Lambda Functions",
  LEX_BOT: "Lex Bots",
  KINESIS_STREAM: "Kinesis Streams",
  KINESIS_FIREHOSE: "Kinesis Firehose",
  KINESIS_VIDEO_STREAM: "Kinesis Video Streams",
  S3_BUCKET: "S3 Buckets",
};

const CATEGORY_ORDER: Record<string, number> = {
  IAM_ROLE: 0,
  LAMBDA: 1,
  LEX_BOT: 2,
  KINESIS_STREAM: 3,
  KINESIS_FIREHOSE: 4,
  KINESIS_VIDEO_STREAM: 5,
  S3_BUCKET: 6,
};

function groupByCategory(resources: Resource[]) {
  const map = new Map<string, Resource[]>();
  for (const r of resources) {
    const list = map.get(r.resource_type) ?? [];
    list.push(r);
    map.set(r.resource_type, list);
  }
  return [...map.entries()].sort(
    ([a], [b]) => (CATEGORY_ORDER[a] ?? 99) - (CATEGORY_ORDER[b] ?? 99),
  );
}

function categoryProgress(items: Resource[]) {
  const total = items.length;
  const completed = items.filter((r) => r.status === ReplicationStatus.REPLICATED).length;
  const failed = items.filter((r) => r.status === ReplicationStatus.FAILED).length;
  const blocked = items.filter((r) => r.status === ReplicationStatus.BLOCKED).length;
  const done = completed + failed + blocked;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  return { total, completed, failed, blocked, done, pct };
}

export default function ProgressTracker({ jobId, sessionId, onComplete }: ProgressTrackerProps) {
  const [data, setData] = useState<ReplicateStatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState<Set<string>>(new Set());
  const completeCalled = useRef(false);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;

    async function poll() {
      try {
        const status = await getReplicationStatus(jobId, sessionId);
        if (!active) return;
        setData(status);
        setError(null);

        if (status.status === "IN_PROGRESS") {
          timer = setTimeout(poll, 2000);
        } else if (!completeCalled.current) {
          completeCalled.current = true;
          onComplete?.(status.resources);
        }
      } catch (err) {
        if (!active) return;
        setError(err instanceof Error ? err.message : "Failed to fetch status");
        timer = setTimeout(poll, 5000);
      }
    }

    poll();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [jobId, sessionId, onComplete]);

  async function handleRetry(resourceId: string) {
    setRetrying((prev) => new Set(prev).add(resourceId));
    try {
      await retryResource(jobId, resourceId, sessionId);
      const status = await getReplicationStatus(jobId, sessionId);
      setData(status);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Retry failed");
    } finally {
      setRetrying((prev) => {
        const next = new Set(prev);
        next.delete(resourceId);
        return next;
      });
    }
  }

  if (error && !data) {
    return <Alert type="error" header="Error">{error}</Alert>;
  }

  if (!data) {
    return <Box textAlign="center">Loading replication status…</Box>;
  }

  const { progress, resources } = data;
  const overallDone = progress.completed + progress.failed + progress.blocked;
  const overallPct = progress.total > 0 ? Math.round((overallDone / progress.total) * 100) : 0;
  const failedResources = resources.filter(
    (r) => r.status === ReplicationStatus.FAILED || r.status === ReplicationStatus.BLOCKED
  );
  const categories = groupByCategory(resources);

  return (
    <SpaceBetween size="l">
      <Header variant="h2">
        Replication Progress — {overallDone} of {progress.total}
      </Header>

      <ProgressBar
        value={overallPct}
        label="Overall progress"
        description={`${progress.completed} succeeded, ${progress.failed} failed, ${progress.blocked} blocked`}
        status={data.status === "FAILED" ? "error" : data.status === "COMPLETED" ? "success" : "in-progress"}
      />

      {error && <Alert type="warning">{error}</Alert>}

      {categories.map(([type, items]) => {
        const cp = categoryProgress(items);
        const label = CATEGORY_LABELS[type] ?? type.replace(/_/g, " ");
        const hasFailed = cp.failed > 0;
        const allDone = cp.done === cp.total;

        return (
          <ExpandableSection
            key={type}
            variant="container"
            defaultExpanded={hasFailed || !allDone}
            headerText={`${label} (${cp.completed}/${cp.total})`}
          >
            <SpaceBetween size="m">
              <ProgressBar
                value={cp.pct}
                label={label}
                description={`${cp.completed} succeeded, ${cp.failed} failed, ${cp.blocked} blocked`}
                status={
                  cp.failed > 0
                    ? "error"
                    : cp.done === cp.total
                      ? "success"
                      : "in-progress"
                }
              />

              <Table
                columnDefinitions={[
                  { id: "name", header: "Name", cell: (r: Resource) => r.name },
                  {
                    id: "status",
                    header: "Status",
                    cell: (r: Resource) => <StatusBadge status={r.status} />,
                  },
                  {
                    id: "replicated_arn",
                    header: "Replicated ARN",
                    cell: (r: Resource) =>
                      r.replicated_arn ? (
                        <Box variant="code" fontSize="body-s">
                          {r.replicated_arn}
                        </Box>
                      ) : (
                        "—"
                      ),
                  },
                  {
                    id: "actions",
                    header: "Actions",
                    cell: (r: Resource) =>
                      r.status === ReplicationStatus.FAILED || r.status === ReplicationStatus.BLOCKED ? (
                        <SpaceBetween direction="horizontal" size="xs">
                          {r.error && (
                            <Box variant="small" color="text-status-error">
                              {r.error}
                            </Box>
                          )}
                          {r.status === ReplicationStatus.FAILED && (
                            <Button
                              variant="inline-link"
                              loading={retrying.has(r.id)}
                              onClick={() => handleRetry(r.id)}
                            >
                              Retry
                            </Button>
                          )}
                        </SpaceBetween>
                      ) : null,
                  },
                ]}
                items={items}
                variant="embedded"
                empty={<Box textAlign="center">No resources</Box>}
              />
            </SpaceBetween>
          </ExpandableSection>
        );
      })}

      {failedResources.length > 0 && (
        <SpaceBetween size="s">
          <Header variant="h3">Error Details</Header>
          {failedResources.map((r) => (
            <ErrorDetail key={r.id} resourceName={r.name} error={r.error ?? "Unknown error"} />
          ))}
        </SpaceBetween>
      )}
    </SpaceBetween>
  );
}
