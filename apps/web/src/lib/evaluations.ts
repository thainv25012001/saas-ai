/**
 * Pure helpers for the Evaluations dashboard (docs/PHASE-6.md §5, §6), plus
 * the one REST call it makes.
 *
 * The lookups (run status, score, comparison state) follow the
 * `document-status.ts` shape: one mapping, read by every component that shows
 * the value, so the table, the summary and the poll predicate cannot disagree.
 */

import type { BadgeTone } from "@/components/ui/Badge";
import type { EvaluationRunStatus } from "@/graphql/generated";
import { type ApiError, fetchWithRefresh, parseErrorEnvelope } from "./api";
import type { DocumentAuth } from "./documents";

// --- Runs --------------------------------------------------------------

/** A run can still change while it is queued or running; every other status
 * is terminal. */
export function isRunActive(status: EvaluationRunStatus): boolean {
  return status === "PENDING" || status === "RUNNING";
}

/** `shouldPollImports`' counterpart for one run: poll while it can still
 * move, and never from a hidden tab. */
export function shouldPollRun(status: EvaluationRunStatus | undefined, tabHidden: boolean): boolean {
  if (tabHidden || status === undefined) return false;
  return isRunActive(status);
}

export function runStatusLabel(status: EvaluationRunStatus): string {
  switch (status) {
    case "PENDING":
      return "Queued";
    case "RUNNING":
      return "Running";
    case "COMPLETED":
      return "Completed";
    case "FAILED":
      return "Failed";
    case "CANCELLED":
      return "Cancelled";
  }
}

/** `COMPLETED` is success because the run finished, not because its cases
 * passed -- the pass rate beside it says how well. `CANCELLED` is neutral:
 * someone chose it, nothing broke. */
export function runStatusTone(status: EvaluationRunStatus): BadgeTone {
  switch (status) {
    case "PENDING":
      return "neutral";
    case "RUNNING":
      return "info";
    case "COMPLETED":
      return "success";
    case "FAILED":
      return "danger";
    case "CANCELLED":
      return "neutral";
  }
}

/** `0.85` -> `85%`, `0.855` -> `85.5%`; `null` (no case scored) -> `—`. */
export function formatPassRate(rate: number | null | undefined): string {
  if (rate === null || rate === undefined || Number.isNaN(rate)) return "—";
  return `${Math.round(rate * 1000) / 10}%`;
}

/** The LLM calls a run makes. `turns` is a floor: one agent turn per case,
 * and a turn that calls tools makes more than one model call. `judgeCalls`
 * is a ceiling: with a judge, one call per case that has a reference answer
 * -- the only cases it grades -- and none for a turn that errored. The form
 * words each side accordingly. */
export function runCallEstimate(
  cases: readonly CaseExpectations[],
  withJudge: boolean,
): { turns: number; judgeCalls: number } {
  return {
    turns: cases.length,
    judgeCalls: withJudge ? cases.filter((c) => Boolean(c.referenceAnswer?.trim())).length : 0,
  };
}

// --- Scores ------------------------------------------------------------

const SCORER_LABELS: Record<string, string> = {
  required_phrases: "Required phrases",
  tool_selection: "Tool selection",
  document_recall: "Document recall",
  product_recall: "Product recall",
  judge: "Judge",
};

/** Display name for a scorer key; an unknown key the API adds later still
 * renders, as itself. */
export function scorerLabel(name: string): string {
  return SCORER_LABELS[name] ?? name;
}

export type ScoreLike = { name: string; score: number | null; passed: boolean; status: string };

/** A scorer that could not score (`status: "error"` -- a judge that failed
 * or returned garbage) is `warn`, not `danger`: nothing measured the answer
 * as wrong, the measurement itself did not happen. It still fails the case,
 * and the case's own Fail badge says so. */
export function scoreTone(score: ScoreLike): BadgeTone {
  if (score.status === "error") return "warn";
  return score.passed ? "success" : "danger";
}

export function scoreBadgeLabel(score: ScoreLike): string {
  const label = scorerLabel(score.name);
  if (score.status === "error") return `${label}: error`;
  return `${label} ${formatPassRate(score.score)}`;
}

/** A score's `detail` crosses the wire as JSON text (its shape differs per
 * scorer). `null` when it is not a JSON object -- the caller then shows
 * nothing rather than guessing. */
