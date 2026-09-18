"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "urql";
import { Alert } from "@/components/ui/Alert";
import { Card } from "@/components/ui/Card";
import { PageHeader } from "@/components/ui/PageHeader";
import { LoadingState } from "@/components/ui/Spinner";
import { DocumentsTable } from "@/components/knowledge/DocumentsTable";
import { UploadDropzone } from "@/components/knowledge/UploadDropzone";
import { DeleteDocumentDocument, DocumentsDocument } from "@/graphql/generated";
import { API_URL, type ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { retryDocument, uploadDocument, validateDocumentFile } from "@/lib/documents";
import { useDocumentPolling } from "@/lib/use-document-polling";
import { firstGraphQLError } from "@/lib/graphql-errors";

/** Tracks page visibility so the poll effect can stop while no one is
 * looking at this tab -- see `shouldPollDocuments`. Kept as its own hook
 * (rather than inlined) so the effect below only has to reason about one
 * boolean, not the listener wiring. */
function useTabHidden(): boolean {
  const [hidden, setHidden] = useState(() => typeof document !== "undefined" && document.hidden);
  useEffect(() => {
    function onVisibilityChange() {
      setHidden(document.hidden);
    }
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, []);
  return hidden;
}

export default function KnowledgePage() {
  const { user, accessToken, setAccessToken, loading } = useAuth();
  const [{ data, fetching, error }, refetchDocuments] = useQuery({
    query: DocumentsDocument,
    pause: loading || !user,
  });
  const [, runDeleteDocument] = useMutation(DeleteDocumentDocument);

  // Memoised because the poll effect below depends on it: `?? []` is a new
  // array on every render, which would tear down and recreate the interval
  // every render instead of only when the data or the tab's visibility
  // actually changes.
  const documents = useMemo(() => data?.documents ?? [], [data]);
  const tabHidden = useTabHidden();

  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<ApiError | null>(null);
  const [retryingId, setRetryingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // Memoised for the same reason `documents` is above: `useDocumentPolling`
  // depends on this by reference, and a fresh closure every render would
  // tear down and recreate the interval every render instead of only when
  // `refetchDocuments` itself changes (which urql keeps stable).
  const pollDocuments = useCallback(() => {
    refetchDocuments({ requestPolicy: "network-only" });
  }, [refetchDocuments]);

  // Polls the whole list -- not one row at a time -- while anything is
  // still pending/processing, and stops the moment either every row has
  // settled or the tab goes into the background. The interval-lifecycle
  // mechanism itself (not just the predicate deciding when to run) has its
  // own test in `use-document-polling.test.ts`.
  useDocumentPolling(documents, tabHidden, pollDocuments);

  async function handleUpload(file: File) {
    if (!accessToken) return;
    // Client-side re-check of the same two rules the server enforces
    // (`validateDocumentFile`'s docstring) -- this is what makes a bad pick
    // fail instantly instead of after a round trip.
    const validationError = validateDocumentFile(file);
    if (validationError) {
      setUploadError(validationError);
      return;
    }
    setUploadError(null);
    setUploading(true);
    try {
      // Re-uploading identical bytes returns the existing document and
      // enqueues nothing (server-side dedup by checksum) -- indistinguishable
      // here from a fresh upload, and correctly so: either way the row the
      // refetch below shows is the one now backing that content.
      await uploadDocument(file, "", { accessToken, apiUrl: API_URL, onAccessToken: setAccessToken });
      await refetchDocuments({ requestPolicy: "network-only" });
    } catch (err) {
      setUploadError(err as ApiError);
    } finally {
      setUploading(false);
    }
  }

  async function handleRetry(id: string) {
    if (!accessToken) return;
    setActionError(null);
    setRetryingId(id);
    try {
      await retryDocument(id, { accessToken, apiUrl: API_URL, onAccessToken: setAccessToken });
      await refetchDocuments({ requestPolicy: "network-only" });
    } catch (err) {
      setActionError((err as ApiError).message);
    } finally {
      setRetryingId(null);
    }
  }

  async function handleDelete(id: string, title: string) {
    if (!window.confirm(`Delete "${title}"? This cannot be undone.`)) return;
    setActionError(null);
    setDeletingId(id);
    try {
      const result = await runDeleteDocument({ id });
      if (result.error) {
        setActionError(firstGraphQLError(result.error));
      } else {
        await refetchDocuments({ requestPolicy: "network-only" });
      }
    } finally {
      setDeletingId(null);
    }
  }

  const queryError = firstGraphQLError(error);

  return (
    <div className="space-y-4">
      <PageHeader title="Knowledge" description="The documents your assistant answers from." />

      <Card>
        <div className="p-5">
          <UploadDropzone uploading={uploading} error={uploadError} onUpload={handleUpload} />
        </div>
      </Card>

      {queryError ? <Alert tone="danger">{queryError}</Alert> : null}
      {actionError ? <Alert tone="danger">{actionError}</Alert> : null}

      <Card>
        {fetching && !data ? (
          <LoadingState label="Loading documents…" />
        ) : queryError ? null : (
          <DocumentsTable
            documents={documents.map((doc) => ({
              id: String(doc.id),
              title: doc.title,
              status: doc.status,
              chunkCount: doc.chunkCount,
              error: doc.error,
              createdAt: String(doc.createdAt),
            }))}
            retryingId={retryingId}
            deletingId={deletingId}
            onRetry={handleRetry}
            onDelete={handleDelete}
          />
        )}
      </Card>
    </div>
  );
}
