import { describe, expect, it } from "vitest";
import { conversationLabel, toTranscript } from "./conversation-transcript";

const USER = {
  id: "m1",
  seq: 1,
  role: "USER" as const,
  content: "how much is the starter plan?",
  model: null,
  inputTokens: null,
  outputTokens: null,
  costUsd: null,
  latencyMs: null,
  error: null,
  citations: [],
};

const ASSISTANT = {
  id: "m2",
  seq: 2,
  role: "ASSISTANT" as const,
  content: "Forty dollars a month.",
  model: "gpt-4o-mini",
  inputTokens: 412,
  outputTokens: 88,
  costUsd: "0.00123",
  latencyMs: 1840,
  error: null,
  citations: [],
};

describe("toTranscript", () => {
  it("renders a stored exchange the way a streamed one looks", () => {
    const [question, answer] = toTranscript([USER, ASSISTANT]);

    expect(question).toMatchObject({ role: "user", text: USER.content, status: "done" });
    expect(answer).toMatchObject({ role: "assistant", text: ASSISTANT.content, status: "done" });
  });

  it("carries model, tokens, cost and latency onto the assistant message", () => {
    // These are what the per-turn chips and the session total are computed
    // from. Dropping them makes a reopened conversation claim it cost nothing.
    const [, answer] = toTranscript([USER, ASSISTANT]);

    expect(answer.meta).toEqual({
      model: "gpt-4o-mini",
      usage: { input_tokens: 412, output_tokens: 88 },
      costUsd: "0.00123",
      latencyMs: 1840,
    });
  });

  it("gives a turn with no usage recorded no meta at all", () => {
    // A failed turn has no token counts. Inventing zeroes would report a real
    // conversation as having used no tokens rather than as unmeasured.
    const [, answer] = toTranscript([
      USER,
      { ...ASSISTANT, inputTokens: null, outputTokens: null, model: null },
    ]);

    expect(answer.meta).toBeUndefined();
  });

  it("renders a failed turn as an error, not as a blank message", () => {
    const [answer] = toTranscript([
      { ...ASSISTANT, content: null, error: "the provider is unavailable" },
    ]);

    expect(answer.status).toBe("error");
    expect(answer.text).toBe("");
    expect(answer.error).toEqual({ code: "error", message: "the provider is unavailable" });
  });

  it("keeps the partial text of a turn that failed after some of it arrived", () => {
    // The API persists what streamed before the failure precisely so history
    // agrees with what the user watched happen.
    const [answer] = toTranscript([
      { ...ASSISTANT, content: "Forty doll", error: "the provider is unavailable" },
    ]);

    expect(answer.status).toBe("error");
    expect(answer.text).toBe("Forty doll");
  });

  it("converts citations to the shape the transcript already renders", () => {
    const [answer] = toTranscript([
      {
        ...ASSISTANT,
        citations: [
          {
            chunkId: "c1",
            documentId: "d1",
            productId: null,
            documentTitle: "Pricing",
            excerpt: "…",
            rank: 1,
            score: 0.91,
          },
        ],
      },
    ]);

    expect(answer.citations).toEqual([
      {
        chunkId: "c1",
        documentId: "d1",
        productId: null,
        documentTitle: "Pricing",
        excerpt: "…",
        rank: 1,
        score: 0.91,
        page: null,
      },
    ]);
  });

  it("keeps a product citation's product id, with no chunk or document", () => {
    // A reloaded conversation must still show a product source (final
    // review I1), and several product citations must not collapse onto one
    // shared empty-string id.
    const [answer] = toTranscript([
      {
        ...ASSISTANT,
        citations: [
          {
            chunkId: null,
            documentId: null,
            productId: "p1",
            documentTitle: "Aurora Sedan",
            excerpt: "28499.00 USD · in_stock",
            rank: 1,
            score: 0,
          },
          {
            chunkId: null,
            documentId: null,
            productId: "p2",
            documentTitle: "Borealis SUV",
            excerpt: "35999.00 USD · in_stock",
            rank: 2,
            score: 0,
          },
        ],
      },
    ]);

    expect(answer.citations?.map((c) => [c.chunkId, c.documentId, c.productId])).toEqual([
      [null, null, "p1"],
      [null, null, "p2"],
    ]);
  });

  it("leaves citations undefined when a turn was not grounded", () => {
    // `undefined` and `[]` mean different things to the transcript: no
    // retrieval happened, versus retrieval found nothing.
    const [answer] = toTranscript([ASSISTANT]);

    expect(answer.citations).toBeUndefined();
  });

  it("skips system and tool turns, which have no rendering here", () => {
    const transcript = toTranscript([
      { ...USER, role: "SYSTEM" as const },
      USER,
      { ...USER, id: "m3", role: "TOOL" as const },
    ]);

    expect(transcript.map((m) => m.id)).toEqual(["m1"]);
  });
});

describe("conversationLabel", () => {
  it("uses the title once the job has written one", () => {
    expect(conversationLabel({ title: "Starter plan pricing", preview: "how much?" })).toBe(
      "Starter plan pricing",
    );
  });

  it("falls back to the first question while the title is still in flight", () => {
    // Titling is a background job, so a conversation you just started has
    // title: null for a few seconds -- which is exactly when you are looking
    // at it.
    expect(conversationLabel({ title: null, preview: "how much is the starter plan?" })).toBe(
      "how much is the starter plan?",
    );
  });

  it("never returns an empty string", () => {
    expect(conversationLabel({ title: null, preview: null })).not.toBe("");
    expect(conversationLabel({ title: "   ", preview: null })).not.toBe("");
  });
});
