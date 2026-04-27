import { useState } from "react";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Flashbar, { type FlashbarProps } from "@cloudscape-design/components/flashbar";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import type { DiscoveredResource, AssociateResultEntry } from "../../types";
import { discoverTarget, associateDiscovered } from "../../api/client";

const RESOURCE_TYPE_LABELS: Record<string, string> = {
  LEX_BOT: "Lex Bot",
  LAMBDA: "Lambda",
  KINESIS_STREAM: "Kinesis Stream",
  KINESIS_FIREHOSE: "Firehose",
  S3_BUCKET: "S3 Bucket",
  KINESIS_VIDEO_STREAM: "KVS (Media)",
};

export default function DiscoverAssociate() {
  const [instanceArn, setInstanceArn] = useState("");
  const [discovering, setDiscovering] = useState(false);
  const [associating, setAssociating] = useState(false);
  const [resources, setResources] = useState<DiscoveredResource[]>([]);
  const [selectedItems, setSelectedItems] = useState<DiscoveredResource[]>([]);
  const [sourceRegion, setSourceRegion] = useState("");
  const [targetRegion, setTargetRegion] = useState("");
  const [results, setResults] = useState<AssociateResultEntry[]>([]);
  const [flash, setFlash] = useState<FlashbarProps.MessageDefinition[]>([]);

  const handleDiscover = async () => {
    if (!instanceArn.trim()) return;
    setDiscovering(true);
    setResources([]);
    setSelectedItems([]);
    setResults([]);
    setFlash([]);
    try {
      const resp = await discoverTarget({ instanceArn: instanceArn.trim() });
      setResources(resp.resources);
      setSourceRegion(resp.sourceRegion);
      setTargetRegion(resp.targetRegion);
      // Auto-select unassociated resources
      setSelectedItems(resp.resources.filter((r) => r.association_status === "not_associated"));
      if (resp.totalDiscovered === 0) {
        setFlash([{ type: "info", content: "No matching resources found in the target region.", dismissible: true, id: "no-results" }]);
      } else {
        setFlash([{
          type: "success",
          content: `Found ${resp.totalDiscovered} resources: ${resp.totalNotAssociated} available for association, ${resp.totalAlreadyAssociated} already associated.`,
          dismissible: true,
          id: "discover-ok",
        }]);
      }
    } catch (err: any) {
      setFlash([{ type: "error", content: err.message || "Discovery failed", dismissible: true, id: "discover-err" }]);
    } finally {
      setDiscovering(false);
    }
  };

  const handleAssociate = async () => {
    if (selectedItems.length === 0) return;
    setAssociating(true);
    setResults([]);
    try {
      const resp = await associateDiscovered({
        instanceArn: instanceArn.trim(),
        resources: selectedItems,
      });
      setResults(resp.results);
      setFlash([{
        type: resp.totalErrors > 0 ? "warning" : "success",
        content: `Association complete: ${resp.totalAssociated} associated, ${resp.totalAlreadyAssociated} already associated, ${resp.totalErrors} errors.`,
        dismissible: true,
        id: "assoc-result",
      }]);
      // Refresh discovery to update statuses
      try {
        const refreshed = await discoverTarget({ instanceArn: instanceArn.trim() });
        setResources(refreshed.resources);
        setSelectedItems([]);
      } catch { /* ignore refresh errors */ }
    } catch (err: any) {
      setFlash([{ type: "error", content: err.message || "Association failed", dismissible: true, id: "assoc-err" }]);
    } finally {
      setAssociating(false);
    }
  };

  return (
    <SpaceBetween size="l">
      <Container header={<Header variant="h2">Discover & Associate Target Resources</Header>}>
        <SpaceBetween size="m">
          <FormField
            label="Connect Instance ARN"
            description="Enter the source Connect instance ARN to discover matching resources in the target region"
          >
            <SpaceBetween direction="horizontal" size="xs">
              <div style={{ flexGrow: 1 }}>
                <Input
                  value={instanceArn}
                  onChange={({ detail }) => setInstanceArn(detail.value)}
                  placeholder="arn:aws:connect:us-east-1:123456789012:instance/abc-123"
                  disabled={discovering}
                />
              </div>
              <Button
                variant="primary"
                onClick={handleDiscover}
                loading={discovering}
                disabled={!instanceArn.trim()}
              >
                Discover
              </Button>
            </SpaceBetween>
          </FormField>
          {sourceRegion && targetRegion && (
            <Box>
              <StatusIndicator type="info">
                Source: {sourceRegion} → Target: {targetRegion}
              </StatusIndicator>
            </Box>
          )}
        </SpaceBetween>
      </Container>

      {flash.length > 0 && <Flashbar items={flash} />}

      {resources.length > 0 && (
        <Table
          header={
            <Header
              variant="h2"
              counter={`(${resources.length})`}
              actions={
                <Button
                  variant="primary"
                  onClick={handleAssociate}
                  loading={associating}
                  disabled={selectedItems.length === 0}
                >
                  Associate Selected ({selectedItems.length})
                </Button>
              }
            >
              Discovered Resources
            </Header>
          }
          selectionType="multi"
          selectedItems={selectedItems}
          onSelectionChange={({ detail }) =>
            setSelectedItems(detail.selectedItems)
          }
          isItemDisabled={(item) => item.association_status === "already_associated"}
          columnDefinitions={[
            {
              id: "name",
              header: "Name",
              cell: (item) => item.name,
              sortingField: "name",
            },
            {
              id: "type",
              header: "Type",
              cell: (item) => RESOURCE_TYPE_LABELS[item.resource_type] || item.resource_type,
              sortingField: "resource_type",
            },
            {
              id: "arn",
              header: "ARN",
              cell: (item) => (
                <Box fontSize="body-s" color="text-body-secondary">
                  {item.arn.length > 80 ? `...${item.arn.slice(-77)}` : item.arn}
                </Box>
              ),
            },
            {
              id: "source_match",
              header: "Source Match",
              cell: (item) => item.source_match || "—",
            },
            {
              id: "status",
              header: "Association Status",
              cell: (item) =>
                item.association_status === "already_associated" ? (
                  <StatusIndicator type="success">Associated</StatusIndicator>
                ) : (
                  <StatusIndicator type="pending">Not associated</StatusIndicator>
                ),
            },
          ]}
          items={resources}
          trackBy="arn"
          empty={<Box textAlign="center">No resources discovered</Box>}
        />
      )}

      {results.length > 0 && (
        <Container header={<Header variant="h2">Association Results</Header>}>
          <Table
            columnDefinitions={[
              { id: "resource", header: "Resource", cell: (item) => item.resource },
              {
                id: "type",
                header: "Type",
                cell: (item) => RESOURCE_TYPE_LABELS[item.resource_type] || item.resource_type,
              },
              {
                id: "status",
                header: "Status",
                cell: (item) => {
                  if (item.status === "associated") return <StatusIndicator type="success">Associated</StatusIndicator>;
                  if (item.status === "already_associated") return <StatusIndicator type="info">Already associated</StatusIndicator>;
                  if (item.status === "error") return <StatusIndicator type="error">{item.error || "Error"}</StatusIndicator>;
                  return <StatusIndicator type="info">{item.status}</StatusIndicator>;
                },
              },
              { id: "message", header: "Message", cell: (item) => item.message || item.error || "—" },
            ]}
            items={results}
            trackBy="resource"
          />
        </Container>
      )}
    </SpaceBetween>
  );
}
