/**
 * Runtime sign-in configuration.
 *
 * The values (Amazon Cognito user pool, app client and hosted-UI domain) are
 * generated at deploy time from the CloudFormation stack outputs and uploaded
 * next to the app as `/config.json` (see scripts/write-frontend-config.sh).
 * They are identifiers, not secrets: the app client is a public client that
 * uses the authorization code flow with PKCE and has no client secret.
 */

export interface AuthConfig {
  /** AWS Region of the user pool, e.g. "eu-west-2". */
  region: string;
  /** User pool ID, e.g. "eu-west-2_AbCdEfGhI". */
  userPoolId: string;
  /** App client ID. */
  clientId: string;
  /** Hosted-UI host name without scheme, e.g. "my-prefix.auth.eu-west-2.amazoncognito.com". */
  domain: string;
}

export type AuthConfigResult =
  | { status: "ok"; config: AuthConfig }
  /** Authentication is switched off (local development only). */
  | { status: "disabled" }
  /** Authentication is required but not configured: the app must not render. */
  | { status: "error"; message: string };

const REGION_RE = /^[a-z]{2}(-[a-z]+)+-\d$/;
const USER_POOL_RE = /^[a-z]{2}(-[a-z]+)+-\d_[0-9A-Za-z]+$/;
const CLIENT_ID_RE = /^[0-9a-z]{10,64}$/;
// A bare host name: no scheme, path, port or credentials.
const DOMAIN_RE = /^(?=.{4,253}$)([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$/;

/** Validate untrusted JSON and return a typed config, or null when invalid. */
export function parseAuthConfig(raw: unknown): AuthConfig | null {
  if (typeof raw !== "object" || raw === null) return null;
  const r = raw as Record<string, unknown>;
  const { region, userPoolId, clientId, domain } = r;
  if (
    typeof region !== "string" || !REGION_RE.test(region) ||
    typeof userPoolId !== "string" || !USER_POOL_RE.test(userPoolId) ||
    typeof clientId !== "string" || !CLIENT_ID_RE.test(clientId) ||
    typeof domain !== "string" || !DOMAIN_RE.test(domain)
  ) {
    return null;
  }
  // The pool must belong to the stated Region.
  if (!userPoolId.startsWith(`${region}_`)) return null;
  return { region, userPoolId, clientId, domain };
}

/**
 * Load `/config.json`.
 *
 * In a production build a missing or invalid file is an error (fail closed:
 * the app never renders unauthenticated). In `vite dev` it means "no auth",
 * so the UI can run against a local backend that has no authorizer in front.
 */
export async function loadAuthConfig(
  fetchFn: typeof fetch = fetch,
  isDev: boolean = import.meta.env.DEV,
): Promise<AuthConfigResult> {
  const notConfigured: AuthConfigResult = isDev
    ? { status: "disabled" }
    : {
        status: "error",
        message:
          "Sign-in is not configured. The deployment is missing /config.json. " +
          "Re-run the deployment, or see the README (Authentication).",
      };
  try {
    const res = await fetchFn("/config.json", { cache: "no-store" });
    if (!res.ok) return notConfigured;
    const parsed = parseAuthConfig(await res.json());
    if (!parsed) {
      return isDev
        ? { status: "disabled" }
        : { status: "error", message: "Sign-in configuration (/config.json) is invalid." };
    }
    return { status: "ok", config: parsed };
  } catch {
    return notConfigured;
  }
}