export function parseScoreDetail(detail: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(detail);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

/** A tool call's `arguments`, JSON text, as the object `ToolCall` renders.
 * Anything that is not a JSON object is kept whole under `value` rather than
 * dropped. */
export function parseToolArguments(text: string): Record<string, unknown> {
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
    return { value: parsed };
  } catch {
    return { value: text };
  }
}

// --- Comparing runs (docs/PHASE-6.md §6) --------------------------------

/** `error` is optional so a caller comparing only pass/fail can omit it;
 * the run page passes it. */
export type ResultLike = { caseId: string | null; question: string; passed: boolean; error?: string | null };

/** `errored`: the candidate's turn errored, so it measured nothing -- neither
 * a regression nor an improvement, whatever the base did. */
export type ComparisonState = "regressed" | "improved" | "errored" | "unchanged" | "new";

export type Comparison<C extends ResultLike = ResultLike, B extends ResultLike = C> = {
  candidate: C;
  /** The matching result in the base run; `null` for a `new` case. */
  base: B | null;
  state: ComparisonState;
};

/**
 * Case by case, every candidate result against the base run, in the
 * candidate's order.
 *
 * Matched on `caseId`. A result whose case has since been deleted has
 * `caseId === null`, so it falls back to matching on `question` -- the one
 * thing the result keeps of the case. A candidate whose case still exists
 * also falls back to the question if the base has no result for that id but
 * holds an orphaned (null-id) result asking the same thing.
 *
 * A candidate whose turn errored is `errored`, matched or not.
 *
 * A case in the base but not the candidate (removed since) has no row to
 * annotate and is not reported.
 */
export function compareRuns<B extends ResultLike, C extends ResultLike>(
  base: readonly B[],
  candidate: readonly C[],
): Comparison<C, B>[] {
  const byCase = new Map<string, B>();
  const byQuestion = new Map<string, B>();
  const orphanByQuestion = new Map<string, B>();
  for (const result of base) {
    if (result.caseId !== null && !byCase.has(result.caseId)) byCase.set(result.caseId, result);
    if (!byQuestion.has(result.question)) byQuestion.set(result.question, result);
    if (result.caseId === null && !orphanByQuestion.has(result.question)) {
      orphanByQuestion.set(result.question, result);
    }
  }

  return candidate.map((result) => {
    const match =
      result.caseId !== null
        ? (byCase.get(result.caseId) ?? orphanByQuestion.get(result.question) ?? null)
        : (byQuestion.get(result.question) ?? null);
    return { candidate: result, base: match, state: comparisonState(match, result) };
  });
}

function comparisonState(base: ResultLike | null, candidate: ResultLike): ComparisonState {
  if (candidate.error !== null && candidate.error !== undefined) return "errored";
  if (base === null) return "new";
  if (base.passed && !candidate.passed) return "regressed";
  if (!base.passed && candidate.passed) return "improved";
  return "unchanged";
}

export function comparisonCounts(comparisons: readonly { state: ComparisonState }[]): Record<ComparisonState, number> {
  const counts: Record<ComparisonState, number> = { regressed: 0, improved: 0, errored: 0, unchanged: 0, new: 0 };
  for (const { state } of comparisons) counts[state] += 1;
  return counts;
}

export function comparisonLabel(state: ComparisonState): string {
  switch (state) {
    case "regressed":
      return "Regressed";
    case "improved":
      return "Improved";
    case "errored":
      return "Errored";
    case "unchanged":
      return "Unchanged";
    case "new":
      return "New";
  }
}

/** `unchanged` earns no colour, so a scan picks out the rows that moved.
 * `errored` is `warn`, like a scorer that could not score: nothing measured
 * the answer as worse, the measurement did not happen. */
export function comparisonTone(state: ComparisonState): BadgeTone {
  switch (state) {
    case "regressed":
      return "danger";
    case "improved":
      return "success";
    case "errored":
      return "warn";
    case "unchanged":
      return "neutral";
    case "new":
      return "info";
  }
}

// --- Cases -------------------------------------------------------------

/** The four builtin tools a case may expect -- `KNOWN_BUILTIN_TOOL_NAMES` in
 * `apps/api/app/evaluations/schemas.py`, duplicated because no endpoint lists
 * them and the checkboxes have to render before anything is typed. */
