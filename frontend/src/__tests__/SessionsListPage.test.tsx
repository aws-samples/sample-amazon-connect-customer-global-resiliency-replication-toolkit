import { render, screen, waitForElementToBeRemoved } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import SessionsListPage from "../components/Session/SessionsListPage";
import type { SessionSummary } from "../types";

vi.mock("../api/client", () => ({
  listRecentSessions: vi.fn(),
}));

import * as api from "../api/client";

function makeSession(idx: number): SessionSummary {
  const sid = `session-${String(idx).padStart(8, "0")}-aaaa-bbbb-cccc-dddddddddddd`;
  return {
    sessionId: sid,
    instanceArn: `arn:aws:connect:us-east-1:123456789012:instance/${sid}`,
    sourceRegion: "us-east-1",
    targetRegion: "us-west-2",
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    resourceCount: 5,
  };
}

describe("SessionsListPage states", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders a loading indicator while sessions are being fetched", async () => {
    // Never resolves — the component stays in its loading state.
    vi.mocked(api.listRecentSessions).mockReturnValue(new Promise(() => {}));

    render(<SessionsListPage />);

    expect(await screen.findByText(/Loading sessions\.\.\./i)).toBeInTheDocument();
  });

  it("renders empty-state heading and CTA when no sessions exist", async () => {
    vi.mocked(api.listRecentSessions).mockResolvedValue([]);

    render(<SessionsListPage />);

    // Wait for the loading indicator to disappear
    await waitForElementToBeRemoved(() => screen.queryByText(/Loading sessions\.\.\./i));

    expect(screen.getByText(/No sessions yet/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Start a new replication/i }),
    ).toBeInTheDocument();
  });

  it("renders a row per session when the list is populated", async () => {
    const sessions = [makeSession(1), makeSession(2)];
    vi.mocked(api.listRecentSessions).mockResolvedValue(sessions);

    render(<SessionsListPage />);

    await waitForElementToBeRemoved(() => screen.queryByText(/Loading sessions\.\.\./i));

    // Each session's unique 8-digit suffix should appear at least once on the
    // page. We use getAllByText because the session ID shows up both in the
    // short-link cell and inside the full instance ARN cell on the same row.
    for (const s of sessions) {
      const unique = s.sessionId.substring(8, 16); // e.g. "00000001"
      const matches = screen.getAllByText(new RegExp(unique));
      expect(matches.length).toBeGreaterThan(0);
    }
  });
});
