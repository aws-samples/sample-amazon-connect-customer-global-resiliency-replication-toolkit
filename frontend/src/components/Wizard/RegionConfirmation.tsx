import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import type { ValidateInstanceResponse } from "../../types";

interface RegionConfirmationProps {
  instanceInfo: ValidateInstanceResponse;
}

function ValueWithLabel({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <Box variant="awsui-key-label">{label}</Box>
      <div>{children}</div>
    </div>
  );
}

export default function RegionConfirmation({ instanceInfo }: RegionConfirmationProps) {
  return (
    <SpaceBetween size="l">
      {/* ALGR prerequisite callout — Lex bot replication depends on Amazon Lex
          Global Resiliency, which must be enabled/allowlisted for the account.
          There is no AWS API to query allowlist status, so this is informational. */}
      <Alert type="info" header="Amazon Lex Global Resiliency (ALGR) is required for Lex bots">
        Lex V2 bots are replicated exclusively via <strong>Amazon Lex Global Resiliency
        (ALGR)</strong> using the <Box variant="code" display="inline">CreateBotReplica</Box> API.
        ALGR must be enabled/allow-listed for this AWS account and the{" "}
        {instanceInfo.sourceRegion} → {instanceInfo.targetRegion} region pair before Lex bots can
        be replicated. If ALGR is not enabled, Lex bot replication will fail — contact your AWS
        account team to enable it. (AWS provides no API to query allow-list status, so this
        prerequisite cannot be auto-detected.) Non-Lex resources are unaffected.
      </Alert>

      <Container header={<Header variant="h2">Instance Details</Header>}>
        <ColumnLayout columns={2} variant="text-grid">
          <ValueWithLabel label="Instance Name">{instanceInfo.instanceName}</ValueWithLabel>
          <ValueWithLabel label="Instance ID">{instanceInfo.instanceId}</ValueWithLabel>
          <ValueWithLabel label="Instance ARN">{instanceInfo.instanceArn}</ValueWithLabel>
          <ValueWithLabel label="Status">{instanceInfo.status}</ValueWithLabel>
          <ValueWithLabel label="Identity Management">
            <SpaceBetween direction="horizontal" size="xs">
              <span>{instanceInfo.identityManagementType}</span>
              {instanceInfo.isSaml && <Badge color="blue">SAML</Badge>}
            </SpaceBetween>
          </ValueWithLabel>
        </ColumnLayout>
      </Container>

      <Container header={<Header variant="h2">Region Pair</Header>}>
        <ColumnLayout columns={2} variant="text-grid">
          <ValueWithLabel label="Source Region">{instanceInfo.sourceRegion}</ValueWithLabel>
          <ValueWithLabel label="Target Region (DR)">{instanceInfo.targetRegion}</ValueWithLabel>
        </ColumnLayout>
      </Container>

      {/* Case 1: Replica exists (regardless of SAML) — success */}
      {instanceInfo.hasReplica && (
        <Alert type="success" header="Replica Instance Detected">
          A replica Connect instance exists in {instanceInfo.replicaRegion}. After resource
          replication, use the "Associate with DR Instance" button to enable attributes and
          associate all replicated resources automatically.
          <Box margin={{ top: "s" }}>
            <ColumnLayout columns={2} variant="text-grid">
              <ValueWithLabel label="Replica Alias">{instanceInfo.replicaAlias || "—"}</ValueWithLabel>
              <ValueWithLabel label="Replica ARN">{instanceInfo.replicaArn ?? "—"}</ValueWithLabel>
              <ValueWithLabel label="Replica Region">{instanceInfo.replicaRegion ?? "—"}</ValueWithLabel>
              <ValueWithLabel label="Replica Instance ID">{instanceInfo.replicaArn?.split("/").pop() ?? "—"}</ValueWithLabel>
              {instanceInfo.replicaStatus && (
                <ValueWithLabel label="Replication Status">{instanceInfo.replicaStatus}</ValueWithLabel>
              )}
            </ColumnLayout>
          </Box>
        </Alert>
      )}

      {/* Case 2: No replica + SAML — can create one */}
      {!instanceInfo.hasReplica && instanceInfo.isSaml && (
        <Alert type="warning" header="No Replica Instance Found">
          No replica Connect instance was found in {instanceInfo.targetRegion}. After resource
          replication, use the Connect ReplicateInstance API to create a replica instance in the
          target region, then use the "Associate with DR Instance" button to associate resources.
        </Alert>
      )}

      {/* Case 3: No replica + Non-SAML — blocker for replica creation */}
      {!instanceInfo.hasReplica && !instanceInfo.isSaml && (
        <Alert type="warning" header="No Replica Instance Found">
          No replica Connect instance was found in {instanceInfo.targetRegion}. This instance
          uses {instanceInfo.identityManagementType} authentication. Creating a new replica
          instance via the ReplicateInstance API requires SAML. Resource replication can still
          proceed, but you will need to migrate to SAML or create a replica manually before
          associating resources.
        </Alert>
      )}
    </SpaceBetween>
  );
}
