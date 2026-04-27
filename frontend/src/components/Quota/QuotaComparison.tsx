import { useState } from "react";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import Alert from "@cloudscape-design/components/alert";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Spinner from "@cloudscape-design/components/spinner";
import Badge from "@cloudscape-design/components/badge";
import Select, { SelectProps } from "@cloudscape-design/components/select";
import Tabs from "@cloudscape-design/components/tabs";
import {
  compareQuotas,
  type QuotaCompareResponse,
  type QuotaEntry,
} from "../../api/client";

const REGION_OPTIONS: SelectProps.Option[] = [
  { value: "", label: "Auto-detect from ACGR" },
  { value: "us-east-1", label: "US East (N. Virginia) — us-east-1" },
  { value: "us-west-2", label: "US West (Oregon) — us-west-2" },
  { value: "eu-west-2", label: "Europe (London) — eu-west-2" },
  { value: "eu-central-1", label: "Europe (Frankfurt) — eu-central-1" },
  { value: "ap-southeast-1", label: "Asia Pacific (Singapore) — ap-southeast-1" },
  { value: "ap-northeast-1", label: "Asia Pacific (Tokyo) — ap-northeast-1" },
  { value: "ap-southeast-2", label: "Asia Pacific (Sydney) — ap-southeast-2" },
  { value: "ap-northeast-2", label: "Asia Pacific (Seoul) — ap-northeast-2" },
  { value: "ca-central-1", label: "Canada (Central) — ca-central-1" },
  { value: "af-south-1", label: "Africa (Cape Town) — af-south-1" },
];

function formatValue(val: number | null): string {
  if (val === null || val === undefined) return "—";
  return val.toLocaleString();
}

