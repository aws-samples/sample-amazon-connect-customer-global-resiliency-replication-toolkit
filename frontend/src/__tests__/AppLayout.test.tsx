import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import App from "../App";

vi.mock("../api/client", () => ({
  // Any call any child component might make on mount should resolve with safe defaults.
  getSessionStatus: vi.fn().mockResolvedValue(null),
  getExecutionStatus: vi.fn().mockRejectedValue(new Error("no sfn")),
  retryAssociation: vi.fn(),
  retryFailed: vi.fn(),
  retryResource: vi.fn(),
  associateResources: vi.fn(),
  selectiveCleanup: vi.fn(),
  cleanupSession: vi.fn(),
  listRecentSessions: vi.fn().mockResolvedValue([]),
  listInstances: vi.fn().mockResolvedValue({ instances: [] }),
  analyzeContactFlows: vi.fn(),
  getFlowAnalysisStatus: vi.fn(),
  validateInstance: vi.fn(),
  discover: vi.fn(),
  getInventory: vi.fn(),
  compareQuotas: vi.fn(),
  discoverTarget: vi.fn(),
  associateDiscovered: vi.fn(),
  replicate: vi.fn(),
  replicateAsync: vi.fn(),
  getReplicationStatus: vi.fn(),
  addResource: vi.fn(),
  auditTarget: vi.fn(),
  diffSession: vi.fn(),
}));

describe("App layout side navigation", () => {
  beforeEach(() => {
    // Reset the hash so the default route (Wizard) is active.
    window.location.hash = "";
  });

  it("renders a SideNavigation with the expected items and keeps the Dark mode toggle", () => {
    render(<App />);

    // The Cloudscape SideNavigation's header reads "ACGR Replicator".
    expect(screen.getByText(/ACGR Replicator/)).toBeInTheDocument();

    // All five primary nav items render as links (not buttons).
    const navItemLabels = [
      "Wizard",
      "Sessions",
      "Contact Flows",
      "Quota Comparison",
      "Discover & Associate",
    ];
    for (const label of navItemLabels) {
      const link = screen.getByRole("link", { name: label });
      expect(link).toBeInTheDocument();
    }

    // Primary navigation should NOT be rendered as top-nav buttons.
    for (const label of ["Wizard", "Sessions", "Contact Flows", "Quota Comparison"]) {
      expect(
        screen.queryByRole("button", { name: new RegExp(`^${label}$`) }),
      ).toBeNull();
    }

    // The Dark mode toggle still renders in the header.
    expect(screen.getByText(/Dark mode/i)).toBeInTheDocument();
  });
});
