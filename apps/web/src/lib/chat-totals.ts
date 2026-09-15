/** The metadata a completed assistant turn carries (from the `message_end`
 * SSE event). Structural on purpose, so this module does not import a
 * component's props type. */
export type TurnMeta = {
  usage: { input_tokens: number; output_tokens: number };
  costUsd: string | null;
};

export type SessionTotals = {
  inputTokens: number;
  outputTokens: number;
  costUsd: number;
  pricedTurns: number;
  /** Turns whose model is not in the pricing table. Never folded into `costUsd`. */
  unpricedTurns: number;
};

export function sessionTotals(turns: readonly TurnMeta[]): SessionTotals {
  return turns.reduce<SessionTotals>(
    (totals, turn) => {
      const cost = turn.costUsd === null ? Number.NaN : Number(turn.costUsd);
      const priced = Number.isFinite(cost);
      return {
        inputTokens: totals.inputTokens + turn.usage.input_tokens,
        outputTokens: totals.outputTokens + turn.usage.output_tokens,
        // An unpriced turn adds nothing to the total and is counted separately:
        // reporting it as $0 would describe a cheap session, not an unknown one.
        costUsd: priced ? totals.costUsd + cost : totals.costUsd,
        pricedTurns: totals.pricedTurns + (priced ? 1 : 0),
        unpricedTurns: totals.unpricedTurns + (priced ? 0 : 1),
      };
    },
    { inputTokens: 0, outputTokens: 0, costUsd: 0, pricedTurns: 0, unpricedTurns: 0 },
  );
}