export const BUILTIN_TOOL_NAMES = [
  "retrieve_knowledge",
  "search_products",
  "get_product",
  "create_lead",
] as const;

export type CaseExpectations = {
  referenceAnswer: string | null;
  requiredPhrases: readonly string[];
  expectedToolNames: readonly string[];
  expectedDocumentIds: readonly string[];
  expectedProductIds: readonly string[];
};

/** The server's "at least one expectation" rule (`CaseInput`), mirrored so
 * the honest path never waits on a 422. Tags are labels, not expectations. */
export function hasExpectation(expectations: CaseExpectations): boolean {
  return (
    Boolean(expectations.referenceAnswer?.trim()) ||
    expectations.requiredPhrases.some((phrase) => phrase.trim() !== "") ||
    expectations.expectedToolNames.length > 0 ||
    expectations.expectedDocumentIds.length > 0 ||
    expectations.expectedProductIds.length > 0
  );
}

/** A case only a judge can grade: none of the deterministic expectations
 * (phrases, tools, documents, products). `CaseInput` guarantees every case
 * has at least one expectation, so this is a reference-answer-only case. */
function isReferenceOnly(expectations: CaseExpectations): boolean {
  return (
    !expectations.requiredPhrases.some((phrase) => phrase.trim() !== "") &&
    expectations.expectedToolNames.length === 0 &&
    expectations.expectedDocumentIds.length === 0 &&
    expectations.expectedProductIds.length === 0
  );
}

export function referenceOnlyCount(cases: readonly CaseExpectations[]): number {
  return cases.filter(isReferenceOnly).length;
}

/** The server's 422 for a judge-less start (`reference_only_message` in
 * `apps/api/app/evaluations/service.py`), word for word, so the form says
 * exactly what the API would. */
export function referenceOnlyMessage(count: number): string {
  const cases = count === 1 ? "1 case has" : `${count} cases have`;
  return `${cases} only a reference answer; add a judge or a deterministic expectation`;
}

/** One phrase (or tag) per line; blank lines dropped, each trimmed, repeats
 * collapsed -- what the server would store anyway. */
export function splitLines(text: string): string[] {
  return [...new Set(text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean))];
}

// --- Starting a run (REST) ----------------------------------------------

/** The body of `POST /api/v1/evaluations/runs` -- `StartRunInput` in
 * `apps/api/app/evaluations/schemas.py`. Each optional pair is both or
 * neither; `buildStartRunPayload` is what guarantees that. */
export type StartRunPayload = {
  dataset_id: string;
  agent_id: string;
  prompt_version_id?: string;
  provider?: string;
  model?: string;
  judge_provider?: string;
  judge_model?: string;
};

export type ModelPair = { provider: string; model: string };

export function buildStartRunPayload({
  datasetId,
  agentId,
  promptVersionId,
  override,
  judge,
}: {
  datasetId: string;
  agentId: string;
  promptVersionId: string | null;
  override: ModelPair | null;
  judge: ModelPair | null;
}): StartRunPayload {
  const payload: StartRunPayload = { dataset_id: datasetId, agent_id: agentId };
  if (promptVersionId) payload.prompt_version_id = promptVersionId;
  if (override && override.provider && override.model) {
    payload.provider = override.provider;
    payload.model = override.model;
  }
  if (judge && judge.provider && judge.model) {
    payload.judge_provider = judge.provider;
    payload.judge_model = judge.model;
  }
  return payload;
}

/** Starts a run and resolves with its id once the server has accepted it
 * (202). REST rather than GraphQL for the commit-then-enqueue reason in
 * docs/PHASE-6.md §7. Rejects with the API's error envelope -- a `conflict`
 * when a run of this dataset is already queued or running. */
export async function startEvaluationRun(payload: StartRunPayload, auth: DocumentAuth): Promise<{ id: string }> {
  const response = await fetchWithRefresh(
    (token) =>
      fetch(`${auth.apiUrl}/api/v1/evaluations/runs`, {
        method: "POST",
        body: JSON.stringify(payload),
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      }),
    auth.accessToken,
    auth.onAccessToken,
  );
  if (!response.ok) throw await parseErrorEnvelope(response, "internal_error");
  const body = (await response.json()) as { id: string };
  return { id: String(body.id) };
}

export type { ApiError };
