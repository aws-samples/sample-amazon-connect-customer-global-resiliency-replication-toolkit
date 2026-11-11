import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Alert from "@cloudscape-design/components/alert";

const DISCOVERY_SERVICES = [
  "Lambda Functions",
  "Lex Bots",
  "Kinesis Data Streams",
  "Kinesis Firehose",
  "Kinesis Video Streams",
  "IAM Roles",
  "S3 Buckets",
];

interface DiscoveryProgressProps {
  isDiscovering: boolean;
  error?: string | null;
}

export default function DiscoveryProgress({ isDiscovering, error }: DiscoveryProgressProps) {
  return (
    <SpaceBetween size="l">
      <Container header={<Header variant="h2">Discovering Resources</Header>}>
        <SpaceBetween size="s">
          {DISCOVERY_SERVICES.map((service) => (
            <StatusIndicator
              key={service}
              type={isDiscovering ? "loading" : "success"}
            >
              {service}
            </StatusIndicator>
          ))}
        </SpaceBetween>
      </Container>

      {error && (
        <Alert type="error" header="Discovery Error">
          {error}
        </Alert>
      )}
    </SpaceBetween>
  );
}
