/** Shapes of the JSON the API returns (see backend/app/schemas.py). */

export type Me = {
  sub: string;
  username: string;
  name: string;
  roles: string[];
  groups: string[];
  principals: string[];
  can_upload: boolean;
  is_admin: boolean;
  share_targets: string[];
};

export type DocumentStatus = "PENDING" | "PROCESSING" | "READY" | "FAILED";

export type KnowledgeDocument = {
  id: string;
  title: string;
  filename: string;
  file_type: "pdf" | "docx" | "markdown" | "html" | "text";
  size_bytes: number;
  status: DocumentStatus;
  error: string | null;
  chunk_count: number;
  owner_name: string;
  is_owner: boolean;
  can_delete: boolean;
  shared_with: string[];
  created_at: string;
  updated_at: string;
};

export type AuditEvent = {
  id: string;
  occurred_at: string;
  actor_name: string;
  action: string;
  document_ids: string[];
  details: Record<string, unknown>;
};

/** A passage the answer may cite (the "sources" server-sent event). */
export type Source = {
  index: number;
  chunk_id: string;
  document_id: string;
  document_title: string;
  heading: string | null;
  page: number | null;
  text: string;
  score: number;
};

/** "group:/engineering/platform" -> "engineering / platform", "everyone" -> "Everyone". */
export function principalLabel(principal: string): string {
  if (principal === "everyone") return "Everyone";
  if (principal.startsWith("group:/")) return principal.slice("group:/".length).split("/").join(" / ");
  if (principal.startsWith("user:")) return "a specific user";
  return principal;
}
