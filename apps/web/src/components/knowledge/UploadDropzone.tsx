"use client";

import { useRef, useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { focusRing, cn } from "@/components/ui/cn";
import { Icon } from "@/components/ui/icons";
import { LoadingState } from "@/components/ui/Spinner";
import type { ApiError } from "@/lib/api";
import { ACCEPTED_DOCUMENT_TYPES, formatByteLimit, MAX_UPLOAD_BYTES } from "@/lib/documents";

export type UploadDropzoneProps = {
  uploading?: boolean;
  error?: ApiError | null;
  onUpload: (file: File) => void;
  /** Mime type -> extension. Defaults to the document types; the Products
   * page passes the catalogue import's `.csv`/`.json`. */
  acceptedTypes?: Record<string, string>;
  /** The usable budget to state up front. */
  maxBytes?: number;
  /** The hidden file input's accessible name. */
  pickLabel?: string;
  /** The line under the zone. Defaults to the Knowledge page's note on how
   * matching works; another page says what its own files must contain. */
  note?: React.ReactNode;
};

// Requirement 3 (Phase 3): the default embedder's honesty belongs where the
// user uploads, not only in docs/PHASE-3.md §2.3. Framed as a fact about
// matching, not a warning -- nothing here is broken.
const LEXICAL_NOTE = (
  <>
    Matching today is by shared wording, not meaning — “car” and “automobile” will not match each
    other yet.
  </>
);

/**
 * Drag-and-drop plus a plain file picker, both funnelling into the same
 * `onUpload`. The accepted types and the size limit are shown as static
 * help text rendered on every render of this component -- before any file
 * is ever chosen -- rather than surfacing only once a pick is rejected.
 * That ordering is requirement 4 of Task 8: a 422/413 after the fact is a
 * worse experience than not attempting a doomed upload at all.
 *
 * Presentational only: the page owns the actual REST call and its
 * fetching/error state, this owns the pointer/keyboard interaction. Same
 * split as `CreateAgentForm`.
 */
export function UploadDropzone({
  uploading = false,
  error = null,
  onUpload,
  acceptedTypes = ACCEPTED_DOCUMENT_TYPES,
  maxBytes = MAX_UPLOAD_BYTES,
  pickLabel = "Choose a document to upload",
  note = LEXICAL_NOTE,
}: UploadDropzoneProps) {
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  function pick(file: File | undefined | null) {
    if (!file || uploading) return;
    onUpload(file);
  }

  return (
    <div className="space-y-2">
      {error ? <Alert tone="danger">{error.message}</Alert> : null}

      <div
        role="button"
        tabIndex={0}
        aria-disabled={uploading}
        onClick={() => inputRef.current?.click()}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragOver={(event) => {
          event.preventDefault();
          if (!uploading) setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragOver(false);
          pick(event.dataTransfer.files[0]);
        }}
        className={cn(
          "flex cursor-pointer flex-col items-center gap-2 rounded-card border-2 border-dashed px-6 py-8 text-center transition-colors",
          dragOver ? "border-line-strong bg-surface-muted" : "border-line",
          uploading && "pointer-events-none opacity-60",
          focusRing,
        )}
      >
        {uploading ? (
          <LoadingState label="Uploading…" />
        ) : (
          <>
            <Icon name="upload" size="lg" className="text-ink-subtle" />
            <p className="text-sm font-medium text-ink">
              Drag a file here, or <span className="underline">browse</span>
            </p>
            {/* Requirement 4: stated up front, not learned from a rejection. */}
            <p className="text-xs text-ink-subtle">
              Accepts {Object.values(acceptedTypes).join(", ")} — up to {formatByteLimit(maxBytes)}.
            </p>
          </>
        )}
        <input
          ref={inputRef}
          type="file"
          className="sr-only"
          aria-label={pickLabel}
          // Types and extensions both: a browser filters by whichever it
          // recognises, and `.csv` on a machine with no mime association
          // is only matched by its extension.
          accept={[...Object.keys(acceptedTypes), ...Object.values(acceptedTypes)].join(",")}
          disabled={uploading}
          onChange={(event) => {
            pick(event.target.files?.[0]);
            // Cleared so picking the same file again (e.g. after fixing a
            // rejected one is not what happened here, but after a retry
            // flow) still fires onChange -- a browser does not fire it a
            // second time for an unchanged value.
            event.target.value = "";
          }}
        />
      </div>

      <p className="text-xs text-ink-subtle">{note}</p>
    </div>
  );
}