export default function QuotaComparison() {
  const [instanceArn, setInstanceArn] = useState("");
  const [targetRegion, setTargetRegion] = useState<SelectProps.Option>(
    REGION_OPTIONS[0]
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<QuotaCompareResponse | null>(null);

  const handleCompare = async () => {
    if (!instanceArn.trim()) {
      setError("Please enter a Connect instance ARN");
      return;
    }
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const resp = await compareQuotas({
        instance_arn: instanceArn.trim(),
        target_region: targetRegion.value || undefined,
      });
      if (resp.error) {
        setError(resp.error);
      }
      setResult(resp);
    } catch (e: any) {
      setError(e.message || "Quota comparison failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h2"
            description="Enter your Connect instance ARN to check ACGR status and compare service quotas across regions"
          >
            Service Quota Comparison
          </Header>
        }
      >
        <SpaceBetween size="m">
          <ColumnLayout columns={2}>
            <FormField label="Connect Instance ARN" constraintText="e.g. arn:aws:connect:us-east-1:123456789012:instance/abc-123">
              <Input
                value={instanceArn}
                onChange={({ detail }) => setInstanceArn(detail.value)}
                placeholder="arn:aws:connect:us-east-1:..."
              />
            </FormField>
            <FormField label="Target Region (optional)" constraintText="Leave as auto-detect to use the ACGR paired region">
              <Select
                selectedOption={targetRegion}
                onChange={({ detail }) => setTargetRegion(detail.selectedOption)}
                options={REGION_OPTIONS}
              />
            </FormField>
          </ColumnLayout>
          <Button variant="primary" onClick={handleCompare} loading={loading}>
            Compare Quotas
          </Button>
        </SpaceBetween>
      </Container>

      {error && <Alert type="error">{error}</Alert>}

      {loading && (
        <Box textAlign="center" padding="xl">
          <Spinner size="large" />
          <Box variant="p" color="text-body-secondary" margin={{ top: "s" }}>
            Querying Service Quotas API for both regions... This may take 15-30 seconds.
          </Box>
        </Box>
      )}

      {result && result.acgr_status && (
        <AcgrStatusPanel acgrStatus={result.acgr_status} />
      )}

      {result && result.comparison && result.comparison.length > 0 && (
        <QuotaResultTabs result={result} />
      )}
    </SpaceBetween>
  );
}


function AcgrStatusPanel({ acgrStatus }: { acgrStatus: QuotaCompareResponse["acgr_status"] }) {
  return (
    <Container
      header={<Header variant="h2">ACGR Status</Header>}
    >
      <ColumnLayout columns={4} variant="text-grid">
        <div>
          <Box variant="awsui-key-label">Instance ID</Box>
          <Box>{acgrStatus.instance_id || "—"}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Instance Status</Box>
          <StatusIndicator type={acgrStatus.instance_status === "ACTIVE" ? "success" : "warning"}>
            {acgrStatus.instance_status || "Unknown"}
          </StatusIndicator>
        </div>
        <div>
          <Box variant="awsui-key-label">ACGR (Global Resiliency)</Box>
          {acgrStatus.acgr_enabled ? (
            <StatusIndicator type="success">Enabled</StatusIndicator>
          ) : (
            <StatusIndicator type="stopped">Not Enabled</StatusIndicator>
          )}
        </div>
        <div>
          <Box variant="awsui-key-label">Target Region</Box>
          <Box>{acgrStatus.target_region || "—"}</Box>
        </div>
      </ColumnLayout>
      {acgrStatus.tdg && (
        <Box margin={{ top: "m" }}>
          <ColumnLayout columns={3} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">TDG Name</Box>
              <Box>{acgrStatus.tdg.name}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">TDG Status</Box>
              <StatusIndicator type={acgrStatus.tdg.status === "ACTIVE" ? "success" : "info"}>
                {acgrStatus.tdg.status}
              </StatusIndicator>
            </div>
            <div>
              <Box variant="awsui-key-label">TDG ARN</Box>
              <Box variant="samp" fontSize="body-s">{acgrStatus.tdg.arn}</Box>
            </div>
          </ColumnLayout>
        </Box>
      )}
      {acgrStatus.error && (
        <Box margin={{ top: "m" }}>
          <Alert type="warning">{acgrStatus.error}</Alert>
        </Box>
      )}
    </Container>
  );
}

function QuotaResultTabs({ result }: { result: QuotaCompareResponse }) {
  const allItems = result.comparison;
  const discrepancies = result.discrepancies;

  // Group by service
  const serviceGroups = new Map<string, QuotaEntry[]>();
  for (const entry of allItems) {
    const existing = serviceGroups.get(entry.service) || [];
    existing.push(entry);
    serviceGroups.set(entry.service, existing);
  }

  return (
    <Tabs
      tabs={[
        {
          id: "all",
          label: `All Quotas (${allItems.length})`,
          content: (
            <QuotaTable
              items={allItems}
              sourceRegion={result.source_region}
              targetRegion={result.target_region}
            />
          ),
        },
        {
          id: "discrepancies",
          label: (
            <span>
              Discrepancies{" "}
              <Badge color={discrepancies.length > 0 ? "red" : "green"}>
                {discrepancies.length}
              </Badge>
            </span>
          ),
          content:
            discrepancies.length > 0 ? (
              <SpaceBetween size="m">
                <Alert type="warning">
                  {discrepancies.length} quota(s) have lower limits in the target region.
                  These should be addressed before DR failover to avoid service disruptions.
                </Alert>
                <QuotaTable
                  items={discrepancies}
                  sourceRegion={result.source_region}
                  targetRegion={result.target_region}
                />
              </SpaceBetween>
            ) : (
              <Alert type="success">
                All quotas match or are higher in the target region. No action needed.
              </Alert>
            ),
        },
        ...Array.from(serviceGroups.entries()).map(([service, items]) => ({
          id: service.replace(/\s+/g, "-").toLowerCase(),
          label: service,
          content: (
            <QuotaTable
              items={items}
              sourceRegion={result.source_region}
              targetRegion={result.target_region}
            />
          ),
        })),
      ]}
    />
  );
}

function QuotaTable({
  items,
  sourceRegion,
  targetRegion,
}: {
  items: QuotaEntry[];
  sourceRegion: string;
  targetRegion: string;
}) {
  return (
    <Table
      items={items}
      columnDefinitions={[
        {
          id: "service",
          header: "Service",
          cell: (item) => item.service,
          sortingField: "service",
          width: 160,
        },
        {
          id: "quota_name",
          header: "Quota",
          cell: (item) => item.quota_name,
          sortingField: "quota_name",
          width: 280,
        },
        {
          id: "source_value",
          header: `Source (${sourceRegion})`,
          cell: (item) =>
            item.source_error ? (
              <StatusIndicator type="warning">{item.source_error}</StatusIndicator>
            ) : (
              <span>
                {formatValue(item.source_value)}
                {item.source_applied && <Badge color="blue">Applied</Badge>}
              </span>
            ),
          width: 160,
        },
        {
          id: "target_value",
          header: `Target (${targetRegion})`,
          cell: (item) =>
            item.target_error ? (
              <StatusIndicator type="warning">{item.target_error}</StatusIndicator>
            ) : (
              <span>
                {formatValue(item.target_value)}
                {item.target_applied && <Badge color="blue">Applied</Badge>}
              </span>
            ),
          width: 160,
        },
        {
          id: "match",
          header: "Status",
          cell: (item) => {
            if (item.match === null) return <Badge color="grey">Unknown</Badge>;
            if (item.match) return <StatusIndicator type="success">Match</StatusIndicator>;
            if (
              item.target_value !== null &&
              item.source_value !== null &&
              item.target_value < item.source_value
            ) {
              return <StatusIndicator type="error">Lower in Target</StatusIndicator>;
            }
            return <StatusIndicator type="info">Higher in Target</StatusIndicator>;
          },
          width: 140,
        },
        {
          id: "discrepancy",
          header: "Discrepancy Explanation",
          cell: (item) => item.discrepancy || "—",
          width: 320,
        },
        {
          id: "adjustable",
          header: "Adjustable",
          cell: (item) =>
            item.adjustable ? (
              <StatusIndicator type="success">Yes</StatusIndicator>
            ) : (
              <StatusIndicator type="stopped">No</StatusIndicator>
            ),
          width: 100,
        },
      ]}
      sortingDisabled={false}
      variant="embedded"
      stickyHeader
      empty={<Box textAlign="center">No quotas to display</Box>}
      header={
        <Header counter={`(${items.length})`}>
          Quota Comparison
        </Header>
      }
    />
  );
}
