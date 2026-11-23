import { signIn, userManager } from "../auth/userManager";

export type InvalidField = { field: string; message: string };

/** An API call that failed. Carries what the UI needs to explain it to the user. */
export class ApiError extends Error {
  readonly status: number;
  readonly fieldErrors: InvalidField[];
  readonly retryAfterSeconds?: number;

  constructor(status: number, message: string, fieldErrors: InvalidField[] = [], retryAfterSeconds?: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.fieldErrors = fieldErrors;
    this.retryAfterSeconds = retryAfterSeconds;
  }

  messageFor(field: string): string | undefined {
    return this.fieldErrors.find((e) => e.field === field)?.message;
  }
}

type ProblemDetail = { title?: string; detail?: string; errors?: InvalidField[] };

type RequestOptions = {
  method?: "GET" | "POST" | "DELETE";
  /** A plain object is sent as JSON; FormData as multipart (file uploads). */
  body?: unknown;
  signal?: AbortSignal;
  accept?: string;
};

/** The current access token, or a redirect to sign-in if the session is gone. */
export async function accessToken(): Promise<string> {
  const user = await userManager.getUser();
  if (!user || user.expired) {
    await signIn();
    throw new ApiError(401, "Your session has expired. Signing you in again…");
  }
  return user.access_token;
}

/**
 * fetch() for the API: adds the access token and turns RFC 9457 problem responses into
 * {@link ApiError}s. Returns the raw Response (callers that stream read the body themselves).
 */
export async function apiRequest(
  path: string,
  { method = "GET", body, signal, accept = "application/json" }: RequestOptions = {},
): Promise<Response> {
  const isForm = body instanceof FormData;
  const response = await fetch(path, {
    method,
    signal,
    headers: {
      Accept: accept,
      Authorization: `Bearer ${await accessToken()}`,
      // For FormData the browser sets the multipart boundary itself.
      ...(body === undefined || isForm ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : isForm ? body : JSON.stringify(body),
  });

  if (response.status === 401) {
    await signIn();
    throw new ApiError(401, "Your session has expired. Signing you in again…");
  }
  if (!response.ok) {
    throw await toApiError(response);
  }
  return response;
}

/** JSON request/response helper used by the TanStack Query hooks. */
export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await apiRequest(path, options);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function toApiError(response: Response): Promise<ApiError> {
  let problem: ProblemDetail = {};
  try {
    problem = (await response.json()) as ProblemDetail;
  } catch {
    // Some errors (e.g. from a proxy) have no JSON body.
  }
  const retryAfter = Number(response.headers.get("Retry-After")) || undefined;
  return new ApiError(
    response.status,
    problem.detail ?? defaultMessage(response.status),
    Array.isArray(problem.errors) ? problem.errors : [],
    retryAfter,
  );
}

function defaultMessage(status: number): string {
  if (status === 403) return "You don't have permission to do that.";
  if (status === 404) return "That item doesn't exist.";
  if (status === 429) return "Too many requests. Please wait a moment.";
  if (status >= 500) return "The server had a problem. Please try again.";
  return `Request failed (${status}).`;
}
