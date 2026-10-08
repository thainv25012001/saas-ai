"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";
import { useMutation, useQuery } from "urql";
import { GmailConnectionCard } from "@/components/supplier-quotes/GmailConnectionCard";
import { MappingReview, type MappingRow } from "@/components/supplier-quotes/MappingReview";
import { QuoteReports } from "@/components/supplier-quotes/QuoteReports";
import { type SupplierDraft, SupplierList } from "@/components/supplier-quotes/SupplierList";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardHeader } from "@/components/ui/Card";
import { PageHeader } from "@/components/ui/PageHeader";
import { LoadingState } from "@/components/ui/Spinner";
import {
  ConfirmSupplierMappingDocument,
  type ConfirmSupplierMappingInput,
  CreateSupplierDocument,
  SupplierQuoteOverviewDocument,
  SupplierReportDocument,
  UpdateSupplierDocument,
} from "@/graphql/generated";
import { API_URL, type ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { firstGraphQLError } from "@/lib/graphql-errors";
import {
  disconnectGmail,
  gmailCallbackNotice,
  reprocessSupplierMessage,
  startGmailConnect,
} from "@/lib/supplier-quotes";

/** A queued re-fetch that failed after its mapping was already saved. The
 * mapping stands; only the queueing needs another try. */
type RequeueFailure = { messageId: string; message: string };

function SupplierQuotesPage() {
  const { user, accessToken, setAccessToken, loading } = useAuth();
  const paused = loading || !user;
  const searchParams = useSearchParams();
  const callbackNotice = gmailCallbackNotice(searchParams.get("gmail"), searchParams.get("reason"));

  const [{ data, fetching, error }, refetchOverview] = useQuery({
    query: SupplierQuoteOverviewDocument,
    pause: paused,
  });
  const refresh = () => refetchOverview({ requestPolicy: "network-only" });

  const [selectedReportId, setSelectedReportId] = useState<string | null>(null);
  const [{ data: reportData, fetching: reportFetching, error: reportError }] = useQuery({
    query: SupplierReportDocument,
    variables: { id: selectedReportId ?? "" },
    pause: paused || !selectedReportId,
  });

  const [createResult, createSupplier] = useMutation(CreateSupplierDocument);
  const [updateResult, updateSupplier] = useMutation(UpdateSupplierDocument);
  const [, confirmMapping] = useMutation(ConfirmSupplierMappingDocument);

  const [gmailBusy, setGmailBusy] = useState(false);
  const [gmailError, setGmailError] = useState<string | null>(null);
  const [supplierError, setSupplierError] = useState<string | null>(null);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [mappingErrors, setMappingErrors] = useState<Record<string, string>>({});
  const [requeue, setRequeue] = useState<RequeueFailure | null>(null);
  const [requeueing, setRequeueing] = useState(false);

  const auth = accessToken ? { accessToken, apiUrl: API_URL, onAccessToken: setAccessToken } : null;
  const canManageGmail = user?.role === "owner";

  const suppliers = useMemo(() => data?.suppliers ?? [], [data]);
  const supplierNames = useMemo(
    () => Object.fromEntries(suppliers.map((supplier) => [supplier.id, supplier.name])),
    [suppliers],
  );

  async function handleConnect() {
    if (!auth) return;
    setGmailError(null);
    setGmailBusy(true);
    try {
      window.location.assign(await startGmailConnect(auth));
    } catch (err) {
      setGmailError((err as ApiError).message);
      setGmailBusy(false);
    }
  }

  async function handleDisconnect() {
    if (!auth) return;
    setGmailError(null);
    setGmailBusy(true);
    try {
      await disconnectGmail(auth);
      refresh();
    } catch (err) {
      setGmailError((err as ApiError).message);
    } finally {
      setGmailBusy(false);
    }
  }

  async function handleSaveSupplier(id: string | null, draft: SupplierDraft) {
    setSupplierError(null);
    const result = id ? await updateSupplier({ id, input: draft }) : await createSupplier({ input: draft });
    if (result.error) {
      setSupplierError(firstGraphQLError(result.error));
      return;
    }
    refresh();
  }

  async function queueReprocess(messageId: string): Promise<boolean> {
    if (!auth) return false;
    try {
      await reprocessSupplierMessage(messageId, auth);
      setRequeue(null);
      return true;
    } catch (err) {
      setRequeue({ messageId, message: (err as ApiError).message });
      return false;
    }
  }

  async function handleConfirm(mapping: MappingRow & { sourceMessageId: string | null }, input: ConfirmSupplierMappingInput) {
    setConfirmingId(mapping.id);
    setMappingErrors((current) => {
      const next = { ...current };
      delete next[mapping.id];
      return next;
    });
    try {
      // The mutation's transaction has committed by the time it answers, so
      // the worker queued next always reads the confirmed mapping.
      const result = await confirmMapping({ id: mapping.id, input });
      if (result.error) {
        setMappingErrors((current) => ({
          ...current,
          [mapping.id]: firstGraphQLError(result.error) ?? "The mapping could not be confirmed.",
        }));
        return;
      }
      if (mapping.sourceMessageId) await queueReprocess(mapping.sourceMessageId);
      refresh();
    } finally {
      setConfirmingId(null);
    }
  }

  async function handleRetryRequeue() {
    if (!requeue) return;
    setRequeueing(true);
    try {
      if (await queueReprocess(requeue.messageId)) refresh();
    } finally {
      setRequeueing(false);
    }
  }

  const queryError = firstGraphQLError(error);
  const mappings = data?.pendingSupplierMappings ?? [];
  const messages = data?.pendingSupplierMessages ?? [];
  const reviewCount = mappings.length + messages.length;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Supplier quotes"
        description="Spreadsheet quotes from your suppliers' emails, compared against your catalogue."
      />

      {callbackNotice ? <Alert tone={callbackNotice.tone}>{callbackNotice.message}</Alert> : null}
      {queryError ? <Alert tone="danger">{queryError}</Alert> : null}

      {fetching && !data ? (
        <LoadingState label="Loading supplier quotes…" />
      ) : data ? (
        <>
          <GmailConnectionCard
            connection={data.gmailConnection}
            canManage={canManageGmail}
            busy={gmailBusy}
            error={gmailError}
            onConnect={handleConnect}
            onDisconnect={handleDisconnect}
          />

          <Card>
            <CardHeader
              title={reviewCount > 0 ? `Needs review (${reviewCount})` : "Needs review"}
              description="Confirm new spreadsheet layouts so their emails can be analysed."
            />
            {requeue ? (
              <div className="px-5 pt-4">
                <Alert tone="warn" title="Mapping saved, but the email was not queued">
                  <p>{requeue.message}</p>
                  <Button
                    className="mt-2"
                    size="sm"
                    variant="secondary"
                    loading={requeueing}
                    loadingLabel="Queueing…"
                    onClick={handleRetryRequeue}
                  >
                    Queue it again
                  </Button>
                </Alert>
              </div>
            ) : null}
            <MappingReview
              mappings={mappings}
              messages={messages}
              supplierNames={supplierNames}
              confirmingId={confirmingId}
              errors={mappingErrors}
              onConfirm={(row, input) => {
                const mapping = mappings.find((candidate) => candidate.id === row.id);
                if (mapping) void handleConfirm(mapping, input);
              }}
            />
          </Card>

          <Card>
            <CardHeader
              title="Reports"
              description="Profit is shown only where a current catalogue sale price exists in the same currency and unit."
            />
            <QuoteReports
              reports={data.supplierReports}
              supplierNames={supplierNames}
              selectedId={selectedReportId}
              onSelect={setSelectedReportId}
              detail={reportData?.supplierReport ?? null}
              detailLoading={reportFetching && !reportData}
              detailError={firstGraphQLError(reportError)}
            />
          </Card>

          <Card>
            <CardHeader
              title="Suppliers"
              description="Emails from these senders are checked for .xlsx quotes. Other mail is ignored."
            />
            <SupplierList
              suppliers={suppliers}
              saving={createResult.fetching || updateResult.fetching}
              error={supplierError}
              onSave={(id, draft) => void handleSaveSupplier(id, draft)}
            />
          </Card>
        </>
      ) : null}
    </div>
  );
}

export default function SupplierQuotesRoute() {
  return (
    <Suspense fallback={<LoadingState label="Loading supplier quotes…" />}>
      <SupplierQuotesPage />
    </Suspense>
  );
}
