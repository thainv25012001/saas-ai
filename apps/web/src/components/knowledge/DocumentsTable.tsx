"use client";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { formatTimestamp } from "@/lib/format";
import { Icon } from "@/components/ui/icons";
import type { DocumentStatus } from "@/graphql/generated";
import { canRetryDocument, documentStatusLabel, documentStatusTone } from "@/lib/document-status";

export type DocumentRow = {
  id: string;
  title: string;
  status: DocumentStatus;
  chunkCount: number;
  error: string | null;
  createdAt: string;
};

export type DocumentsTableProps = {
  documents: readonly DocumentRow[];
  /** The id currently mid-retry or mid-delete, so only that row's button
   * shows a loading state instead of freezing the whole table. */
  retryingId?: string | null;
  deletingId?: string | null;
  onRetry: (id: string) => void;
  onDelete: (id: string, title: string) => void;
};

/**
 * The list itself: status, chunk count, upload time, and the two actions a
 * row can offer. Presentational -- the page owns the GraphQL query, the
 * poll timer and the REST calls; this owns rendering one screenful of rows
 * and asking for a row's id back through `onRetry`/`onDelete`. Kept
 * separate from `page.tsx` specifically so it is testable with plain props,
 * the same split `CreateAgentForm` uses for the same reason.
 */
export function DocumentsTable({
  documents,
  retryingId = null,
  deletingId = null,
  onRetry,
  onDelete,
}: DocumentsTableProps) {
  if (documents.length === 0) {
    return (
      <EmptyState
        icon="knowledge"
        title="No documents yet"
        description="Upload a file above and your assistant will answer from it, with citations, once it finishes processing."
      />
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-line bg-surface-muted text-xs uppercase tracking-wide text-ink-subtle">
          <tr>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Title
            </th>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Status
            </th>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Chunks
            </th>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Uploaded
            </th>
            <th scope="col" className="px-5 py-2.5 font-medium">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {documents.map((doc) => {
            const retryable = canRetryDocument(doc.status);
            const isRetrying = retryingId === doc.id;
            const isDeleting = deletingId === doc.id;
            return (
              <tr key={doc.id}>
                <td className="max-w-sm px-5 py-3 font-medium text-ink">
                  {/* Untrusted text (the file's own name/title): rendered as
                    * a plain string child, never dangerouslySetInnerHTML or
                    * a markdown pass -- React escapes it by construction. */}
                  <span className="block truncate" title={doc.title}>
                    {doc.title}
                  </span>
                  {doc.status === "FAILED" && doc.error ? (
                    <Alert tone="danger" className="mt-1.5">
                      {doc.error}
                    </Alert>
                  ) : null}
                </td>
                <td className="px-5 py-3">
                  <Badge tone={documentStatusTone(doc.status, doc.chunkCount)}>
                    {documentStatusLabel(doc.status, doc.chunkCount)}
                  </Badge>
                </td>
                <td className="px-5 py-3 text-ink-muted">{doc.chunkCount}</td>
                <td className="px-5 py-3 text-ink-muted">{formatTimestamp(doc.createdAt)}</td>
                <td className="px-5 py-3 text-right">
                  <div className="flex justify-end gap-2">
                    {retryable ? (
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => onRetry(doc.id)}
                        loading={isRetrying}
                        loadingLabel="Retrying…"
                      >
                        <Icon name="retry" size="sm" />
                        Retry
                      </Button>
                    ) : null}
                    <Button
                      variant="danger"
                      size="sm"
                      onClick={() => onDelete(doc.id, doc.title)}
                      loading={isDeleting}
                      loadingLabel="Deleting…"
                    >
                      <Icon name="trash" size="sm" />
                      Delete
                    </Button>
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
