import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Box from "@cloudscape-design/components/box";

interface ErrorDetailProps {
  resourceName: string;
  error: string;
}

export default function ErrorDetail({ resourceName, error }: ErrorDetailProps) {
  return (
    <ExpandableSection headerText={`Error: ${resourceName}`}>
      <Box variant="code" color="text-status-error">
        {error}
      </Box>
    </ExpandableSection>
  );
}
