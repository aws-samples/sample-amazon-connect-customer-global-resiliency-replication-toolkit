import { useState, useEffect, useCallback } from "react";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import Link from "@cloudscape-design/components/link";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import { listRecentSessions } from "../../api/client";
import type { SessionSummary } from "../../types";

export default function SessionsListPage() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lookupId, setLookupId] = useState("");

  const fetchSessions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listRecentSessions(4);
      setSessions(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load sessions");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSessions();
  }, [fetchSessions]);

  function navigateToSession(sessionId: string) {
    window.location.hash = `#/session/${encodeURIComponent(sessionId)}`;
  }

  return (
    <SpaceBetween size="l">
      {/* Session lookup */}
      <Container header={<Header variant="h2">Session Lookup</Header>}>
        <SpaceBetween size="s">
          <FormField label="Look up a session by ID">
            <SpaceBetween direction="horizontal" size="xs">
              <Input
                value={lookupId}
                onChange={({ detail }) => setLookupId(detail.value)}
                placeholder="e.g. 2c0dcbe7-128d-4db6-821b-9b563bd47b71"
              />
              <Button
                variant="primary"
                onClick={() => navigateToSession(lookupId.trim())}
                disabled={!lookupId.trim()}
              >
                Go to Session
              </Button>
            </SpaceBetween>
          </FormField>
        </SpaceBetween>
      </Container>

      {/* Error */}
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      {/* Loading state (shown above the table while sessions are fetching) */}
      {loading && (
        <Container>
          <Box textAlign="center" padding="l">
            <StatusIndicator type="loading">Loading sessions...</StatusIndicator>
          </Box>
        </Container>
      )}

      {/* Recent sessions table */}
      <Table
        header={
          <Header
            variant="h2"
            counter={`(${sessions.length})`}
            actions={
              <Button iconName="refresh" loading={loading} onClick={fetchSessions}>
                Refresh
              </Button>
            }
          >
            Recent Sessions
          </Header>
        }
        loading={loading}
        loadingText="Loading recent sessions..."
        items={sessions}
        columnDefinitions={[
          {
            id: "sessionId",
            header: "Session ID",
            cell: (item: SessionSummary) => (
              <Link onFollow={() => navigateToSession(item.sessionId)}>
                {item.sessionId.substring(0, 8)}...
              </Link>
            ),
            width: 140,
          },
          {
            id: "instanceArn",
            header: "Instance",
            cell: (item: SessionSummary) => {
              const parts = item.instanceArn.split("/");
              return <Box variant="code">{parts[parts.length - 1]}</Box>;
            },
            width: 200,
          },
          {
            id: "regions",
            header: "Regions",
            cell: (item: SessionSummary) =>
              `${item.sourceRegion} → ${item.targetRegion}`,
            width: 200,
          },
          {
            id: "resources",
            header: "Resources",
            cell: (item: SessionSummary) => item.resourceCount || "—",
            width: 100,
          },
          {
            id: "created",
            header: "Created",
            cell: (item: SessionSummary) =>
              new Date(item.createdAt).toLocaleString(),
            width: 180,
          },
          {
            id: "updated",
            header: "Last Updated",
            cell: (item: SessionSummary) =>
              new Date(item.updatedAt).toLocaleString(),
            width: 180,
          },
          {
            id: "actions",
            header: "Actions",
            cell: (item: SessionSummary) => (
              <Button
                variant="inline-link"
                onClick={() => navigateToSession(item.sessionId)}
              >
                View Details
              </Button>
            ),
            width: 120,
          },
        ]}
        variant="container"
        empty={
          loading ? null : (
            <Box textAlign="center" padding={{ vertical: "xl" }}>
              <SpaceBetween size="s">
                <Box variant="h3">No sessions yet</Box>
                <Box variant="p" color="text-body-secondary">
                  Start a replication session to see it listed here.
                </Box>
                <Button
                  variant="primary"
                  onClick={() => {
                    window.location.hash = "#/";
                  }}
                >
                  Start a new replication
                </Button>
              </SpaceBetween>
            </Box>
          )
        }
        stickyHeader
      />
    </SpaceBetween>
  );
}
