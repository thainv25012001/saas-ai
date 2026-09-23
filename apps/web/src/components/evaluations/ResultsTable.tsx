"use client";

import { Fragment, useState } from "react";
import { ToolCall } from "@/components/chat/ToolCall";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { cn, focusRing } from "@/components/ui/cn";
import { EmptyState } from "@/components/ui/EmptyState";
import { Icon } from "@/components/ui/icons";
import { formatLatency, formatUsd } from "@/lib/format";
import {
  type ComparisonState,
  comparisonCounts,
  comparisonLabel,
  comparisonTone,
  parseScoreDetail,
  parseToolArguments,
  scoreBadgeLabel,
  scorerLabel,
  scoreTone,
} from "@/lib/evaluations";

export type ResultScore = { name: string; score: number | null; passed: boolean; status: string; detail: string };
export type ResultToolCall = { name: string; arguments: string; isError: boolean; excerpt: string };

export type ResultRow = {
  id: string;
  caseId: string | null;
  question: string;
  answer: string | null;
  error: string | null;
  passed: boolean;
  citedDocumentIds: readonly string[];
  citedProductIds: readonly string[];
  latencyMs: number | null;
  costUsd: string | null;
  scores: readonly ResultScore[];
  toolCalls: readonly ResultToolCall[];
};

const COMPARISON_ORDER: ComparisonState[] = ["regressed", "improved", "unchanged", "new"];

const checkboxClasses = cn("size-4 rounded-control accent-primary", focusRing, "focus-visible:ring-offset-1");

/**
 * One row per case of a run. A row expands to what the model actually did:
 * its answer, the error, each scorer's detail (the judge's rationale, the
 * phrases or tools that were missing), the tool calls and what it cited.
 *
 * Almost everything here is untrusted: the question and reference came from
 * the customer, and the answer, the rationale and the tool arguments were
 * written by a model that may be quoting a document or a visitor. All of it
 * is a JSX text child -- no `dangerouslySetInnerHTML`, no markdown pass, so
 * `**bold**` stays two asterisks and `<script>` stays characters.
 *
 * With `comparison` (result id -> state, from `compareRuns`) each row gets a
 * regressed/improved/unchanged/new badge, the counts sit above the table,
 * and a filter narrows it to the regressions.
 */
