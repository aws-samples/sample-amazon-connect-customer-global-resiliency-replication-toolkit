import { useState, useEffect } from "react";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import FormField from "@cloudscape-design/components/form-field";
import Select, { SelectProps } from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Alert from "@cloudscape-design/components/alert";
import { listInstances, validateInstance } from "../api/client";
import type { ValidateInstanceResponse } from "../types";

// ACGR-supported source regions only. These match the backend's authoritative
// region-pair map; listing an instance in any other region would be rejected by
// validate-instance / discover, so we don't offer them here. Osaka
// (ap-northeast-3) is a replica target only and is intentionally excluded.
const CONNECT_REGIONS = [
  { value: "us-east-1", label: "US East (N. Virginia)" },
  { value: "us-west-2", label: "US West (Oregon)" },
  { value: "eu-central-1", label: "Europe (Frankfurt)" },
  { value: "eu-west-2", label: "Europe (London)" },
  { value: "ap-northeast-1", label: "Asia Pacific (Tokyo)" },
];

interface InstanceOption {
  instanceId: string;
  instanceAlias: string;
  instanceArn: string;
}

interface Props {
  onValidated: (instance: ValidateInstanceResponse) => void;
}

export default function InstancePicker({ onValidated }: Props) {
  const [selectedRegion, setSelectedRegion] = useState<SelectProps.Option | null>(null);
  const [instances, setInstances] = useState<InstanceOption[]>([]);
  const [selectedInstance, setSelectedInstance] = useState<SelectProps.Option | null>(null);
  const [loadingInstances, setLoadingInstances] = useState(false);
  const [validating, setValidating] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!selectedRegion?.value) {
      setInstances([]);
      setSelectedInstance(null);
      return;
    }
    const fetchInstances = async () => {
      setLoadingInstances(true);
      setError("");
      setInstances([]);
      setSelectedInstance(null);
      try {
        const resp = await listInstances(selectedRegion.value!);
        setInstances(resp.instances);
      } catch (e: any) {
        setError(e.message || "Failed to list instances");
      } finally {
        setLoadingInstances(false);
      }
    };
    fetchInstances();
  }, [selectedRegion]);

  const handleSelectInstance = async () => {
    if (!selectedInstance?.value) return;
    const instance = instances.find((i) => i.instanceArn === selectedInstance.value);
    if (!instance) return;
    setValidating(true);
    setError("");
    try {
      const validated = await validateInstance({ instanceArn: instance.instanceArn });
      onValidated(validated);
    } catch (e: any) {
      setError(e.message || "Validation failed");
    } finally {
      setValidating(false);
    }
  };

  const instanceOptions: SelectProps.Options = instances.map((i) => ({
    value: i.instanceArn,
    label: i.instanceAlias || i.instanceId,
    description: i.instanceArn,
  }));

  return (
    <SpaceBetween size="m">
      <FormField label="Region" description="Select an ACGR-supported region to browse instances">
        <Select
          selectedOption={selectedRegion}
          onChange={({ detail }) => setSelectedRegion(detail.selectedOption)}
          options={CONNECT_REGIONS}
          placeholder="Select a region"
        />
      </FormField>
      <FormField label="Instance" description="Select a Connect instance from the list">
        {loadingInstances ? (
          <Box padding="s">
            <StatusIndicator type="loading">Loading instances...</StatusIndicator>
          </Box>
        ) : (
          <Select
            selectedOption={selectedInstance}
            onChange={({ detail }) => setSelectedInstance(detail.selectedOption)}
            options={instanceOptions}
            placeholder={instances.length === 0 ? "No instances found" : "Select an instance"}
            disabled={instances.length === 0}
            empty="No instances found in this region"
          />
        )}
      </FormField>
      {error && <Alert type="error">{error}</Alert>}
      <Button variant="primary" onClick={handleSelectInstance} loading={validating} disabled={!selectedInstance}>
        Validate & Select
      </Button>
    </SpaceBetween>
  );
}
