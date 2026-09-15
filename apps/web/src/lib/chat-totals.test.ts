import { describe, expect, it } from "vitest";
import { sessionTotals, type TurnMeta } from "./chat-totals";

const turn = (input: number, output: number, costUsd: string | null): TurnMeta => ({
  usage: { input_tokens: input, output_tokens: output },
  costUsd,
});

describe("sessionTotals", () => {
  it("is all zeroes for an empty transcript", () => {
    expect(sessionTotals([])).toEqual({
      inputTokens: 0,
      outputTokens: 0,
      costUsd: 0,
      pricedTurns: 0,
      unpricedTurns: 0,
    });
  });

  it("sums tokens and cost across turns", () => {
    const totals = sessionTotals([turn(10, 20, "0.0012"), turn(5, 7, "0.0003")]);
    expect(totals.inputTokens).toBe(15);
    expect(totals.outputTokens).toBe(27);
    expect(totals.costUsd).toBeCloseTo(0.0015, 10);
    expect(totals.pricedTurns).toBe(2);
    expect(totals.unpricedTurns).toBe(0);
  });

  it("counts an unpriced turn instead of treating it as free", () => {
    // cost_usd is null when the model is not in the pricing table. Adding it
    // as zero would report a cheap session rather than an unknown one.
    const totals = sessionTotals([turn(10, 20, "0.0012"), turn(1, 1, null)]);
    expect(totals.costUsd).toBeCloseTo(0.0012, 10);
    expect(totals.pricedTurns).toBe(1);
    expect(totals.unpricedTurns).toBe(1);
    expect(totals.inputTokens).toBe(11);
  });

  it("treats an unparseable cost as unpriced rather than as NaN", () => {
    const totals = sessionTotals([turn(1, 1, "not-a-number")]);
    expect(totals.costUsd).toBe(0);
    expect(totals.unpricedTurns).toBe(1);
  });
});
