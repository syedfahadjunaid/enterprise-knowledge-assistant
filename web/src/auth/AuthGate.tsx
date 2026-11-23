import { type ReactNode, useEffect } from "react";
import { hasAuthParams, useAuth } from "react-oidc-context";

import { Button, FullPageMessage } from "../components/ui";
import { leavingPage, signIn } from "./userManager";

/** Renders the app only for signed-in users; everyone else is sent to Keycloak. */
export function AuthGate({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const leaving = leavingPage();

  const mustSignIn =
    !hasAuthParams() && // not in the middle of handling Keycloak's redirect back
    !auth.isAuthenticated &&
    !auth.activeNavigator &&
    !auth.isLoading &&
    !auth.error &&
    !leaving; // a sign-in or sign-out redirect is already under way

  useEffect(() => {
    if (mustSignIn) void signIn();
  }, [mustSignIn]);

  if (auth.error) {
    return (
      <FullPageMessage title="Sign-in failed" message={auth.error.message}>
        <Button onClick={() => void signIn("/")}>Try again</Button>
      </FullPageMessage>
    );
  }
  if (!auth.isAuthenticated) {
    return <FullPageMessage title={leaving === "signout" ? "Signing you out…" : "Signing you in…"} busy />;
  }
  return <>{children}</>;
}
