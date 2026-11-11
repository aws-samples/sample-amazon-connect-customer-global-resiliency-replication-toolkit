import { createContext, useContext } from "react";

export interface AuthState {
  /**
   * "authenticated": signed in with Amazon Cognito.
   * "disabled": no authentication (local development only).
   */
  status: "authenticated" | "disabled";
  /** Signed-in user's email (or user name), when known. */
  email?: string;
  signOut: () => void;
}

/** Default when no provider is mounted (unit tests, local development). */
export const AuthContext = createContext<AuthState>({
  status: "disabled",
  signOut: () => {},
});

export function useAuth(): AuthState {
  return useContext(AuthContext);
}
