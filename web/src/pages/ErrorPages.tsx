import { Link, useRouteError } from "react-router";

import { FullPageMessage } from "../components/ui";

export function NotFoundPage() {
  return (
    <div className="py-16 text-center">
      <h1 className="text-lg font-semibold">Page not found</h1>
      <Link to="/" className="mt-4 inline-block text-sm text-indigo-700 underline">
        Back to the assistant
      </Link>
    </div>
  );
}

export function RouteErrorPage() {
  const error = useRouteError();
  const message = error instanceof Error ? error.message : "Something went wrong.";
  return <FullPageMessage title="Something went wrong" message={message} />;
}
