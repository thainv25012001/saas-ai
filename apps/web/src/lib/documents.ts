/**
 * REST client for `POST /api/v1/documents` and
 * `POST /api/v1/documents/{id}/retry` -- see `apps/api/app/api/documents.py`.
 *
 * Not GraphQL: a multipart upload has no good GraphQL story here (see that
 * module's docstring), so this talks straight to the API with a Bearer
 * token, exactly the way `sse.ts` does for the chat stream -- `apiFetch` in
 * `api.ts` is same-origin only and JSON-only, neither of which fits a
 * cross-origin multipart POST.
 */

import { apiFetch, type ApiError } from "./api";
import type { TokenResponse } from "./auth";
import type { DocumentStatus } from "@/graphql/generated";

/**
 * `SUPPORTED_MIME_TYPES` in `apps/api/app/rag/extract.py`, duplicated here
 * rather than fetched: the API has no endpoint that exposes it, and a
 * short, rarely-changing list hardcoded in one place is what lets the
 * picker say what it accepts *before* a file is chosen instead of only
 * after a 422 (requirement 4 of Task 8).
 */
export const ACCEPTED_DOCUMENT_TYPES: Record<string, string> = {
  "text/plain": ".txt",
  "text/markdown": ".md",
  "text/html": ".html",
  "application/pdf": ".pdf",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
};

export const ACCEPTED_DOCUMENT_EXTENSIONS = Object.values(ACCEPTED_DOCUMENT_TYPES);

/**
 * `settings.max_request_bytes` (`apps/api/app/core/config.py`) defaults to
 * 20 MB, and bounds the *whole* multipart request -- boundary markers, the
 * `title` field, part headers -- not just the file bytes. 1 KiB of headroom
 * is far more than a single-file request with a short title ever adds on
 * top of the file itself, and it is a number worth stating plainly rather
 * than something like "20 MB minus 214 bytes".
 */
export const MAX_REQUEST_BYTES = 20 * 1024 * 1024;
export const MAX_UPLOAD_BYTES = MAX_REQUEST_BYTES - 1024;

export function isAcceptedDocumentType(mimeType: string): boolean {
  return mimeType in ACCEPTED_DOCUMENT_TYPES;
}

/** `1536` -> `"1.5 KB"`, `20971520` -> `"20 MB"`. Whole numbers past 10 units
 * skip the decimal -- "20 MB" reads better than "20.0 MB" for a stated limit. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const rounded = value >= 10 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${rounded} ${units[unitIndex]}`;
}

/**
 * The same two checks `upload_document` makes server-side
 * (`SUPPORTED_MIME_TYPES`, `max_request_bytes`), run client-side so a bad
 * pick is rejected instantly with a plain-language reason instead of after
 * a round trip that ends in a 422/413. Not a replacement for the server's
 * checks -- a client can always be bypassed -- just what keeps the honest
 * path from ever needing them.
 */
export function validateDocumentFile(file: { type: string; size: number }): ApiError | null {
  if (!isAcceptedDocumentType(file.type)) {
    return {
      code: "unsupported_document_type",
      message: `"${file.type || "unknown"}" is not a supported file type. Accepts ${ACCEPTED_DOCUMENT_EXTENSIONS.join(", ")}.`,
    };
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return {
      code: "payload_too_large",
      message: `This file is ${formatBytes(file.size)}, over the ${formatBytes(MAX_UPLOAD_BYTES)} limit.`,
    };
  }
  return null;
}

type RawDocumentStatus = "pending" | "processing" | "ready" | "failed";

/** The REST response's `status` is the model's own lowercase `StrEnum`
 * value (`apps/api/app/db/models/document.py`); the GraphQL schema exposes
 * the same states as uppercase enum members. Normalising here means one
 * `DocumentStatus` type and one set of status helpers serve both call
 * paths instead of the UI juggling two casings of the same four states. */
