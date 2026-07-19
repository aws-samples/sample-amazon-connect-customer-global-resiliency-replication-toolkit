import { useState, useEffect, useRef } from "react";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import Alert from "@cloudscape-design/components/alert";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import Select, { type SelectProps } from "@cloudscape-design/components/select";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import FormField from "@cloudscape-design/components/form-field";
import Tabs from "@cloudscape-design/components/tabs";
import { analyzeContactFlows, getFlowAnalysisStatus, listInstances } from "../../api/client";
import type { ContactFlowAnalysisStatusResponse, ContactFlowEntry, FlowReference } from "../../types";

type FilterMode = "all" | "lex" | "lambda";

const FILTER_OPTIONS: SelectProps.Option[] = [
  { value: "all", label: "All Flows" },
  { value: "lex", label: "Lex Only" },
  { value: "lambda", label: "Lambda Only" },
];

// ACGR-supported source regions only — must match the backend's list-instances
// allow-list (derived from the authoritative region-pair map). Osaka
// (ap-northeast-3) is a replica target only and is intentionally excluded.
const CONNECT_REGIONS: SelectProps.Option[] = [
  { value: "us-east-1", label: "US East (N. Virginia)" },
  { value: "us-west-2", label: "US West (Oregon)" },
  { value: "eu-central-1", label: "Europe (Frankfurt)" },
  { value: "eu-west-2", label: "Europe (London)" },
  { value: "ap-northeast-1", label: "Asia Pacific (Tokyo)" },
];

const POLL_INTERVAL_MS = 3000;

