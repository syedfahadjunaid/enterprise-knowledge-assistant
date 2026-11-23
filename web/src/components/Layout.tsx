import { NavLink, Outlet } from "react-router";

import { useMe } from "../api/hooks";
import { principalLabel } from "../api/types";
import { signOut } from "../auth/userManager";
import { Alert, Badge, Button, Spinner } from "./ui";

type NavItem = { to: string; label: string; adminOnly?: boolean };

const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Ask" },
  { to: "/documents", label: "Documents" },
  { to: "/audit", label: "Audit log", adminOnly: true },
];

/** Page frame: header with navigation and the signed-in user. Role checks here are cosmetic;
 * the API enforces every permission itself. */
export function Layout() {
  const me = useMe();
  const items = NAV_ITEMS.filter((item) => !item.adminOnly || me.data?.is_admin);

  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex h-16 max-w-5xl items-center gap-6 px-4 sm:px-6">
          <div className="flex items-center gap-2 font-semibold">
            <img src="/favicon.svg" alt="" className="size-7" />
            Knowledge Assistant
          </div>
          <nav aria-label="Main" className="flex gap-1">
            {items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                className={({ isActive }) =>
                  `rounded-md px-3 py-2 text-sm font-medium ${
                    isActive ? "bg-indigo-50 text-indigo-700" : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3">
            {me.data && (
              <div className="hidden text-right sm:block">
                <div className="text-sm font-medium">{me.data.name}</div>
                <div className="flex justify-end gap-1">
                  {me.data.groups.map((g) => (
                    <Badge key={g} tone="indigo">
                      {principalLabel(`group:${g}`)}
                    </Badge>
                  ))}
                  {me.data.roles
                    .filter((r) => r.startsWith("kb-"))
                    .map((r) => (
                      <Badge key={r}>{r}</Badge>
                    ))}
                </div>
              </div>
            )}
            <Button variant="secondary" onClick={() => void signOut()}>
              Sign out
            </Button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-5xl px-4 py-8 sm:px-6">
        {me.isPending ? (
          <Spinner />
        ) : me.isError ? (
          <Alert tone="error">Couldn't load your profile: {me.error.message}</Alert>
        ) : (
          <Outlet />
        )}
      </main>
    </div>
  );
}
