import { useState } from "react";
import Modal from "@cloudscape-design/components/modal";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";

const ARN_PATTERN = /^arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:.+$/;

interface AddResourceModalProps {
  visible: boolean;
  onDismiss: () => void;
  onAdd: (arn: string) => Promise<void>;
}

export default function AddResourceModal({ visible, onDismiss, onAdd }: AddResourceModalProps) {
  const [arn, setArn] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isValid = ARN_PATTERN.test(arn);

  async function handleSubmit() {
    if (!isValid) return;
    setLoading(true);
    setError(null);
    try {
      await onAdd(arn);
      setArn("");
      onDismiss();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to add resource");
    } finally {
      setLoading(false);
    }
  }

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header="Add Resource"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>Cancel</Button>
            <Button variant="primary" loading={loading} disabled={!isValid} onClick={handleSubmit}>
              Add
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <FormField
          label="Resource ARN"
          description="Enter the ARN of the resource to add to the inventory"
          errorText={arn.length > 0 && !isValid ? "Invalid ARN format" : undefined}
        >
          <Input
            value={arn}
            onChange={({ detail }) => setArn(detail.value)}
            placeholder="arn:aws:lambda:us-west-2:123456789012:function:my-function"
          />
        </FormField>
        {error && <Alert type="error">{error}</Alert>}
      </SpaceBetween>
    </Modal>
  );
}
