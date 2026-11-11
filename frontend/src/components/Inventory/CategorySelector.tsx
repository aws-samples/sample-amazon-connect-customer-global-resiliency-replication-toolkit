import { useMemo } from "react";
import SpaceBetween from "@cloudscape-design/components/space-between";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import Checkbox from "@cloudscape-design/components/checkbox";
import Alert from "@cloudscape-design/components/alert";
import type { Resource } from "../../types";

interface CategorySelectorProps {
  resources: Resource[];
  selectedIds: Set<string>;
  onSelectionChange: (ids: Set<string>) => void;
}

/** Human-friendly labels and display order for resource categories. */
const CATEGORY_CONFIG: Record<string, { label: string; order: number }> = {
  IAM_ROLE: { label: "IAM Roles", order: 0 },
  LAMBDA: { label: "Lambda Functions", order: 1 },
  LEX_BOT: { label: "Lex Bots", order: 2 },
  KINESIS_STREAM: { label: "Kinesis Streams", order: 3 },
  KINESIS_FIREHOSE: { label: "Kinesis Firehose", order: 4 },
  KINESIS_VIDEO_STREAM: { label: "Kinesis Video Streams", order: 5 },
  S3_BUCKET: { label: "S3 Buckets", order: 6 },
  APPROVED_ORIGIN: { label: "Approved Origins", order: 7 },
};

function categoryLabel(type: string): string {
  return CATEGORY_CONFIG[type]?.label ?? type.replace(/_/g, " ");
}

function categoryOrder(type: string): number {
  return CATEGORY_CONFIG[type]?.order ?? 99;
}

