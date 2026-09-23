// @vitest-environment happy-dom
import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { EvaluationRunStatus } from "@/graphql/generated";
import {
  buildStartRunPayload,
  compareRuns,
  comparisonCounts,
  formatPassRate,
  hasExpectation,
  isRunActive,
  parseScoreDetail,
  parseToolArguments,
  referenceOnlyCount,
  referenceOnlyMessage,
  runCallEstimate,
  scoreBadgeLabel,
  scoreTone,
  shouldPollRun,
  splitLines,
  startEvaluationRun,
} from "./evaluations";
import { POLL_INTERVAL_MS, usePollWhile } from "./use-document-polling";

const r = (caseId: string | null, question: string, passed: boolean) => ({ caseId, question, passed });

describe("compareRuns", () => {
  it("labels regressed, improved, unchanged and new cases", () => {
    const base = [r("a", "A?", true), r("b", "B?", false), r("c", "C?", true)];
    const candidate = [r("a", "A?", false), r("b", "B?", true), r("c", "C?", true), r("d", "D?", false)];
    expect(compareRuns(base, candidate).map((c) => [c.candidate.caseId, c.state])).toEqual([
      ["a", "regressed"],
      ["b", "improved"],
      ["c", "unchanged"],
      ["d", "new"],
    ]);
  });

  it("treats two failures as unchanged, not regressed", () => {
    expect(compareRuns([r("a", "A?", false)], [r("a", "A?", false)])[0].state).toBe("unchanged");
  });

  it("matches a result whose case was deleted (caseId null) by its question", () => {
    const base = [r("a", "What is the warranty?", true)];
    const candidate = [r(null, "What is the warranty?", false), r(null, "Never asked before?", true)];
    const [matched, unmatched] = compareRuns(base, candidate);
    expect(matched.state).toBe("regressed");
    expect(matched.base?.caseId).toBe("a");
    expect(unmatched.state).toBe("new");
  });

  it("falls back to an orphaned base result with the same question", () => {
    const [comparison] = compareRuns([r(null, "A?", false)], [r("a", "A?", true)]);
    expect(comparison.state).toBe("improved");
  });

  it("does not match on question when both sides still have a case id", () => {
    // Two different cases can ask the same question; the id is what counts.
    const [comparison] = compareRuns([r("a", "Same?", true)], [r("b", "Same?", false)]);
    expect(comparison.state).toBe("new");
  });

  it("counts each state", () => {
    const comparisons = compareRuns([r("a", "A?", true)], [r("a", "A?", false), r("b", "B?", true)]);
    expect(comparisonCounts(comparisons)).toEqual({ regressed: 1, improved: 0, errored: 0, unchanged: 0, new: 1 });
  });

  it("marks a candidate whose turn errored as errored, never regressed or improved", () => {
    const errored = (caseId: string, question: string) => ({ caseId, question, passed: false, error: "boom" });
    const comparisons = compareRuns(
      [r("a", "A?", true), r("b", "B?", false)],
      [errored("a", "A?"), errored("b", "B?"), errored("c", "C?")],
    );
    expect(comparisons.map((c) => c.state)).toEqual(["errored", "errored", "errored"]);
    expect(comparisonCounts(comparisons)).toEqual({ regressed: 0, improved: 0, errored: 3, unchanged: 0, new: 0 });
  });

  it("still compares a candidate whose error is null", () => {
    const [comparison] = compareRuns([r("a", "A?", true)], [{ ...r("a", "A?", false), error: null }]);
    expect(comparison.state).toBe("regressed");
  });
});

describe("formatPassRate", () => {
  it("formats a fraction as a percentage", () => {
    expect(formatPassRate(0.85)).toBe("85%");
    expect(formatPassRate(1)).toBe("100%");
    expect(formatPassRate(0.855)).toBe("85.5%");
  });

  it("shows a dash when nothing was scored", () => {
    expect(formatPassRate(null)).toBe("—");
    expect(formatPassRate(undefined)).toBe("—");
  });
});

describe("run status", () => {
  it("is active only while queued or running", () => {
    const statuses: EvaluationRunStatus[] = ["PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"];
    expect(statuses.filter(isRunActive)).toEqual(["PENDING", "RUNNING"]);
  });

  it("polls only an active run in a visible tab", () => {
    expect(shouldPollRun("RUNNING", false)).toBe(true);
    expect(shouldPollRun("RUNNING", true)).toBe(false);
    expect(shouldPollRun("COMPLETED", false)).toBe(false);
    expect(shouldPollRun(undefined, false)).toBe(false);
  });
});

describe("run polling", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("stops polling once the run reaches a terminal status", () => {
    vi.useFakeTimers();
    const onPoll = vi.fn();
    const { rerender } = renderHook(
      ({ status }: { status: EvaluationRunStatus }) => usePollWhile(shouldPollRun(status, false), onPoll),
      { initialProps: { status: "RUNNING" as EvaluationRunStatus } },
    );
    vi.advanceTimersByTime(POLL_INTERVAL_MS * 2);
    expect(onPoll).toHaveBeenCalledTimes(2);

    rerender({ status: "COMPLETED" });
    vi.advanceTimersByTime(POLL_INTERVAL_MS * 5);
    expect(onPoll).toHaveBeenCalledTimes(2);
  });
});

const expectations = (overrides: Partial<Parameters<typeof referenceOnlyCount>[0][number]> = {}) => ({
  referenceAnswer: null,
  requiredPhrases: [],
  expectedToolNames: [],
  expectedDocumentIds: [],
  expectedProductIds: [],
  ...overrides,
});

