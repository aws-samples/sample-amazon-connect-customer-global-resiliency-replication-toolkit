import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { listRecentSessions, setAuthHooks } from "../api/client";

function mockFetch(status: number, body: unknown = []) {
  const fn = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    statusText: "x",
    text: async () => JSON.stringify(body),
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

const sentHeaders = (fn: ReturnType<typeof vi.fn>) =>
  (fn.mock.calls[0][1] as RequestInit).headers as Record<string, string>;

describe("API client authentication", () => {
  beforeEach(() => setAuthHooks({ getToken: null }));
  afterEach(() => vi.unstubAllGlobals());

  it("sends no Authorization header when authentication is off", async () => {
    const f = mockFetch(200);
    await listRecentSessions();
    expect(sentHeaders(f).Authorization).toBeUndefined();
    expect(sentHeaders(f)["Content-Type"]).toBe("application/json");
  });

  it("attaches the ID token when authentication is on", async () => {
    setAuthHooks({ getToken: async () => "the-id-token" });
    const f = mockFetch(200);
    await listRecentSessions();
    expect(sentHeaders(f).Authorization).toBe("the-id-token");
  });

  it("asks to sign in again on 401 and does not return data", async () => {
    const onUnauthorized = vi.fn();
    setAuthHooks({ getToken: async () => "expired", onUnauthorized });
    mockFetch(401, { message: "Unauthorized" });
    await expect(listRecentSessions()).rejects.toThrow(/session has expired/i);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("does not trigger sign-in on other errors", async () => {
    const onUnauthorized = vi.fn();
    setAuthHooks({ getToken: async () => "t", onUnauthorized });
    mockFetch(500, { detail: "boom" });
    await expect(listRecentSessions()).rejects.toThrow("boom");
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it("does not send the request when the token cannot be obtained", async () => {
    setAuthHooks({ getToken: async () => { throw new Error("Redirecting to sign-in"); } });
    const f = mockFetch(200);
    await expect(listRecentSessions()).rejects.toThrow(/sign-in/i);
    expect(f).not.toHaveBeenCalled();
  });
});
