import { describe, expect, it } from "vitest";
import { NEW_CONVERSATION, resolveTurnOutcome, type TurnState } from "./chat-turn";

describe("resolveTurnOutcome", () => {
  it("clears the conversation id when the first turn ends without a message_end", () => {
    // Stop pressed (or the connection dropped) on the very first turn: the
    // backend rolled back the transaction that created the conversation, so
    // the id from `message_start` points at a row that does not exist.
    // Keeping it wedges every subsequent send on a pre-stream 404.
    const after = resolveTurnOutcome(NEW_CONVERSATION, {
      conversationId: "conv-1",
      sawMessageEnd: false,
    });

    expect(after.conversationId).toBeNull();
    expect(after.committed).toBe(false);
  });

  it("keeps the conversation id when the first turn completes", () => {
    const after = resolveTurnOutcome(NEW_CONVERSATION, {
      conversationId: "conv-1",
      sawMessageEnd: true,
    });

    expect(after.conversationId).toBe("conv-1");
    expect(after.committed).toBe(true);
  });

  it("keeps the conversation id when a later turn is aborted after a completed first", () => {
    // The conversation row was committed by the first turn, so it is durable
    // no matter how this one ends -- throwing the id away here would
    // needlessly split one thread into two.
    const committed = resolveTurnOutcome(NEW_CONVERSATION, {
      conversationId: "conv-1",
      sawMessageEnd: true,
    });

    const after = resolveTurnOutcome(committed, {
      conversationId: "conv-1",
      sawMessageEnd: false,
    });

    expect(after.conversationId).toBe("conv-1");
    expect(after.committed).toBe(true);
  });

  it("clears the id when the first turn fails before any message_start arrives", () => {
    // A pre-stream error (a 404, a 500, a network failure) produces no
    // `message_start` at all, so the turn carries the id it was sent with --
    // `null` for a first turn. Nothing to keep, and nothing to wedge on.
    const after = resolveTurnOutcome(NEW_CONVERSATION, {
      conversationId: null,
      sawMessageEnd: false,
    });

    expect(after).toEqual(NEW_CONVERSATION);
  });

  it("does not mutate the state it is given", () => {
    const before: TurnState = { conversationId: "conv-1", committed: false };

    resolveTurnOutcome(before, { conversationId: "conv-1", sawMessageEnd: false });

    expect(before).toEqual({ conversationId: "conv-1", committed: false });
  });
});
