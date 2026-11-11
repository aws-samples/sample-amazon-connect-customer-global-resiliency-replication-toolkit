import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import fc from "fast-check";
import ContactFlowAnalysis from "../components/ContactFlows/ContactFlowAnalysis";

vi.mock("../api/client", () => ({
  listInstances: vi.fn().mockResolvedValue({ instances: [] }),
  analyzeContactFlows: vi.fn(),
  getFlowAnalysisStatus: vi.fn(),
}));

const STORAGE_KEY = "replicator.wizard.instanceArn";

async function findManualArnInput(): Promise<HTMLInputElement> {
  // The Manual ARN Entry tab renders an input with the placeholder that starts with "arn:aws:connect:us-east-1:..."
  const input = (await screen.findByPlaceholderText(
    /^arn:aws:connect:us-east-1:/,
  )) as HTMLInputElement;
  return input;
}

function switchToManualTab(): void {
  // Cloudscape Tabs render each tab label as a clickable element. Click the "Manual ARN Entry" label.
  const tab = screen.getByText(/Manual ARN Entry/i);
  fireEvent.click(tab);
}

/**
 * Validates: Requirements 3.3, 3.4 (Property 7 — ARN pre-fill round-trip)
 */
describe("ContactFlowAnalysis ARN pre-fill (Property 7)", () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  it("pre-fills the Manual ARN input with any stored Connect instance ARN (property)", async () => {
    const arnGen = fc
      .tuple(
        fc.constantFrom("us-east-1", "us-west-2"),
        fc.hexaString({ minLength: 8, maxLength: 8 }),
        fc.hexaString({ minLength: 4, maxLength: 4 }),
        fc.hexaString({ minLength: 4, maxLength: 4 }),
        fc.hexaString({ minLength: 4, maxLength: 4 }),
        fc.hexaString({ minLength: 12, maxLength: 12 }),
      )
      .map(
        ([r, a, b, c, d, e]) =>
          `arn:aws:connect:${r}:123456789012:instance/${a}-${b}-${c}-${d}-${e}`,
      );

    await fc.assert(
      fc.asyncProperty(arnGen, async (arn) => {
        sessionStorage.clear();
        sessionStorage.setItem(STORAGE_KEY, arn);

        const { unmount } = render(<ContactFlowAnalysis />);
        try {
          switchToManualTab();
          const input = await findManualArnInput();
          await waitFor(() => {
            expect(input.value).toBe(arn);
          });
        } finally {
          unmount();
          sessionStorage.clear();
        }
      }),
      { numRuns: 8 },
    );
  });

  it("leaves the Manual ARN input empty when sessionStorage has no stored ARN", async () => {
    sessionStorage.clear();

    render(<ContactFlowAnalysis />);
    switchToManualTab();
    const input = await findManualArnInput();
    expect(input.value).toBe("");
  });
});
