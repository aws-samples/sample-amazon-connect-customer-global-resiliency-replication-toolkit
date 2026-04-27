import StatusIndicator, {
  type StatusIndicatorProps,
} from "@cloudscape-design/components/status-indicator";
import { ReplicationStatus } from "../../types";

const STATUS_MAP: Record<
  ReplicationStatus,
  { type: StatusIndicatorProps.Type; label: string }
> = {
  [ReplicationStatus.NOT_REPLICATED]: { type: "stopped", label: "Not Replicated" },
  [ReplicationStatus.IN_PROGRESS]: { type: "in-progress", label: "In Progress" },
  [ReplicationStatus.REPLICATED]: { type: "success", label: "Replicated" },
  [ReplicationStatus.FAILED]: { type: "error", label: "Failed" },
  [ReplicationStatus.BLOCKED]: { type: "warning", label: "Blocked" },
  [ReplicationStatus.SKIPPED]: { type: "info", label: "Skipped" },
};

interface StatusBadgeProps {
  status: ReplicationStatus;
}

export default function StatusBadge({ status }: StatusBadgeProps) {
  const mapping = STATUS_MAP[status] ?? { type: "stopped" as const, label: status };
  return <StatusIndicator type={mapping.type}>{mapping.label}</StatusIndicator>;
}
