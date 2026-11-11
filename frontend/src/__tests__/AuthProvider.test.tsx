import { render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

const mgr = vi.hoisted(() => ({
  signinRedirect: vi.fn().mockResolvedValue(undefined),
  signinCallback: vi.fn(),
  getUser: vi.fn(),
  removeUser: vi.fn().mockResolvedValue(undefined),
  events: { addSilentRenewError: vi.fn(), addUserSignedOut: vi.fn() },
}));
vi.mock("oidc-client-ts", () => ({
  UserManager: vi.fn(() => mgr),
  WebStorageStateStore: vi.fn(),
}));

const loadAuthConfig = vi.hoisted(() => vi.fn());
vi.mock("../auth/config", () => ({ loadAuthConfig }));

const setAuthHooks = vi.hoisted(() => vi.fn());
vi.mock("../api/client", () => ({ setAuthHooks }));

import AuthProvider, { resetAuthForTests } from "../auth/AuthProvider";

const CFG = {
  status: "ok",
  config: {
    region: "eu-west-2",
    userPoolId: "eu-west-2_AbCdEf123",
    clientId: "1abc2def3ghi4jkl5mno6pqr7s",
    domain: "p.auth.eu-west-2.amazoncognito.com",
  },
};
const USER = { expired: false, id_token: "idtok", profile: { email: "me@example.com" } };

const renderGate = () =>
  render(
    <AuthProvider>
      <div>PROTECTED APP</div>
    </AuthProvider>,
  );

describe("AuthProvider (sign-in gate)", () => {
  beforeEach(() => {
    resetAuthForTests();
    vi.clearAllMocks();
    window.history.replaceState(null, "", "/");
    loadAuthConfig.mockResolvedValue(CFG);
    mgr.getUser.mockResolvedValue(null);
  });

  it("fails closed when sign-in is required but not configured", async () => {
    loadAuthConfig.mockResolvedValue({ status: "error", message: "Sign-in is not configured." });
    renderGate();
    expect(await screen.findByText("Sign-in is not configured.")).toBeTruthy();
    expect(screen.queryByText("PROTECTED APP")).toBeNull();
    expect(mgr.signinRedirect).not.toHaveBeenCalled();
  });

  it("renders the app without sign-in only when authentication is disabled (development)", async () => {
    loadAuthConfig.mockResolvedValue({ status: "disabled" });
    renderGate();
    expect(await screen.findByText("PROTECTED APP")).toBeTruthy();
    expect(setAuthHooks).toHaveBeenCalledWith({ getToken: null });
  });

  it("redirects to the hosted sign-in page when there is no session, and shows no app", async () => {
    renderGate();
    expect(await screen.findByText(/Redirecting to sign-in/)).toBeTruthy();
    expect(mgr.signinRedirect).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("PROTECTED APP")).toBeNull();
  });

  it("redirects when the stored session has expired", async () => {
    mgr.getUser.mockResolvedValue({ ...USER, expired: true });
    renderGate();
    expect(await screen.findByText(/Redirecting to sign-in/)).toBeTruthy();
    expect(screen.queryByText("PROTECTED APP")).toBeNull();
  });

  it("renders the app and registers the token hooks with a valid session", async () => {
    mgr.getUser.mockResolvedValue(USER);
    renderGate();
    expect(await screen.findByText("PROTECTED APP")).toBeTruthy();
    const hooks = setAuthHooks.mock.calls.at(-1)![0];
    await expect(hooks.getToken()).resolves.toBe("idtok");
    expect(mgr.signinRedirect).not.toHaveBeenCalled();
  });

  it("completes the sign-in callback once, cleans the URL and restores the page", async () => {
    window.history.replaceState(null, "", "/?code=abc&state=xyz");
    mgr.signinCallback.mockResolvedValue({ state: { hash: "#/quotas" } });
    mgr.getUser.mockResolvedValue(USER);
    renderGate();
    expect(await screen.findByText("PROTECTED APP")).toBeTruthy();
    expect(mgr.signinCallback).toHaveBeenCalledTimes(1);
    expect(window.location.search).toBe("");
    expect(window.location.hash).toBe("#/quotas");
  });

  it("ignores a returned hash that is not an in-app route", async () => {
    window.history.replaceState(null, "", "/?code=abc&state=xyz");
    mgr.signinCallback.mockResolvedValue({ state: { hash: "javascript:alert(1)" } });
    mgr.getUser.mockResolvedValue(USER);
    renderGate();
    await screen.findByText("PROTECTED APP");
    expect(window.location.hash).toBe("");
  });

  it("shows an error, not the app, when the callback fails", async () => {
    window.history.replaceState(null, "", "/?code=abc&state=xyz");
    mgr.signinCallback.mockRejectedValue(new Error("bad state"));
    renderGate();
    expect(await screen.findByText(/could not be completed/i)).toBeTruthy();
    expect(screen.queryByText("PROTECTED APP")).toBeNull();
  });

  it("shows the error returned by the hosted sign-in page", async () => {
    window.history.replaceState(null, "", "/?error=access_denied&error_description=User+is+disabled");
    renderGate();
    await waitFor(() => expect(screen.getByText(/Sign-in failed/)).toBeTruthy());
    expect(screen.queryByText("PROTECTED APP")).toBeNull();
  });
});