export function ResultsTable({
  results,
  comparison = null,
  documentTitles = {},
}: {
  results: readonly ResultRow[];
  comparison?: Readonly<Record<string, ComparisonState>> | null;
  documentTitles?: Readonly<Record<string, string>>;
}) {
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [onlyRegressions, setOnlyRegressions] = useState(false);

  if (results.length === 0) {
    return (
      <EmptyState
        icon="evaluation"
        title="No results yet"
        description="Each case's result appears here as soon as it has been scored."
      />
    );
  }

  const counts = comparison
    ? comparisonCounts(results.map((result) => ({ state: comparison[result.id] ?? "new" })))
    : null;
  const shown =
    comparison && onlyRegressions ? results.filter((result) => comparison[result.id] === "regressed") : results;

  function toggle(id: string) {
    const next = new Set(expanded);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setExpanded(next);
  }

  return (
    <div>
      {counts ? (
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-5 py-3">
          <ul className="flex flex-wrap gap-2" aria-label="Compared with the other run">
            {COMPARISON_ORDER.map((state) => (
              <li key={state}>
                <Badge tone={counts[state] > 0 ? comparisonTone(state) : "neutral"}>
                  {comparisonLabel(state)}: {counts[state]}
                </Badge>
              </li>
            ))}
          </ul>
          <label className="flex items-center gap-2 text-sm text-ink">
            <input
              type="checkbox"
              className={checkboxClasses}
              checked={onlyRegressions}
              onChange={(event) => setOnlyRegressions(event.target.checked)}
            />
            Show only regressions
          </label>
        </div>
      ) : null}

      {shown.length === 0 ? (
        <p className="px-5 py-6 text-sm text-ink-muted">No case regressed.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-line bg-surface-muted text-xs uppercase tracking-wide text-ink-subtle">
              <tr>
                <th scope="col" className="px-5 py-2.5 font-medium">
                  Question
                </th>
                <th scope="col" className="px-5 py-2.5 font-medium">
                  Result
                </th>
                <th scope="col" className="px-5 py-2.5 font-medium">
                  Scores
                </th>
                <th scope="col" className="px-5 py-2.5 font-medium">
                  Latency
                </th>
                <th scope="col" className="px-5 py-2.5 font-medium">
                  Cost
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {shown.map((result) => {
                const open = expanded.has(result.id);
                const state = comparison ? (comparison[result.id] ?? "new") : null;
                const detailsId = `result-${result.id}-details`;
                return (
                  <Fragment key={result.id}>
                    <tr className="align-top">
                      <td className="max-w-md px-5 py-3">
                        <button
                          type="button"
                          aria-expanded={open}
                          aria-controls={detailsId}
                          onClick={() => toggle(result.id)}
                          className={cn(
                            "flex w-full items-start gap-1.5 rounded-control text-left text-ink hover:underline",
                            focusRing,
                            "focus-visible:ring-offset-2",
                          )}
                        >
                          <Icon
                            name={open ? "chevronDown" : "chevronRight"}
                            size="md"
                            className="mt-0.5 text-ink-subtle"
                          />
                          <span className="line-clamp-2 whitespace-pre-wrap">{result.question}</span>
                        </button>
                      </td>
                      <td className="whitespace-nowrap px-5 py-3">
                        <div className="flex flex-wrap gap-1.5">
                          {result.passed ? <Badge tone="success">Pass</Badge> : <Badge tone="danger">Fail</Badge>}
                          {state ? <Badge tone={comparisonTone(state)}>{comparisonLabel(state)}</Badge> : null}
                        </div>
                      </td>
                      <td className="px-5 py-3">
                        <div className="flex flex-wrap gap-1.5">
                          {result.error ? <Badge tone="danger">Errored</Badge> : null}
                          {result.scores.map((score) => (
                            <Badge key={score.name} tone={scoreTone(score)}>
                              {scoreBadgeLabel(score)}
                            </Badge>
                          ))}
                        </div>
                      </td>
                      <td className="whitespace-nowrap px-5 py-3 text-ink-muted">{formatLatency(result.latencyMs)}</td>
                      <td className="whitespace-nowrap px-5 py-3 text-ink-muted">{formatUsd(result.costUsd)}</td>
                    </tr>
                    {open ? (
                      <tr id={detailsId} className="bg-canvas">
                        <td colSpan={5} className="px-5 py-4">
                          <ResultDetails result={result} documentTitles={documentTitles} />
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-1.5">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-subtle">{title}</h3>
      {children}
    </section>
  );
}

/** A detail value as one line of text, whatever JSON it arrived as. */
function detailText(value: unknown): string {
  if (Array.isArray(value)) return value.map((item) => String(item)).join(", ");
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

const DETAIL_LABELS: Record<string, string> = {
  missing: "Missing",
  unexpected: "Also called",
  correctness: "Verdict",
  grounded: "Grounded in what it saw",
  rationale: "Rationale",
  error: "Error",
};

function ScoreDetail({ score }: { score: ResultScore }) {
  const detail = parseScoreDetail(score.detail);
  const entries = detail
    ? Object.entries(detail).filter(([, value]) => !(Array.isArray(value) && value.length === 0))
    : [];
  return (
    <div className="rounded-control border border-line bg-surface px-3 py-2 text-xs">
      <div className="flex items-center gap-2">
        <span className="font-medium text-ink">{scorerLabel(score.name)}</span>
        <Badge tone={scoreTone(score)}>{scoreBadgeLabel(score)}</Badge>
      </div>
      {entries.length > 0 ? (
        <dl className="mt-1.5 space-y-1">
          {entries.map(([key, value]) => (
            <div key={key} className="flex flex-wrap gap-1.5">
              <dt className="text-ink-subtle">{DETAIL_LABELS[key] ?? key}:</dt>
              <dd className="whitespace-pre-wrap break-words text-ink">
                {typeof value === "boolean" ? (value ? "Yes" : "No") : detailText(value)}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}
    </div>
  );
}

function ResultDetails({
  result,
  documentTitles,
}: {
  result: ResultRow;
  documentTitles: Readonly<Record<string, string>>;
}) {
  return (
    <div className="space-y-4">
      {result.error ? <Alert tone="danger" title="The turn errored">{result.error}</Alert> : null}

      <Section title="Answer">
        {result.answer ? (
          <p className="whitespace-pre-wrap break-words text-sm text-ink">{result.answer}</p>
        ) : (
          <p className="text-sm text-ink-subtle">No answer.</p>
        )}
      </Section>

      {result.scores.length > 0 ? (
        <Section title="Scores">
          <div className="grid gap-2 md:grid-cols-2">
            {result.scores.map((score) => (
              <ScoreDetail key={score.name} score={score} />
            ))}
          </div>
        </Section>
      ) : null}

      <Section title="Tool calls">
        {result.toolCalls.length === 0 ? (
          <p className="text-sm text-ink-subtle">No tools were called.</p>
        ) : (
          <div className="space-y-1.5">
            {result.toolCalls.map((call, index) => (
              <ToolCall
                key={index}
                data={{
                  id: String(index),
                  name: call.name,
                  arguments: parseToolArguments(call.arguments),
                  status: "done",
                  result: call.excerpt,
                  isError: call.isError,
                }}
              />
            ))}
          </div>
        )}
      </Section>

      <Section title="Cited">
        {result.citedDocumentIds.length === 0 && result.citedProductIds.length === 0 ? (
          <p className="text-sm text-ink-subtle">Nothing was cited.</p>
        ) : (
          <ul className="space-y-0.5 text-sm">
            {result.citedDocumentIds.map((id) => (
              <li key={`d-${id}`} className="text-ink">
                <span className="text-ink-subtle">Document: </span>
                {documentTitles[id] ?? <span className="font-mono text-xs">{id}</span>}
              </li>
            ))}
            {result.citedProductIds.map((id) => (
              <li key={`p-${id}`} className="text-ink">
                <span className="text-ink-subtle">Product: </span>
                <span className="font-mono text-xs">{id}</span>
              </li>
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}
