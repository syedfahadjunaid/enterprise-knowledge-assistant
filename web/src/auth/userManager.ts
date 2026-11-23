import { InMemoryWebStorage, type User, UserManager, WebStorageStateStore } from "oidc-client-ts";

/**
 * OpenID Connect client for Keycloak.
 *
 * Flow: authorization code + PKCE (oidc-client-ts always uses PKCE for the code flow).
 * The SPA is a *public* client: it has no client secret, because anything shipped to a
 * browser can be read by anyone.
 *
 * Tokens live in memory only, never in localStorage, so they disappear when the tab closes and
 * can't be picked up later from disk. On a page reload the app simply signs in again; Keycloak
 * still has a session, so that's an instant redirect with no password prompt.
 */
export const userManager = new UserManager({
  authority: import.meta.env.VITE_OIDC_AUTHORITY ?? "http://localhost:8380/realms/eka",
  client_id: "eka-web",
  redirect_uri: `${window.location.origin}/`,
  post_logout_redirect_uri: `${window.location.origin}/`,
  scope: "openid profile email",
  userStore: new WebStorageStateStore({ store: new InMemoryWebStorage() }),
  // Uses the refresh token to get a new 5-minute access token before the old one expires.
  automaticSilentRenew: true,
});

type ReturnState = { returnTo?: string };

/**
 * Set once we've started sending the browser to Keycloak. The page is about to unload, so
 * nothing else may start another redirect. Without this, signing out clears the user, the
 * sign-in gate sees "not signed in" and starts a sign-in that cancels the logout.
 * It also collapses several simultaneous 401 responses into a single redirect.
 */
let leavingFor: "signin" | "signout" | null = null;

export function leavingPage() {
  return leavingFor;
}

/** Sends the browser to the Keycloak login page, remembering where the user was. */
export async function signIn(returnTo = window.location.pathname + window.location.search) {
  if (leavingFor) return;
  leavingFor = "signin";
  try {
    await userManager.signinRedirect({ state: { returnTo } satisfies ReturnState });
  } catch (error) {
    leavingFor = null;
    throw error;
  }
}

/** Ends the Keycloak session too (not just this tab), then comes back to the login page. */
export async function signOut() {
  if (leavingFor) return;
  leavingFor = "signout";
  try {
    await userManager.signoutRedirect();
  } catch (error) {
    leavingFor = null;
    throw error;
  }
}

/**
 * Only same-site paths are allowed as a post-login destination. Without this check a crafted
 * login link could bounce the user to another site after sign-in (an "open redirect").
 */
export function safeReturnTo(path: unknown): string {
  if (typeof path !== "string" || !path.startsWith("/") || path.startsWith("//") || path.includes("\\")) {
    return "/";
  }
  return path;
}

/** Runs after Keycloak redirects back: remove ?code=&state= from the URL, return to the page. */
export function onSigninCallback(user: User | undefined) {
  const returnTo = safeReturnTo((user?.state as ReturnState | undefined)?.returnTo);
  window.history.replaceState({}, document.title, returnTo);
}