export default function ContactFlowAnalysis() {
  const [instanceArn, setInstanceArn] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<ContactFlowAnalysisStatusResponse | null>(null);
  const [filterMode, setFilterMode] = useState<SelectProps.Option>(FILTER_OPTIONS[0]);
  const [prefilledFromWizard, setPrefilledFromWizard] = useState(false);

  // Polling state
  const [progress, setProgress] = useState<{ analyzed: number; total: number } | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Instance picker state
  const [selectedRegion, setSelectedRegion] = useState<SelectProps.Option | null>(null);
  const [instances, setInstances] = useState<Array<{ instanceId: string; instanceAlias: string; instanceArn: string }>>([]);
  const [selectedInstance, setSelectedInstance] = useState<SelectProps.Option | null>(null);
  const [loadingInstances, setLoadingInstances] = useState(false);

  // Seed the Manual-ARN-tab input from the wizard's validated instance ARN, if any.
  useEffect(() => {
    const stored = sessionStorage.getItem("replicator.wizard.instanceArn");
    if (stored) {
      setInstanceArn(stored);
      setPrefilledFromWizard(true);
    }
    // Run once on mount only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const clearPrefilledArn = () => {
    sessionStorage.removeItem("replicator.wizard.instanceArn");
    setInstanceArn("");
    setPrefilledFromWizard(false);
  };

  // Cleanup polling on unmount
  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  const stopPolling = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const startPolling = (jid: string) => {
    stopPolling();
    pollRef.current = setInterval(async () => {
      try {
        const status = await getFlowAnalysisStatus(jid);
        setProgress({ analyzed: status.analyzed_flows, total: status.total_flows });

        if (status.status === "COMPLETED") {
          stopPolling();
          setResult(status);
          setLoading(false);
        } else if (status.status === "FAILED") {
          stopPolling();
          setError(status.error || "Analysis failed");
          setLoading(false);
        }
      } catch (e: unknown) {
        // Don't stop polling on transient errors
      }
    }, POLL_INTERVAL_MS);
  };

  const handleRegionChange = async (option: SelectProps.Option) => {
    setSelectedRegion(option);
    setSelectedInstance(null);
    setInstances([]);
    if (!option.value) return;
    setLoadingInstances(true);
    setError("");
    try {
      const resp = await listInstances(option.value);
      setInstances(resp.instances);
    } catch (e: any) {
      setError(e.message || "Failed to list instances");
    } finally {
      setLoadingInstances(false);
    }
  };

  const submitAnalysis = async (arn: string) => {
    setLoading(true);
    setError("");
    setResult(null);
    setProgress(null);
    try {
      const resp = await analyzeContactFlows({
        instance_arn: arn,
        session_id: sessionId.trim() || undefined,
      });
      startPolling(resp.job_id);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to start analysis");
      setLoading(false);
    }
  };

  const handleAnalyze = () => {
    if (!instanceArn.trim()) {
      setError("Please enter a Connect instance ARN");
      return;
    }
    submitAnalysis(instanceArn.trim());
  };

  const handlePickerAnalyze = () => {
    if (!selectedInstance?.value) return;
    submitAnalysis(selectedInstance.value);
  };

  const instanceOptions: SelectProps.Options = instances.map((i) => ({
    value: i.instanceArn,
    label: i.instanceAlias || i.instanceId,
    description: i.instanceArn,
  }));

  const currentFilter = (filterMode.value || "all") as FilterMode;
  const filteredFlows = result?.flows
    ? result.flows.filter((flow) => {
        if (currentFilter === "lex") return flow.lex_references.length > 0;
        if (currentFilter === "lambda") return flow.lambda_references.length > 0;
        return true;
      })
    : [];

  const progressPercent = progress && progress.total > 0
    ? Math.round((progress.analyzed / progress.total) * 100)
    : 0;

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h2"
            description="Analyze contact flows to identify Lex bot and Lambda function references. Supports instances with 1000+ flows via async processing."
          >
            Contact Flow Analysis
          </Header>
        }
      >
        <SpaceBetween size="m">
          <Tabs
            tabs={[
              {
                id: "browse",
                label: "Browse Instances",
                content: (
                  <SpaceBetween size="m">
                    <ColumnLayout columns={2}>
                      <FormField label="Region" description="Select a region to browse Connect instances">
                        <Select
                          selectedOption={selectedRegion}
                          onChange={({ detail }) => handleRegionChange(detail.selectedOption)}
                          options={CONNECT_REGIONS}
                          placeholder="Select a region"
                        />
                      </FormField>
                      <FormField label="Instance" description="Select a Connect instance">
                        {loadingInstances ? (
                          <Box padding="s">
                            <StatusIndicator type="loading">Loading instances...</StatusIndicator>
                          </Box>
                        ) : (
                          <Select
                            selectedOption={selectedInstance}
                            onChange={({ detail }) => setSelectedInstance(detail.selectedOption)}
                            options={instanceOptions}
                            placeholder={instances.length === 0 ? "Select a region first" : "Select an instance"}
                            disabled={instances.length === 0}
                            empty="No instances found in this region"
                          />
                        )}
                      </FormField>
                    </ColumnLayout>
                    <FormField
                      label="Session ID (optional)"
                      constraintText="Provide a session ID to cross-reference with resource inventory"
                    >
                      <Input
                        value={sessionId}
                        onChange={({ detail }) => setSessionId(detail.value)}
                        placeholder="session-id"
                      />
                    </FormField>
                    <Button variant="primary" onClick={handlePickerAnalyze} loading={loading} disabled={!selectedInstance}>
                      Analyze
                    </Button>
                  </SpaceBetween>
                ),
              },
              {
                id: "manual",
                label: "Manual ARN Entry",
                content: (
                  <SpaceBetween size="m">
                    <ColumnLayout columns={2}>
                      <FormField
                        label="Connect Instance ARN"
                        constraintText="e.g. arn:aws:connect:us-east-1:123456789012:instance/abc-123"
                        info={
                          prefilledFromWizard ? (
                            <Button variant="inline-link" onClick={clearPrefilledArn}>
                              Clear
                            </Button>
                          ) : undefined
                        }
                      >
                        <Input
                          value={instanceArn}
                          onChange={({ detail }) => {
                            setInstanceArn(detail.value);
                            if (prefilledFromWizard) setPrefilledFromWizard(false);
                          }}
                          placeholder="arn:aws:connect:us-east-1:..."
                        />
                      </FormField>
                      <FormField
                        label="Session ID (optional)"
                        constraintText="Provide a session ID to cross-reference with resource inventory"
                      >
                        <Input
                          value={sessionId}
                          onChange={({ detail }) => setSessionId(detail.value)}
                          placeholder="session-id"
                        />
                      </FormField>
                    </ColumnLayout>
                    <Button variant="primary" onClick={handleAnalyze} loading={loading}>
                      Analyze
                    </Button>
                  </SpaceBetween>
                ),
              },
            ]}
          />
        </SpaceBetween>
      </Container>

      {error && <Alert type="error">{error}</Alert>}

      {loading && progress && (
        <Container>
          <SpaceBetween size="s">
            <ProgressBar
              value={progressPercent}
              label="Analyzing contact flows"
              description={
                progress.total > 0
                  ? `Analyzed ${progress.analyzed} of ${progress.total} flows`
                  : "Starting analysis..."
              }
              status="in-progress"
            />
          </SpaceBetween>
        </Container>
      )}

      {loading && !progress && (
        <Container>
          <Box textAlign="center" padding="l">
            <StatusIndicator type="loading">Starting analysis...</StatusIndicator>
          </Box>
        </Container>
      )}

      {result && <SummaryBanner result={result} />}

      {result?.flows && (
        <FlowTable
          flows={filteredFlows}
          filterMode={currentFilter}
          filterOption={filterMode}
          onFilterChange={setFilterMode}
        />
      )}
    </SpaceBetween>
  );
}

