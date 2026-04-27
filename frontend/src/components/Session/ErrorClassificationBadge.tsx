import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import type { ErrorClassification } from "../../types";

const ERROR_TYPE_COLORS: Record<string, "red" | "blue" | "grey" | "green"> = {
  permission: "red",
  "not-found": "blue",
  timeout: "grey",
  conflict: "red",
  quota: "red",
  "service-error": "red",
  unknown: "grey",
};

const ERROR_TYPE_LABELS: Record<string, string> = {
  permission: "Permission",
  "not-found": "Not Found",
  timeout: "Timeout",
  conflict: "Conflict",
  quota: "Quota",
  "service-error": "Service Error",
  unknown: "Unknown",
};

interface Props {
  classification: ErrorClassification;
}

export default function ErrorClassificationBadge({ classification }: Props) {
  const color = ERROR_TYPE_COLORS[classification.error_type] ?? "grey";
  const label = ERROR_TYPE_LABELS[classification.error_type] ?? classification.error_type;

  return (
    <SpaceBetween size="xxs">
      <Box>
        <Badge color={color}>{label}</Badge>
      </Box>
      <Box fontSize="body-s" color="text-body-secondary">
        {classification.raw_message}
      </Box>
      <Box fontSize="body-s">
        <strong>Guidance:</strong> {classification.guidance}
      </Box>
    </SpaceBetween>
  );
}
