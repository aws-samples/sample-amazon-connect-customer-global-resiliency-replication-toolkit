import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import SessionStatusPage from "../components/Session/SessionStatusPage";
import type { SessionStatusResponse } from "../types";

vi.mock("../api/client", () => ({
  getSessionStatus: vi.fn(),
  getExecutionStatus: vi.fn(),
  retryAssociation: vi.fn(),
  retryFailed: vi.fn(),
  retryResource: vi.fn(),
  associateResources: vi.fn(),
  selectiveCleanup: vi.fn(),
  cleanupSession: vi.fn(),
}));

import * as api from "../api/client";

const SESSION_ID = "session-abc-123";
const RESOURCE_ID = "res-1";

function buildStatus(): SessionStatusResponse {
  return {
    sessionId: SESSION_ID,
    instanceArn:
      "arn:aws:connect:us-east-1:123456789012:instance/11111111-1111-1111-1111-111111111111",
    sourceRegion: "us-east-1",
    targetRegion: "us-west-2",
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    inventory: [
      {
        id: RESOURCE_ID,
        name: "my-fn",
        resource_type: "LAMBDA",
        arn: "arn:aws:lambda:us-east-1:123456789012:function:my-fn",
        replicated_arn: "arn:aws:lambda:us-west-2:123456789012:function:my-fn",
        status: "REPLICATED",
        error: null,
        error_classification: null,
        kms_info: null,
        association_status: "not_attempted",
        association_error: null,
        association_message: null,
        retryable: false,
      },
    ],
    associationResults: null,
    replicationJobs: [],
  };
}

describe("SessionStatusPage cleanup confirmation flow", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getSessionStatus).mockResolvedValue(buildStatus());
    vi.mocked(api.getExecutionStatus).mockRejectedValue(
      new Error("no sfn execution"),
    );
    vi.mocked(api.selectiveCleanup).mockResolvedValue({
      sessionId: SESSION_ID,
      deletedCount: 1,
      failedCount: 0,
      skippedCount: 0,
      entries: [],
    });
    vi.mocked(api.cleanupSession).mockResolvedValue({
      sessionId: SESSION_ID,
      deletedCount: 0,
      failedCount: 0,
      skippedCount: 0,
      entries: [],
    });
  });

  it("opens a confirmation modal before calling any cleanup API, then calls selectiveCleanup on confirm", async () => {
    render(<SessionStatusPage initialSessionId={SESSION_ID} />);

    // Wait for the Cleanup section to render
    await screen.findByRole("button", {
      name: /Clean Up Selected/i,
    });

    // Select the resource checkbox — find the checkbox input inside the cleanup list
    const checkbox = await screen.findByRole("checkbox", { name: /my-fn/ });
    fireEvent.click(checkbox);

    // Now the Clean Up Selected button should be enabled (count = 1)
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /Clean Up Selected \(1\)/ }),
      ).not.toBeDisabled();
    });

    fireEvent.click(
      screen.getByRole("button", { name: /Clean Up Selected \(1\)/ }),
    );

    // Modal opens; neither cleanup API has been called yet
    await screen.findByRole("button", { name: /Disassociate & Delete/i });
    expect(api.selectiveCleanup).not.toHaveBeenCalled();
    expect(api.cleanupSession).not.toHaveBeenCalled();

    // Click the destructive-primary button
    fireEvent.click(
      screen.getByRole("button", { name: /Disassociate & Delete/i }),
    );

    await waitFor(() => {
      expect(api.selectiveCleanup).toHaveBeenCalledTimes(1);
    });
    expect(api.selectiveCleanup).toHaveBeenCalledWith(SESSION_ID, [RESOURCE_ID]);
    expect(api.cleanupSession).not.toHaveBeenCalled();
  });

  it("cancels without calling any cleanup API", async () => {
    render(<SessionStatusPage initialSessionId={SESSION_ID} />);

    await screen.findByRole("button", { name: /Clean Up Selected/i });
    const checkbox = await screen.findByRole("checkbox", { name: /my-fn/ });
    fireEvent.click(checkbox);

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /Clean Up Selected \(1\)/ }),
      ).not.toBeDisabled();
    });
    fireEvent.click(
      screen.getByRole("button", { name: /Clean Up Selected \(1\)/ }),
    );

    await screen.findByRole("button", { name: /^Cancel$/ });
    fireEvent.click(screen.getByRole("button", { name: /^Cancel$/ }));

    // No cleanup API calls
    expect(api.selectiveCleanup).not.toHaveBeenCalled();
    expect(api.cleanupSession).not.toHaveBeenCalled();
  });
});
