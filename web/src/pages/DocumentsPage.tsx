import { type FormEvent, useState } from "react";

import { ApiError } from "../api/client";
import { useDeleteDocument, useDocuments, useMe, useUploadDocument } from "../api/hooks";
import { type DocumentStatus, type KnowledgeDocument, principalLabel } from "../api/types";
import { Alert, Badge, type BadgeTone, Button, Card, Field, PageHeader, Spinner, inputClass } from "../components/ui";

const STATUS_TONE: Record<DocumentStatus, BadgeTone> = {
  PENDING: "amber",
  PROCESSING: "amber",
  READY: "green",
  FAILED: "red",
};

export function DocumentsPage() {
  const me = useMe();
  const documents = useDocuments();

  return (
    <>
      <PageHeader
        title="Documents"
        description="Every document you can read. Answers draw only on these."
      />
      {me.data?.can_upload && <UploadForm shareTargets={me.data.share_targets} />}
      <Card className="mt-6">
        {documents.isPending ? (
          <Spinner />
        ) : documents.isError ? (
          <Alert tone="error">{documents.error.message}</Alert>
        ) : documents.data.length === 0 ? (
          <p className="text-sm text-slate-500">No documents are shared with you yet.</p>
        ) : (
          <DocumentTable documents={documents.data} />
        )}
      </Card>
    </>
  );
}

function DocumentTable({ documents }: { documents: KnowledgeDocument[] }) {
  const remove = useDeleteDocument();
  return (
    <div className="overflow-x-auto">
      {remove.isError && (
        <div className="mb-3">
          <Alert tone="error">{remove.error.message}</Alert>
        </div>
      )}
      <table className="w-full text-left text-sm">
        <thead className="text-xs text-slate-500 uppercase">
          <tr>
            <th className="py-2 pr-4 font-medium">Title</th>
            <th className="py-2 pr-4 font-medium">Shared with</th>
            <th className="py-2 pr-4 font-medium">Owner</th>
            <th className="py-2 pr-4 font-medium">Status</th>
            <th className="py-2" />
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {documents.map((d) => (
            <tr key={d.id} className="align-top">
              <td className="py-3 pr-4">
                <div className="font-medium">{d.title}</div>
                <div className="text-xs text-slate-500">
                  {d.filename} · {d.file_type} · {formatSize(d.size_bytes)}
                </div>
              </td>
              <td className="py-3 pr-4">
                <div className="flex flex-wrap gap-1">
                  {d.shared_with.length === 0 ? (
                    <Badge>Only the owner</Badge>
                  ) : (
                    d.shared_with.map((p) => (
                      <Badge key={p} tone="indigo">
                        {principalLabel(p)}
                      </Badge>
                    ))
                  )}
                </div>
              </td>
              <td className="py-3 pr-4">{d.is_owner ? "You" : d.owner_name}</td>
              <td className="py-3 pr-4">
                <Badge tone={STATUS_TONE[d.status]}>{d.status.toLowerCase()}</Badge>
                {d.status === "READY" && <div className="mt-1 text-xs text-slate-500">{d.chunk_count} passages</div>}
                {d.error && <div className="mt-1 max-w-56 text-xs text-red-700">{d.error}</div>}
              </td>
              <td className="py-3 text-right">
                {d.can_delete && (
                  <Button
                    variant="ghost"
                    disabled={remove.isPending}
                    onClick={() => {
                      if (window.confirm(`Delete "${d.title}"? This can't be undone.`)) remove.mutate(d.id);
                    }}
                  >
                    Delete
                  </Button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const ACCEPT = ".pdf,.docx,.md,.markdown,.html,.htm,.txt";

export function UploadForm({ shareTargets }: { shareTargets: string[] }) {
  const upload = useUploadDocument();
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [shareWith, setShareWith] = useState<string[]>([]);
  const [formKey, setFormKey] = useState(0);

  const error = upload.error instanceof ApiError ? upload.error : null;

  function toggle(principal: string) {
    setShareWith((current) =>
      current.includes(principal) ? current.filter((p) => p !== principal) : [...current, principal],
    );
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!file) return;
    upload.mutate(
      { file, title, shareWith },
      {
        onSuccess: () => {
          setFile(null);
          setTitle("");
          setShareWith([]);
          setFormKey((k) => k + 1); // clears the file input
        },
      },
    );
  }

  return (
    <Card>
      <h2 className="mb-4 font-semibold">Upload a document</h2>
      <form key={formKey} onSubmit={onSubmit} className="grid gap-4 sm:grid-cols-2">
        <Field label="File" htmlFor="file" hint="PDF, Word (.docx), Markdown, HTML or text, up to 20 MB" error={error?.messageFor("file")}>
          <input
            id="file"
            type="file"
            accept={ACCEPT}
            className="text-sm"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </Field>
        <Field label="Title (optional)" htmlFor="title" error={error?.messageFor("title")}>
          <input id="title" className={inputClass} value={title} onChange={(e) => setTitle(e.target.value)} maxLength={300} />
        </Field>
        <fieldset className="sm:col-span-2">
          <legend className="text-sm font-medium text-slate-700">Share with</legend>
          <p className="mb-2 text-xs text-slate-500">You always keep access. Leave everything unticked to keep it private.</p>
          <div className="flex flex-wrap gap-4">
            {shareTargets.map((p) => (
              <label key={p} className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={shareWith.includes(p)} onChange={() => toggle(p)} />
                {principalLabel(p)}
              </label>
            ))}
          </div>
          {error?.messageFor("share_with") && <p className="mt-1 text-xs text-red-600">{error.messageFor("share_with")}</p>}
        </fieldset>
        <div className="flex items-center gap-3 sm:col-span-2">
          <Button type="submit" disabled={!file || upload.isPending}>
            {upload.isPending ? "Uploading…" : "Upload"}
          </Button>
          {upload.isSuccess && <span className="text-sm text-emerald-700">Uploaded. It becomes searchable in a few seconds.</span>}
          {upload.isError && !error?.fieldErrors.length && <span className="text-sm text-red-700">{upload.error.message}</span>}
        </div>
      </form>
    </Card>
  );
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
