import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import type { Resource } from "../../types";

interface DependencyGraphProps {
  resources: Resource[];
  allResources?: Resource[];
}

export default function DependencyGraph({ resources, allResources }: DependencyGraphProps) {
  // Build lookup from ALL resources so we can resolve any dependency
  const lookupSource = allResources && allResources.length > 0 ? allResources : resources;
  const resourceMap = new Map(lookupSource.map((r) => [r.id, r]));
  const withDeps = resources.filter((r) => r.dependencies.length > 0);

  if (withDeps.length === 0) {
    return (
      <Container header={<Header variant="h3">Dependencies</Header>}>
        <Box textAlign="center" color="text-body-secondary">No dependencies between selected resources</Box>
      </Container>
    );
  }

  return (
    <Container header={<Header variant="h3">Dependencies</Header>}>
      <ul style={{ margin: 0, paddingLeft: 20 }}>
        {withDeps.map((r) => (
          <li key={r.id} style={{ marginBottom: 8 }}>
            <strong>{r.name}</strong> ({r.resource_type.replace(/_/g, " ")}) depends on:
            <ul style={{ paddingLeft: 20 }}>
              {r.dependencies.map((depId) => {
                const dep = resourceMap.get(depId);
                return (
                  <li key={depId}>
                    {dep
                      ? `${dep.name} (${dep.resource_type.replace(/_/g, " ")})`
                      : `Unresolved (${depId.slice(0, 6)}…)`}
                  </li>
                );
              })}
            </ul>
          </li>
        ))}
      </ul>
    </Container>
  );
}
