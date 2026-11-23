import { useState } from "react";
import { createBrowserRouter, type RouteObject, RouterProvider } from "react-router";

import { Layout } from "./components/Layout";
import { AskPage } from "./pages/AskPage";
import { AuditPage } from "./pages/AuditPage";
import { DocumentsPage } from "./pages/DocumentsPage";
import { NotFoundPage, RouteErrorPage } from "./pages/ErrorPages";

const routes: RouteObject[] = [
  {
    element: <Layout />,
    errorElement: <RouteErrorPage />,
    children: [
      { index: true, element: <AskPage /> },
      { path: "documents", element: <DocumentsPage /> },
      { path: "audit", element: <AuditPage /> },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];

export function App() {
  // Created only after sign-in, so the router starts from the URL we returned to
  // (not Keycloak's ?code=... callback URL).
  const [router] = useState(() => createBrowserRouter(routes));
  return <RouterProvider router={router} />;
}
