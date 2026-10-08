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
  ConfirmSupplierQuoteMatchDocument,
  CreateSupplierDocument,
  SupplierQuoteOverviewDocument,
  SupplierReportDocument,
  UpdateSupplierDocument,
} from "@/graphql/generated";
import { API_URL, type ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { firstGraphQLError } from "@/lib/graphql-errors";
import {
  type ConfirmMappingBody,
  confirmMapping,
  disconnectGmail,
  gmailCallbackNotice,
  reprocessSupplierMessage,
  startGmailConnect,
} from "@/lib/supplier-quotes";

/** A confirmed mapping whose email could not be queued for its re-fetch.
 * The confirmation stands; only the queueing needs another try. */
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
  const [{ data: reportData, fetching: reportFetching, error: reportError }, refetchReport] = useQuery({
    query: SupplierReportDocument,
    variables: { id: selectedReportId ?? "" },
    pause: paused || !selectedReportId,
  });

  const [createResult, createSupplier] = useMutation(CreateSupplierDocument);
  const [updateResult, updateSupplier] = useMutation(UpdateSupplierDocument);
  const [, confirmQuoteMatch] = useMutation(ConfirmSupplierQuoteMatchDocument);

  const [gmailBusy, setGmailBusy] = useState(false);
  const [gmailError, setGmailError] = useState<string | null>(null);
  const [supplierError, setSupplierError] = useState<string | null>(null);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [mappingErrors, setMappingErrors] = useState<Record<string, string>>({});
  const [requeue, setRequeue] = useState<RequeueFailure | null>(null);
  const [requeueing, setRequeueing] = useState(false);
  const [confirmingQuoteId, setConfirmingQuoteId] = useState<string | null>(null);
  const [matchError, setMatchError] = useState<string | null>(null);

  const auth = accessToken ? { accessToken, apiUrl: API_URL, onAccessToken: setAccessToken } : null;
  // Gmail, sender rules, layouts and matches are the owner's to change; the
  // API refuses anyone else, so for a member the controls are not shown.
  const canManage = user?.role === "owner";

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

  async function handleConfirm(mapping: MappingRow & { sourceMessageId: string | null }, body: ConfirmMappingBody) {
    if (!auth) return;
    setConfirmingId(mapping.id);
    setMappingErrors((current) => {
      const next = { ...current };
      delete next[mapping.id];
      return next;
    });
    try {
      // One request: the API commits the confirmation, then queues the
      // email's re-fetch, so the worker never reads it unconfirmed.
      const { queued } = await confirmMapping(mapping.id, body, auth);
      if (!queued && mapping.sourceMessageId) {
        setRequeue({
          messageId: mapping.sourceMessageId,
          message: "The processing queue was unavailable.",
        });
      }
      refresh();
    } catch (err) {
      setMappingErrors((current) => ({
        ...current,
        [mapping.id]: (err as ApiError).message ?? "The mapping could not be confirmed.",
      }));
    } finally {
      setConfirmingId(null);
    }
  }

  async function handleConfirmMatch(quoteId: string, productId: string) {
    setMatchError(null);
    setConfirmingQuoteId(quoteId);
    try {
      const result = await confirmQuoteMatch({ quoteId, productId });
      if (result.error) {
        setMatchError(firstGraphQLError(result.error) ?? "The match could not be confirmed.");
        return;
      }
      refetchReport({ requestPolicy: "network-only" });
      refresh();
    } finally {
      setConfirmingQuoteId(null);
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
            canManage={canManage}
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
              canManage={canManage}
              onConfirm={(row, body) => {
                const mapping = mappings.find((candidate) => candidate.id === row.id);
                if (mapping) void handleConfirm(mapping, body);
              }}
            />
          </Card>

          <Card>
            <CardHeader
              title="Reports"
              description="Profit is shown only where a current catalogue sale price exists in the same currency and unit."
            />
            {matchError ? (
              <div className="px-5 pt-4">
                <Alert tone="danger">{matchError}</Alert>
              </div>
            ) : null}
            <QuoteReports
              reports={data.supplierReports}
              supplierNames={supplierNames}
              selectedId={selectedReportId}
              onSelect={setSelectedReportId}
              detail={reportData?.supplierReport ?? null}
              detailLoading={reportFetching && !reportData}
              detailError={firstGraphQLError(reportError)}
              canManage={canManage}
              confirmingQuoteId={confirmingQuoteId}
              onConfirmMatch={(quoteId, productId) => void handleConfirmMatch(quoteId, productId)}
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
              canManage={canManage}
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
