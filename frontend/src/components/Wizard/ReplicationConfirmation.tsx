import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import Box from "@cloudscape-design/components/box";
import Badge from "@cloudscape-design/components/badge";
import DependencyGraph from "../Inventory/DependencyGraph";
import type { Resource } from "../../types";

interface ReplicationConfirmationProps {
  resources: Resource[];
}

export default function ReplicationConfirmation({ resources }: ReplicationConfirmationProps) {
  const typeCounts = resources.reduce<Record<string, number>>((acc, r) => {
    acc[r.resource_type] = (acc[r.resource_type] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <SpaceBetween size="l">
      <Container header={<Header variant="h2">Replication Summary</Header>}>
        <SpaceBetween size="s">
          <Box variant="p">
            <strong>{resources.length}</strong> resource{resources.length !== 1 ? "s" : ""} selected for replication:
          </Box>
          <SpaceBetween direction="horizontal" size="s">
            {Object.entries(typeCounts).map(([type, count]) => (
              <Badge key={type} color="blue">
                {type.replace(/_/g, " ")}: {count}
              </Badge>
            ))}
          </SpaceBetween>
        </SpaceBetween>
      </Container>

      <Table
        columnDefinitions={[
          { id: "name", header: "Name", cell: (r: Resource) => r.name },
          { id: "type", header: "Type", cell: (r: Resource) => r.resource_type.replace(/_/g, " ") },
          { id: "arn", header: "ARN", cell: (r: Resource) => r.arn },
        ]}
        items={resources}
        header={<Header variant="h3">Selected Resources</Header>}
        empty={<Box textAlign="center">No resources selected</Box>}
      />

      <DependencyGraph resources={resources} />
    </SpaceBetween>
  );
}