const STATUS_FROM_REST: Record<RawDocumentStatus, DocumentStatus> = {
  pending: "PENDING",
  processing: "PROCESSING",
  ready: "READY",
  failed: "FAILED",
};

export type DocumentUploadResponse = {
  id: string;
  title: string;
  status: DocumentStatus;
  error: string | null;
};

function toDocumentUploadResponse(raw: unknown): DocumentUploadResponse {
  const record = (raw ?? {}) as Record<string, unknown>;
  const rawStatus = typeof record.status === "string" ? record.status : "";
  return {
    id: String(record.id ?? ""),
    title: typeof record.title === "string" ? record.title : "",
    // An unrecognised value is treated as `pending` rather than throwing --
    // this is the tail of a 202 response we already committed to acting on
    // (a document row now exists either way), so the safer failure mode is
    // a status the poll loop will keep refreshing, not a crashed upload
    // handler.
    status: STATUS_FROM_REST[rawStatus as RawDocumentStatus] ?? "PENDING",
    error: typeof record.error === "string" ? record.error : null,
  };
}

async function parseApiError(response: Response): Promise<ApiError> {
  const body: unknown = await response.json().catch(() => null);
  const error =
    body && typeof body === "object" && "error" in body
      ? (body as { error?: unknown }).error
      : null;
  if (error && typeof error === "object" && "code" in error && "message" in error) {
    const { code, message } = error as { code: unknown; message: unknown };
    return {
      code: typeof code === "string" ? code : "internal_error",
      message: typeof message === "string" ? message : "Something went wrong. Please try again.",
    };
  }
  return { code: "internal_error", message: "Something went wrong. Please try again." };
}

export type DocumentAuth = {
  accessToken: string;
  apiUrl: string;
  /** Pushes a rotated token back into React state after a silent refresh --
   * same contract as `StreamChatParams.onAccessToken` in `sse.ts`. */
  onAccessToken?: (token: string) => void;
};

/**
 * One request with a Bearer token, refreshed and retried exactly once on a
 * 401. The dashboard's GraphQL traffic already recovers from an expired
 * access token silently via urql's `authExchange`; without this, uploading
 * or retrying a document from a tab left open past the token's lifetime
 * would be the one action on this page that fails instead.
 */
async function authorizedFetch(
  url: string,
  init: RequestInit,
  auth: DocumentAuth,
): Promise<Response> {
  const send = (token: string) =>
    fetch(url, {
      ...init,
      headers: { ...(init.headers ?? {}), Authorization: `Bearer ${token}` },
    });

  let response = await send(auth.accessToken);
  if (response.status === 401) {
    let refreshed: string | null = null;
    try {
      const tokens = await apiFetch<TokenResponse>("/api/v1/auth/refresh", { method: "POST" });
      refreshed = tokens.access_token;
    } catch {
      refreshed = null;
    }
    if (refreshed !== null) {
      auth.onAccessToken?.(refreshed);
      response = await send(refreshed);
    }
  }
  return response;
}

export async function uploadDocument(
  file: File,
  title: string,
  auth: DocumentAuth,
): Promise<DocumentUploadResponse> {
  const form = new FormData();
  form.append("file", file);
  if (title.trim()) form.append("title", title.trim());

  // No explicit Content-Type: the browser sets `multipart/form-data` with
  // its own boundary from the FormData body, and overriding it here would
  // strip that boundary and break parsing server-side.
  const response = await authorizedFetch(
    `${auth.apiUrl}/api/v1/documents`,
    { method: "POST", body: form },
    auth,
  );

  if (!response.ok) throw await parseApiError(response);
  return toDocumentUploadResponse(await response.json());
}

export async function retryDocument(
  documentId: string,
  auth: DocumentAuth,
): Promise<DocumentUploadResponse> {
  const response = await authorizedFetch(
    `${auth.apiUrl}/api/v1/documents/${documentId}/retry`,
    { method: "POST" },
    auth,
  );

  if (!response.ok) throw await parseApiError(response);
  return toDocumentUploadResponse(await response.json());
}