function SummaryBanner({ result }: { result: ContactFlowAnalysisStatusResponse }) {
  return (
    <SpaceBetween size="s">
      {result.truncated && (
        <Alert type="warning">
          Showing {result.analyzed_flows} of {result.total_flows} total flows.
          Analysis was truncated due to Lambda timeout. Re-run to analyze remaining flows.
        </Alert>
      )}
      <Container header={<Header variant="h2">Summary</Header>}>
        <ColumnLayout columns={3} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Total Flows</Box>
            <Box fontSize="display-l" fontWeight="bold">
              {result.truncated ? `${result.analyzed_flows} / ${result.total_flows}` : result.total_flows}
            </Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Flows with Lex References</Box>
            <Box fontSize="display-l" fontWeight="bold">
              {result.flows_with_lex}
            </Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Flows with Lambda References</Box>
            <Box fontSize="display-l" fontWeight="bold">
              {result.flows_with_lambda}
            </Box>
          </div>
        </ColumnLayout>
      </Container>
    </SpaceBetween>
  );
}

function ReferenceList({ references, label }: { references: FlowReference[]; label: string }) {
  if (references.length === 0) return <Box color="text-status-inactive">None</Box>;
  return (
    <SpaceBetween size="xxs">
      <Box variant="awsui-key-label">{label}</Box>
      {references.map((ref) => (
        <Box key={ref.arn} variant="samp" fontSize="body-s">
          {ref.in_inventory ? (
            <StatusIndicator type="success">{ref.arn}</StatusIndicator>
          ) : (
            <StatusIndicator type="stopped">{ref.arn}</StatusIndicator>
          )}
        </Box>
      ))}
    </SpaceBetween>
  );
}

function FlowTable({
  flows,
  filterMode,
  filterOption,
  onFilterChange,
}: {
  flows: ContactFlowEntry[];
  filterMode: FilterMode;
  filterOption: SelectProps.Option;
  onFilterChange: (opt: SelectProps.Option) => void;
}) {
  return (
    <Table
      items={flows}
      columnDefinitions={[
        {
          id: "flow_name",
          header: "Flow Name",
          cell: (item) => item.flow_name,
          sortingField: "flow_name",
          width: 280,
        },
        {
          id: "flow_type",
          header: "Flow Type",
          cell: (item) => item.flow_type,
          sortingField: "flow_type",
          width: 180,
        },
        {
          id: "lex_count",
          header: "Lex Bot References",
          cell: (item) => item.lex_references.length,
          sortingComparator: (a, b) => a.lex_references.length - b.lex_references.length,
          width: 160,
        },
        {
          id: "lambda_count",
          header: "Lambda References",
          cell: (item) => item.lambda_references.length,
          sortingComparator: (a, b) => a.lambda_references.length - b.lambda_references.length,
          width: 160,
        },
        {
          id: "details",
          header: "Details",
          cell: (item) => {
            const hasRefs = item.lex_references.length > 0 || item.lambda_references.length > 0;
            if (!hasRefs) return <Box color="text-status-inactive">No references</Box>;
            return (
              <ExpandableSection headerText="Show ARNs" variant="footer">
                <SpaceBetween size="s">
                  <ReferenceList references={item.lex_references} label="Lex Bots" />
                  <ReferenceList references={item.lambda_references} label="Lambda Functions" />
                </SpaceBetween>
              </ExpandableSection>
            );
          },
          width: 360,
        },
      ]}
      variant="embedded"
      stickyHeader
      sortingDisabled={false}
      empty={
        <Box textAlign="center">
          {filterMode !== "all"
            ? "No flows match the current filter"
            : "No contact flows found"}
        </Box>
      }
      header={
        <Header
          counter={`(${flows.length})`}
          actions={
            <Select
              selectedOption={filterOption}
              onChange={({ detail }) => onFilterChange(detail.selectedOption)}
              options={FILTER_OPTIONS}
            />
          }
        >
          Contact Flows
        </Header>
      }
    />
  );
}
