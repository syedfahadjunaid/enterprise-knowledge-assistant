import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiFetch } from "./client";
import type { AuditEvent, KnowledgeDocument, Me } from "./types";

/** The signed-in user as the API sees them (roles and groups from the verified token). */
export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => apiFetch<Me>("/api/me"), staleTime: 5 * 60_000 });
}

const inProgress = (d: KnowledgeDocument) => d.status === "PENDING" || d.status === "PROCESSING";

export function useDocuments() {
  return useQuery({
    queryKey: ["documents"],
    queryFn: () => apiFetch<KnowledgeDocument[]>("/api/documents"),
    // While something is being ingested, poll so the status flips to READY on its own.
    refetchInterval: (query) => (query.state.data?.some(inProgress) ? 2000 : false),
  });
}

export type UploadInput = { file: File; title: string; shareWith: string[] };

export function useUploadDocument() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ file, title, shareWith }: UploadInput) => {
      const form = new FormData();
      form.append("file", file);
      if (title.trim()) form.append("title", title.trim());
      for (const principal of shareWith) form.append("share_with", principal);
      return apiFetch<KnowledgeDocument>("/api/documents", { method: "POST", body: form });
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["documents"] }),
  });
}

export function useDeleteDocument() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiFetch<void>(`/api/documents/${encodeURIComponent(id)}`, { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["documents"] }),
  });
}

export function useAuditLog(enabled: boolean) {
  return useQuery({
    queryKey: ["audit"],
    queryFn: () => apiFetch<AuditEvent[]>("/api/audit?limit=100"),
    enabled,
  });
}
