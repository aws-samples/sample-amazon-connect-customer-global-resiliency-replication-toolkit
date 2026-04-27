import SpaceBetween from "@cloudscape-design/components/space-between";
import Select, { type SelectProps } from "@cloudscape-design/components/select";
import Input from "@cloudscape-design/components/input";
import { ResourceType, ReplicationStatus } from "../../types";

const TYPE_OPTIONS: SelectProps.Option[] = [
  { value: "", label: "All types" },
  ...Object.values(ResourceType).map((t) => ({ value: t, label: t.replace(/_/g, " ") })),
];

const STATUS_OPTIONS: SelectProps.Option[] = [
  { value: "", label: "All statuses" },
  ...Object.values(ReplicationStatus).map((s) => ({ value: s, label: s.replace(/_/g, " ") })),
];

interface ResourceFiltersProps {
  typeFilter: string;
  statusFilter: string;
  searchQuery: string;
  onTypeChange: (value: string) => void;
  onStatusChange: (value: string) => void;
  onSearchChange: (value: string) => void;
}

export default function ResourceFilters({
  typeFilter,
  statusFilter,
  searchQuery,
  onTypeChange,
  onStatusChange,
  onSearchChange,
}: ResourceFiltersProps) {
  return (
    <SpaceBetween direction="horizontal" size="m">
      <Select
        selectedOption={TYPE_OPTIONS.find((o) => o.value === typeFilter) ?? TYPE_OPTIONS[0]}
        options={TYPE_OPTIONS}
        onChange={({ detail }) => onTypeChange(detail.selectedOption.value ?? "")}
        placeholder="Filter by type"
      />
      <Select
        selectedOption={STATUS_OPTIONS.find((o) => o.value === statusFilter) ?? STATUS_OPTIONS[0]}
        options={STATUS_OPTIONS}
        onChange={({ detail }) => onStatusChange(detail.selectedOption.value ?? "")}
        placeholder="Filter by status"
      />
      <Input
        value={searchQuery}
        onChange={({ detail }) => onSearchChange(detail.value)}
        placeholder="Search by name or ARN"
        type="search"
      />
    </SpaceBetween>
  );
}
