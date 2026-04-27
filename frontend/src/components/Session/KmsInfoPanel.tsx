import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import SpaceBetween from "@cloudscape-design/components/space-between";
import type { KmsInfo } from "../../types";

const ACTION_COLORS: Record<string, "blue" | "green" | "grey"> = {
  REUSED: "green",
  BOOTSTRAPPED: "blue",
  SKIPPED: "grey",
};

const KEY_TYPE_LABELS: Record<string, string> = {
  CMK: "Customer Managed Key",
  AWS_MANAGED: "AWS Managed Key",
  NONE: "No Encryption",
};

interface Props {
  kmsInfo: KmsInfo;
}

export default function KmsInfoPanel({ kmsInfo }: Props) {
  const actionColor = ACTION_COLORS[kmsInfo.action] ?? "grey";
  const keyTypeLabel = KEY_TYPE_LABELS[kmsInfo.key_type] ?? kmsInfo.key_type;

  return (
    <ExpandableSection headerText="KMS Encryption Details" variant="footer">
      <SpaceBetween size="xs">
        <Box>
          <SpaceBetween direction="horizontal" size="xs">
            <Box variant="awsui-key-label">Key Type</Box>
            <Box>{keyTypeLabel}</Box>
          </SpaceBetween>
        </Box>
        {kmsInfo.key_alias_or_arn && (
          <Box>
            <SpaceBetween direction="horizontal" size="xs">
              <Box variant="awsui-key-label">Key</Box>
              <Box variant="code">{kmsInfo.key_alias_or_arn}</Box>
            </SpaceBetween>
          </Box>
        )}
        <Box>
          <SpaceBetween direction="horizontal" size="xs">
            <Box variant="awsui-key-label">Action</Box>
            <Badge color={actionColor}>{kmsInfo.action}</Badge>
          </SpaceBetween>
        </Box>
        <Box fontSize="body-s" color="text-body-secondary">
          {kmsInfo.message}
        </Box>
      </SpaceBetween>
    </ExpandableSection>
  );
}