export default function CategorySelector({
  resources,
  selectedIds,
  onSelectionChange,
}: CategorySelectorProps) {
  // IAM roles are global (region-less) — the same role is used in both source
  // and target regions, so there is nothing to replicate. They are excluded
  // from selection here (see the alert below).
  const iamRoleCount = useMemo(
    () => resources.filter((r) => r.resource_type === "IAM_ROLE").length,
    [resources],
  );

  const grouped = useMemo(() => {
    const map = new Map<string, Resource[]>();
    for (const r of resources) {
      if (r.resource_type === "IAM_ROLE") continue; // excluded — global
      const list = map.get(r.resource_type) ?? [];
      list.push(r);
      map.set(r.resource_type, list);
    }
    return [...map.entries()].sort(
      ([a], [b]) => categoryOrder(a) - categoryOrder(b),
    );
  }, [resources]);

  // Build reverse dependency map: resourceId -> list of resources that depend on it
  const dependedOnBy = useMemo(() => {
    const map = new Map<string, Resource[]>();
    for (const r of resources) {
      for (const depId of r.dependencies ?? []) {
        const list = map.get(depId) ?? [];
        list.push(r);
        map.set(depId, list);
      }
    }
    return map;
  }, [resources]);

  function toggleAll(_type: string, items: Resource[], checked: boolean) {
    const next = new Set(selectedIds);
    for (const r of items) {
      if (checked) next.add(r.id);
      else next.delete(r.id);
    }
    onSelectionChange(next);
  }

  const totalSelected = selectedIds.size;
  const replicableCount = resources.length - iamRoleCount;

  return (
    <SpaceBetween size="l">
      <Header variant="h2" counter={`(${totalSelected} of ${replicableCount} selected)`}>
        Select Resources to Replicate
      </Header>

      {iamRoleCount > 0 && (
        <Alert type="info" header="IAM roles are not replicated">
          {iamRoleCount} IAM role{iamRoleCount === 1 ? "" : "s"} associated with these
          resources {iamRoleCount === 1 ? "is" : "are"} excluded from replication. IAM is a
          global (region-less) service — the same role ARN works in both the source and target
          regions, so there is nothing to copy. Dependent resources (e.g. Lambda functions)
          will continue to reference their existing role. If your setup requires per-region
          roles, that can be enabled with a backend change.
        </Alert>
      )}

      {grouped.map(([type, items]) => {
        const selectedInCategory = items.filter((r) => selectedIds.has(r.id)).length;
        const allSelected = selectedInCategory === items.length;
        const someSelected = selectedInCategory > 0 && !allSelected;

        return (
          <ExpandableSection
            key={type}
            variant="container"
            defaultExpanded={items.length <= 10}
            headerText={
              `${categoryLabel(type)} (${selectedInCategory}/${items.length})`
            }
          >
            <SpaceBetween size="s">
              <Checkbox
                checked={allSelected}
                indeterminate={someSelected}
                onChange={({ detail }) => toggleAll(type, items, detail.checked)}
              >
                Select all {categoryLabel(type).toLowerCase()}
              </Checkbox>

              <Table
                selectionType="multi"
                selectedItems={items.filter((r) => selectedIds.has(r.id))}
                onSelectionChange={({ detail }) => {
                  const next = new Set(selectedIds);
                  // Remove all items in this category first
                  for (const r of items) next.delete(r.id);
                  // Add back the selected ones
                  for (const r of detail.selectedItems) next.add(r.id);
                  onSelectionChange(next);
                }}
                trackBy="id"
                columnDefinitions={[
                  {
                    id: "name",
                    header: "Name",
                    cell: (r: Resource) => r.name,
                    sortingField: "name",
                  },
                  {
                    id: "arn",
                    header: "ARN",
                    cell: (r: Resource) => (
                      <Box variant="code" fontSize="body-s">
                        {r.arn}
                      </Box>
                    ),
                  },
                  ...(type === "LEX_BOT"
                    ? [
                        {
                          id: "version",
                          header: "Version",
                          cell: (r: Resource) =>
                            r.config_summary?.lex_version ?? "—",
                        },
                      ]
                    : []),
                  ...(type === "LAMBDA"
                    ? [
                        {
                          id: "runtime",
                          header: "Runtime",
                          cell: (r: Resource) =>
                            r.config_summary?.runtime ?? "—",
                        },
                      ]
                    : []),
                  ...(type === "KINESIS_FIREHOSE"
                    ? [
                        {
                          id: "destination",
                          header: "Destination",
                          cell: (r: Resource) => {
                            const destType = r.config_summary?.destination_type ?? "Unknown";
                            const bucket = r.config_summary?.s3_bucket;
                            if (bucket) {
                              // Extract bucket name from ARN
                              const bucketName = bucket.startsWith("arn:") ? bucket.split(":::")[1] ?? bucket : bucket;
                              return `${destType} → ${bucketName}`;
                            }
                            return destType;
                          },
                        },
                        {
                          id: "buffering",
                          header: "Buffering",
                          cell: (r: Resource) =>
                            r.config_summary?.buffering ?? "—",
                        },
                      ]
                    : []),
                  ...(type === "KINESIS_STREAM"
                    ? [
                        {
                          id: "shards",
                          header: "Shards / Retention",
                          cell: (r: Resource) => {
                            const shards = r.config_summary?.shard_count ?? "?";
                            const retention = r.config_summary?.retention_hours ?? "?";
                            return `${shards} shards, ${retention}h retention`;
                          },
                        },
                      ]
                    : []),
                  ...(type === "S3_BUCKET"
                    ? [
                        {
                          id: "storage_types",
                          header: "Usage",
                          cell: (r: Resource) =>
                            r.config_summary?.storage_types ?? "—",
                        },
                      ]
                    : []),
                  ...(type === "APPROVED_ORIGIN"
                    ? [
                        {
                          id: "origin_url",
                          header: "Origin URL",
                          cell: (r: Resource) =>
                            r.config_summary?.origin_url ?? r.name,
                        },
                      ]
                    : []),
                  ...(type === "IAM_ROLE"
                    ? [
                        {
                          id: "policies",
                          header: "Policies",
                          cell: (r: Resource) =>
                            r.config_summary?.attached_policies
                              ? `${r.config_summary.attached_policies} attached`
                              : "—",
                        },
                      ]
                    : []),
                  {
                    id: "dependencies",
                    header: "Integrations",
                    cell: (r: Resource) => {
                      const parts: string[] = [];
                      // Parse lambda_roles for Lex bots: JSON map of resourceId -> role
                      let lambdaRoles: Record<string, string> = {};
                      if (r.resource_type === "LEX_BOT" && r.config_summary?.lambda_roles) {
                        try {
                          lambdaRoles = JSON.parse(r.config_summary.lambda_roles);
                        } catch { /* ignore parse errors */ }
                      }
                      // Forward deps: what this resource depends on
                      if (r.dependencies && r.dependencies.length > 0) {
                        const depNames = r.dependencies
                          .map((depId) => {
                            const dep = resources.find((res) => res.id === depId);
                            const name = dep ? dep.name : depId.slice(0, 8);
                            const role = lambdaRoles[depId];
                            return role ? `${name} (${role})` : name;
                          })
                          .join(", ");
                        parts.push(`Uses: ${depNames}`);
                      }
                      // Reverse deps: what depends on this resource
                      const parents = dependedOnBy.get(r.id);
                      if (parents && parents.length > 0) {
                        const parentNames = parents.map((p) => p.name).join(", ");
                        parts.push(`Used by: ${parentNames}`);
                      }
                      return parts.length > 0 ? parts.join(" | ") : "—";
                    },
                  },
                ]}
                items={items}
                empty={
                  <Box textAlign="center" color="text-body-secondary">
                    No resources
                  </Box>
                }
                variant="embedded"
              />
            </SpaceBetween>
          </ExpandableSection>
        );
      })}

      {resources.length === 0 && (
        <Box textAlign="center" color="text-body-secondary">
          No resources discovered
        </Box>
      )}
    </SpaceBetween>
  );
}
