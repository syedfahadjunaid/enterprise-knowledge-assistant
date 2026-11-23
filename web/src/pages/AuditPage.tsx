import { useAuditLog, useMe } from "../api/hooks";
import type { AuditEvent } from "../api/types";
import { Alert, Badge, Card, PageHeader, Spinner } from "../components/ui";

/** Admin-only view of the append-only audit log, newest first. */
export function AuditPage() {
  const me = useMe();
  const isAdmin = me.data?.is_admin ?? false;
  const events = useAuditLog(isAdmin);

  if (!isAdmin) return <Alert tone="error">The audit log is only available to knowledge base admins.</Alert>;

  return (
    <>
      <PageHeader title="Audit log" description="Who uploaded or deleted documents, and who was shown which documents in an answer." />
      <Card>
        {events.isPending ? (
          <Spinner />
        ) : events.isError ? (
          <Alert tone="error">{events.error.message}</Alert>
        ) : (
          <ul className="divide-y divide-slate-100 text-sm">
            {events.data.map((e) => (
              <li key={e.id} className="py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs text-slate-500">{new Date(e.occurred_at).toLocaleString()}</span>
                  <span className="font-medium">{e.actor_name}</span>
                  <Badge tone={e.action === "ANSWER_SOURCES_SHOWN" ? "indigo" : "slate"}>{e.action}</Badge>
                </div>
                <p className="mt-1 text-slate-600">{describe(e)}</p>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}

function describe(event: AuditEvent): string {
  const d = event.details;
  if (event.action === "ANSWER_SOURCES_SHOWN") {
    const passages = Array.isArray(d.passages) ? (d.passages as { document_title: string }[]) : [];
    const titles = [...new Set(passages.map((p) => p.document_title))];
    const shown = titles.length ? `shown ${titles.join(", ")}` : "no documents matched";
    return `Asked "${String(d.question ?? "")}": ${shown}`;
  }
  if (typeof d.title === "string") return d.title;
  return event.document_ids.length ? `${event.document_ids.length} document(s)` : "";
}
