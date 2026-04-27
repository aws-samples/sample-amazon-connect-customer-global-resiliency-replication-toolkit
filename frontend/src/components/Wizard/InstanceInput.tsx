import { useState } from "react";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";

const ARN_PATTERN = /^arn:aws:connect:[a-z0-9-]+:\d{12}:instance\/[a-f0-9-]+$/;

const SUPPORTED_REGIONS = [
  "us-west-2 ↔ us-east-1",
  "eu-west-2 ↔ eu-central-1",
  "ap-northeast-1 → ap-northeast-3",
];

interface InstanceInputProps {
  instanceArn: string;
  onChange: (arn: string) => void;
  error?: string | null;
}

export default function InstanceInput({ instanceArn, onChange, error }: InstanceInputProps) {
  const [touched, setTouched] = useState(false);

  const isValid = ARN_PATTERN.test(instanceArn);
  const showValidation = touched && instanceArn.length > 0 && !isValid;

  return (
    <SpaceBetween size="l">
      <FormField
        label="Connect Instance ARN"
        description="Enter the ARN of your Amazon Connect instance"
        constraintText="Format: arn:aws:connect:{region}:{account}:instance/{id}"
        errorText={showValidation ? "Invalid Connect instance ARN format" : undefined}
      >
        <Input
          value={instanceArn}
          onChange={({ detail }) => onChange(detail.value)}
          onBlur={() => setTouched(true)}
          placeholder="arn:aws:connect:us-west-2:123456789012:instance/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
        />
      </FormField>

      {error && (
        <Alert type="error" header="Validation Error">
          {error}
        </Alert>
      )}

      <Box variant="p" color="text-body-secondary">
        Supported ACGR region pairs: {SUPPORTED_REGIONS.join(", ")}
      </Box>
    </SpaceBetween>
  );
}
