import { useMemo, useState } from "react";
import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import Box from "@cloudscape-design/components/box";
import Pagination from "@cloudscape-design/components/pagination";
import { useCollection } from "@cloudscape-design/collection-hooks";
import StatusBadge from "../Status/StatusBadge";
import ResourceFilters from "./ResourceFilters";
import AddResourceModal from "./AddResourceModal";
import DependencyGraph from "./DependencyGraph";
import type { Resource } from "../../types";

interface ResourceTableProps {
  resources: Resource[];
  selectedIds: Set<string>;
  onSelectionChange: (ids: Set<string>) => void;
  onAddResource?: (arn: string) => Promise<void>;
}

export default function ResourceTable({
  resources,
  selectedIds,
  onSelectionChange,
  onAddResource,
}: ResourceTableProps) {
  const [typeFilter, setTypeFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [modalVisible, setModalVisible] = useState(false);

  const filtered = useMemo(() => {
    let items = resources;
    if (typeFilter) items = items.filter((r) => r.resource_type === typeFilter);
    if (statusFilter) items = items.filter((r) => r.status === statusFilter);
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      items = items.filter(
        (r) => r.name.toLowerCase().includes(q) || r.arn.toLowerCase().includes(q),
      );
    }
    return items;
  }, [resources, typeFilter, statusFilter, searchQuery]);

  const selectedItems = resources.filter((r) => selectedIds.has(r.id));

  const resourceMap = new Map(resources.map((r) => [r.id, r]));

  const { items, collectionProps, paginationProps } = useCollection(filtered, {
    pagination: { pageSize: 25 },
    sorting: {},
  });

  return (
    <SpaceBetween size="l">
      <ResourceFilters
        typeFilter={typeFilter}
        statusFilter={statusFilter}
        searchQuery={searchQuery}
        onTypeChange={setTypeFilter}
        onStatusChange={setStatusFilter}
        onSearchChange={setSearchQuery}
      />

      <Table
        {...collectionProps}
        selectionType="multi"
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) =>
          onSelectionChange(new Set(detail.selectedItems.map((r) => r.id)))
        }
        trackBy="id"
        sortingDisabled={false}
        columnDefinitions={[
          {
            id: "name",
            header: "Name",
            cell: (r: Resource) => r.name,
            sortingField: "name",
          },
          {
            id: "type",
            header: "Type",
            cell: (r: Resource) => {
              const label = r.resource_type.replace(/_/g, " ");
              if (r.resource_type === "LEX_BOT" && r.config_summary?.lex_version) {
                return `LEX BOT (${r.config_summary.lex_version})`;
              }
              return label;
            },
            sortingField: "resource_type",
          },
          {
            id: "arn",
            header: "ARN",
            cell: (r: Resource) => r.arn,
          },
          {
            id: "status",
            header: "Status",
            cell: (r: Resource) => <StatusBadge status={r.status} />,
            sortingField: "status",
          },
          {
            id: "dependencies",
            header: "Dependencies",
            cell: (r: Resource) => {
              if (r.dependencies.length === 0) return "—";
              return r.dependencies
                .map((depId) => {
                  const dep = resourceMap.get(depId);
                  if (dep) {
                    const type = dep.resource_type.replace(/_/g, " ");
                    return `${dep.name} (${type})`;
                  }
                  return `Unresolved (${depId.slice(0, 6)}…)`;
                })
                .join(", ");
            },
          },
        ]}
        items={items}
        pagination={<Pagination {...paginationProps} />}
        header={
          <Header
            variant="h2"
            counter={`(${filtered.length})`}
            actions={
              onAddResource && (
                <Button onClick={() => setModalVisible(true)}>Add Resource</Button>
              )
            }
          >
            Resource Inventory
          </Header>
        }
        empty={
          <Box textAlign="center" color="text-body-secondary">
            No resources found
          </Box>
        }
      />

      {selectedItems.length > 0 && <DependencyGraph resources={selectedItems} allResources={resources} />}

      {onAddResource && (
        <AddResourceModal
          visible={modalVisible}
          onDismiss={() => setModalVisible(false)}
          onAdd={onAddResource}
        />
      )}
    </SpaceBetween>
  );
}
