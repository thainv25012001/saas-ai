/**
 * REST client for `POST /api/v1/products/import` -- see
 * `apps/api/app/api/products.py`. Multipart, so REST rather than GraphQL, for
 * exactly the reason `documents.ts` gives; the import *records* it creates
 * are read back over GraphQL (`productImports`), like documents are.
 */

import { type ApiError, fetchWithRefresh, parseErrorEnvelope } from "./api";
import { type DocumentAuth, formatByteLimit, formatBytes, MAX_UPLOAD_BYTES } from "./documents";

/**
 * `SUPPORTED_IMPORT_MIME_TYPES` in `apps/api/app/products/importer.py`,
 * duplicated for the same reason `ACCEPTED_DOCUMENT_TYPES` is: the picker has
 * to say what it accepts before a file is chosen, and no endpoint exposes it.
 */
export const ACCEPTED_IMPORT_TYPES: Record<string, string> = {
  "text/csv": ".csv",
  "application/json": ".json",
};

export const ACCEPTED_IMPORT_EXTENSIONS = Object.values(ACCEPTED_IMPORT_TYPES);

/**
 * The import route shares `settings.max_request_bytes` with document upload
 * and bounds the whole multipart request the same way, so the usable budget
 * is the same `MAX_UPLOAD_BYTES` -- one number, not a second copy of it.
 */
export const MAX_IMPORT_BYTES = MAX_UPLOAD_BYTES;

/**
 * `resolve_import_mime_type` in the importer, mirrored: a browser that
 * reports nothing useful for `.csv`/`.json` (empty, or octet-stream -- common
 * for `.csv` on Windows without Excel) falls back to the extension. A
 * reported type is otherwise believed, as on the server, with one exception:
 * the types a real `.csv` is reported as (`CSV_ALIAS_MIME_TYPES`) defer to a
 * `.csv` extension. MIME parameters are stripped before comparing.
 */
const GENERIC_MIME_TYPES = new Set(["", "application/octet-stream", "binary/octet-stream"]);

/** `_CSV_ALIAS_MIME_TYPES` on the server. Chromium and Firefox on Windows
 * report a `.csv` as `application/vnd.ms-excel` whenever Excel is installed
 * -- where most catalogues come from -- and other platforms use `text/plain`
 * or an older CSV spelling. */
const CSV_ALIAS_MIME_TYPES = new Set([
  "application/vnd.ms-excel",
  "text/plain",
  "text/x-csv",
  "application/csv",
  "application/x-csv",
  "text/comma-separated-values",
]);

const EXTENSION_MIME_TYPES: Record<string, string> = {
  csv: "text/csv",
  json: "application/json",
};

export function resolveImportType(reported: string, filename?: string): string {
  const base = reported.split(";", 1)[0].trim().toLowerCase();
  const suffixType = filename
    ? EXTENSION_MIME_TYPES[filename.slice(filename.lastIndexOf(".") + 1).toLowerCase()]
    : undefined;
  if (GENERIC_MIME_TYPES.has(base)) return suffixType ?? reported;
  if (CSV_ALIAS_MIME_TYPES.has(base) && suffixType === "text/csv") return suffixType;
  return base;
}

/** The server's two door checks, run client-side so a doomed pick fails
 * instantly -- see `validateDocumentFile`. */
export function validateImportFile(file: { type: string; size: number; name?: string }): ApiError | null {
  const mimeType = resolveImportType(file.type, file.name);
  if (!(mimeType in ACCEPTED_IMPORT_TYPES)) {
    return {
      code: "unsupported_import_type",
      message: `"${mimeType || "unknown"}" is not a supported file type. Accepts ${ACCEPTED_IMPORT_EXTENSIONS.join(", ")}.`,
    };
  }
  if (file.size > MAX_IMPORT_BYTES) {
    return {
      code: "payload_too_large",
      message: `This file is ${formatBytes(file.size)}, over the ${formatByteLimit(MAX_IMPORT_BYTES)} limit.`,
    };
  }
  return null;
}

/** Starts an import and resolves once the server has accepted it (202). The
 * rows land later, in a worker; the caller refetches `productImports` and
 * polls it for the outcome. */
export async function importProducts(file: File, auth: DocumentAuth): Promise<void> {
  const form = new FormData();
  form.append("file", file);
  // No explicit Content-Type -- the browser supplies the multipart boundary.
  const response = await fetchWithRefresh(
    (token) =>
      fetch(`${auth.apiUrl}/api/v1/products/import`, {
        method: "POST",
        body: form,
        headers: { Authorization: `Bearer ${token}` },
      }),
    auth.accessToken,
    auth.onAccessToken,
  );
  if (!response.ok) throw await parseErrorEnvelope(response, "internal_error");
}