describe("runCallEstimate", () => {
  it("counts one agent turn per case and a judge call only per case with a reference answer", () => {
    const cases = [
      expectations({ referenceAnswer: "30 days" }),
      expectations({ requiredPhrases: ["free"] }),
      expectations({ referenceAnswer: "Blue", requiredPhrases: ["blue"] }),
    ];
    expect(runCallEstimate(cases, false)).toEqual({ turns: 3, judgeCalls: 0 });
    expect(runCallEstimate(cases, true)).toEqual({ turns: 3, judgeCalls: 2 });
  });

  it("does not count a blank reference answer as one the judge grades", () => {
    expect(runCallEstimate([expectations({ referenceAnswer: "  ", requiredPhrases: ["x"] })], true)).toEqual({
      turns: 1,
      judgeCalls: 0,
    });
  });
});

describe("reference-only cases", () => {
  it("counts cases with no deterministic expectation", () => {
    const cases = [
      expectations({ referenceAnswer: "30 days" }),
      expectations({ referenceAnswer: "Two years" }),
      expectations({ referenceAnswer: "Blue", expectedToolNames: ["retrieve_knowledge"] }),
      expectations({ expectedDocumentIds: ["d1"] }),
      expectations({ expectedProductIds: ["p1"] }),
    ];
    expect(referenceOnlyCount(cases)).toBe(2);
  });

  it("says what the server says", () => {
    expect(referenceOnlyMessage(2)).toBe(
      "2 cases have only a reference answer; add a judge or a deterministic expectation",
    );
    expect(referenceOnlyMessage(1)).toBe(
      "1 case has only a reference answer; add a judge or a deterministic expectation",
    );
  });
});

describe("scores", () => {
  it("labels a scored, a failed and an errored score", () => {
    const ok = { name: "tool_selection", score: 1, passed: true, status: "scored" };
    const bad = { name: "required_phrases", score: 0.5, passed: false, status: "scored" };
    const broken = { name: "judge", score: null, passed: false, status: "error" };
    expect([scoreBadgeLabel(ok), scoreTone(ok)]).toEqual(["Tool selection 100%", "success"]);
    expect([scoreBadgeLabel(bad), scoreTone(bad)]).toEqual(["Required phrases 50%", "danger"]);
    expect([scoreBadgeLabel(broken), scoreTone(broken)]).toEqual(["Judge: error", "warn"]);
  });

  it("parses a detail object and refuses anything else", () => {
    expect(parseScoreDetail('{"missing":["a"]}')).toEqual({ missing: ["a"] });
    expect(parseScoreDetail("[1]")).toBeNull();
    expect(parseScoreDetail("not json")).toBeNull();
  });

  it("keeps tool arguments that are not a JSON object instead of dropping them", () => {
    expect(parseToolArguments('{"query":"x"}')).toEqual({ query: "x" });
    expect(parseToolArguments("oops")).toEqual({ value: "oops" });
    expect(parseToolArguments("[1,2]")).toEqual({ value: [1, 2] });
  });
});

describe("cases", () => {
  const none = {
    referenceAnswer: null,
    requiredPhrases: [],
    expectedToolNames: [],
    expectedDocumentIds: [],
    expectedProductIds: [],
  };

  it("requires at least one expectation, and a blank one does not count", () => {
    expect(hasExpectation(none)).toBe(false);
    expect(hasExpectation({ ...none, referenceAnswer: "   " })).toBe(false);
    expect(hasExpectation({ ...none, requiredPhrases: [" "] })).toBe(false);
    expect(hasExpectation({ ...none, referenceAnswer: "Yes" })).toBe(true);
    expect(hasExpectation({ ...none, expectedToolNames: ["search_products"] })).toBe(true);
    expect(hasExpectation({ ...none, expectedProductIds: ["p1"] })).toBe(true);
  });

  it("splits one entry per line, trimmed, without blanks or repeats", () => {
    expect(splitLines(" 3 years \r\n\nfree shipping\n3 years")).toEqual(["3 years", "free shipping"]);
  });
});

describe("buildStartRunPayload", () => {
  it("sends the pinned version and only complete pairs", () => {
    expect(
      buildStartRunPayload({
        datasetId: "d",
        agentId: "a",
        promptVersionId: "v2",
        override: { provider: "openai", model: "" },
        judge: { provider: "anthropic", model: "claude" },
      }),
    ).toEqual({
      dataset_id: "d",
      agent_id: "a",
      prompt_version_id: "v2",
      judge_provider: "anthropic",
      judge_model: "claude",
    });
  });
});

describe("startEvaluationRun", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts JSON with a Bearer token and returns the run id", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ id: "run-1" }), { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);

    const run = await startEvaluationRun(
      { dataset_id: "d", agent_id: "a" },
      { accessToken: "tok", apiUrl: "http://api" },
    );

    expect(run).toEqual({ id: "run-1" });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://api/api/v1/evaluations/runs");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ dataset_id: "d", agent_id: "a" });
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok");
  });

  it("rejects with the API's error envelope on a conflict", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ error: { code: "conflict", message: "a run is already active" } }), {
            status: 409,
          }),
      ),
    );
    await expect(
      startEvaluationRun({ dataset_id: "d", agent_id: "a" }, { accessToken: "tok", apiUrl: "http://api" }),
    ).rejects.toEqual({ code: "conflict", message: "a run is already active" });
  });
});
