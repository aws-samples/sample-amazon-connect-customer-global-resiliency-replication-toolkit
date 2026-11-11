import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import ContactFlowAnalysis from "../components/ContactFlows/ContactFlowAnalysis";
import { analyzeContactFlows, getFlowAnalysisStatus } from "../api/client";

vi.mock("../api/client", () => ({
  listInstances: vi.fn().mockResolvedValue({ instances: [] }),
  analyzeContactFlows: vi.fn(),
  getFlowAnalysisStatus: vi.fn(),
}));

const ARN = "arn:aws:connect:us-east-1:123456789012:instance/abc-123";

async function runAnalysis(skipped: number) {
  vi.mocked(analyzeContactFlows).mockResolvedValue({ job_id: "j1", status: "PENDING" });
  vi.mocked(getFlowAnalysisStatus).mockResolvedValue({
    job_id: "j1", status: "COMPLETED", total_flows: 27, analyzed_flows: 27 - skipped,
    flows_with_lex: 0, flows_with_lambda: 0, truncated: false, skipped_flows: skipped,
    error: null, flows: [],
  });
  render(<ContactFlowAnalysis />);
  fireEvent.click(screen.getByText(/Manual ARN Entry/i));
  const input = await screen.findByPlaceholderText(/^arn:aws:connect:us-east-1:/);
  fireEvent.change(input, { target: { value: ARN } });
  fireEvent.click(screen.getByRole("button", { name: /^Analyze$/ }));
}

describe("Contact flow analysis: flows that could not be read", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
  });

  it("warns, with the numbers, when some flows were skipped", async () => {
    await runAnalysis(9);
    await waitFor(() => expect(screen.getByText("Some flows could not be read")).toBeTruthy(), { timeout: 8000 });
    expect(screen.getByText(/9 of 27 flows were skipped/)).toBeTruthy();
  });

  it("shows no warning when every flow was read", async () => {
    await runAnalysis(0);
    await waitFor(() => expect(screen.getByText("Summary")).toBeTruthy(), { timeout: 8000 });
    expect(screen.queryByText("Some flows could not be read")).toBeNull();
  });
});
