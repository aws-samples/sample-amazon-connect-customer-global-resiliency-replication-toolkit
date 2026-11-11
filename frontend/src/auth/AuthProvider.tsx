/**
 * Sign-in gate for the whole app (Amazon Cognito, authorization code flow + PKCE).
 *
 * The app renders only once a valid session exists. Until then it shows a
 * short status message, and it redirects to the Cognito hosted sign-in page.
 * If sign-in is required but not configured, it shows an error and renders
 * nothing (fail closed).
 *
 * The API client receives the user's ID token through `setAuthHooks`, and
 * API Gateway's Cognito authorizer verifies it on every request.
 */

import { useEffect, useState, type ReactNode } from "react";
import { UserManager, WebStorageStateStore } from "oidc-client-ts";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Spinner from "@cloudscape-design/components/spinner";
import { setAuthHooks } from "../api/client";
import { loadAuthConfig, type AuthConfig } from "./config";
import { AuthContext, type AuthState } from "./useAuth";

type Phase =
  | { kind: "loading" }
  | { kind: "redirecting" }
  | { kind: "error"; message: string }
  | { kind: "ready"; state: AuthState };

function createUserManager(cfg: AuthConfig): UserManager {
  return new UserManager({
    authority: `https://cognito-idp.${cfg.region}.amazonaws.com/${cfg.userPoolId}`,
    client_id: cfg.clientId,
    redirect_uri: `${window.location.origin}/`,
    response_type: "code", // authorization code + PKCE (no client secret)
    scope: "openid email profile",
    // Tokens live in sessionStorage: cleared when the tab is closed.
    userStore: new WebStorageStateStore({ store: window.sessionStorage }),
    stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
    automaticSilentRenew: true,
    loadUserInfo: false,
    // Endpoints are published by the user pool's discovery document; the seed
    // only guarantees the hosted-UI host is the configured one.
    metadataSeed: {
      authorization_endpoint: `https://${cfg.domain}/oauth2/authorize`,
      token_endpoint: `https://${cfg.domain}/oauth2/token`,
    },
  });
}

/** Keep the user on the page they asked for (the app routes with the URL hash). */
function returnToApp(hash: string | undefined): void {
  const safeHash = typeof hash === "string" && hash.startsWith("#/") ? hash : "";
  window.history.replaceState(null, "", `${window.location.pathname}${safeHash}`);
}

async function initialise(): Promise<Phase> {
  const result = await loadAuthConfig();
  if (result.status === "disabled") {
    setAuthHooks({ getToken: null });
    return { kind: "ready", state: { status: "disabled", signOut: () => {} } };
  }
  if (result.status === "error") {
    return { kind: "error", message: result.message };
  }

  const cfg = result.config;
  const mgr = createUserManager(cfg);
  const signIn = (): Promise<void> =>
    mgr.signinRedirect({ state: { hash: window.location.hash } });
  const restartSignIn = (): void => {
    void mgr.removeUser().then(signIn);
  };

  const params = new URLSearchParams(window.location.search);
  if (params.has("error")) {
    const detail = params.get("error_description") ?? params.get("error") ?? "unknown error";
    returnToApp(undefined);
    return { kind: "error", message: `Sign-in failed: ${detail.slice(0, 200)}` };
  }

  if (params.has("code") && params.has("state")) {
    try {
      const signedIn = await mgr.signinCallback();
      returnToApp((signedIn?.state as { hash?: string } | undefined)?.hash);
    } catch {
      returnToApp(undefined);
      return {
        kind: "error",
        message: "Sign-in could not be completed. Please try again.",
      };
    }
  }

  const user = await mgr.getUser();
  if (!user || user.expired) {
    await signIn();
    return { kind: "redirecting" };
  }

  setAuthHooks({
    getToken: async () => {
      const current = await mgr.getUser();
      if (!current || current.expired || !current.id_token) {
        restartSignIn();
        throw new Error("Your session has expired. Redirecting to sign-in…");
      }
      return current.id_token;
    },
    onUnauthorized: restartSignIn,
  });
  mgr.events.addSilentRenewError(restartSignIn);
  mgr.events.addUserSignedOut(restartSignIn);

  const profile = user.profile as Record<string, unknown>;
  const email =
    (typeof profile.email === "string" && profile.email) ||
    (typeof profile["cognito:username"] === "string" && (profile["cognito:username"] as string)) ||
    undefined;

  return {
    kind: "ready",
    state: {
      status: "authenticated",
      email,
      signOut: () => {
        void mgr.removeUser().then(() => {
          const logout =
            `https://${cfg.domain}/logout?client_id=${encodeURIComponent(cfg.clientId)}` +
            `&logout_uri=${encodeURIComponent(`${window.location.origin}/`)}`;
          window.location.assign(logout);
        });
      },
    },
  };
}

// Run once per page load. The authorization code is single-use, so React
// StrictMode's double-invoked effects must not run the callback twice.
let started: Promise<Phase> | null = null;

/** Test helper: forget the cached initialisation. */
export function resetAuthForTests(): void {
  started = null;
}

export default function AuthProvider({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });

  useEffect(() => {
    let alive = true;
    started ??= initialise().catch(
      (): Phase => ({
        kind: "error",
        message: "Sign-in could not be started. Please reload the page.",
      }),
    );
    void started.then((p) => {
      if (alive) setPhase(p);
    });
    return () => {
      alive = false;
    };
  }, []);

  if (phase.kind === "ready") {
    return <AuthContext.Provider value={phase.state}>{children}</AuthContext.Provider>;
  }

  if (phase.kind === "error") {
    return (
      <Box margin="xxl" padding="l">
        <Alert
          type="error"
          header="Cannot sign in"
          action={
            <Button
              onClick={() => {
                started = null;
                window.location.assign(`${window.location.origin}/`);
              }}
            >
              Try again
            </Button>
          }
        >
          {phase.message}
        </Alert>
      </Box>
    );
  }

  return (
    <Box margin="xxl" padding="l" textAlign="center">
      <div role="status" aria-live="polite">
        <Spinner size="large" />{" "}
        {phase.kind === "redirecting" ? "Redirecting to sign-in…" : "Loading…"}
      </div>
    </Box>
  );
}
