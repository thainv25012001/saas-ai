/**
 * The playground's conversation-identity state machine, kept here as a pure
 * function so it can be tested without rendering the page.
 *
 * The bug this exists to prevent: `message_start` is the FIRST event of a
 * turn and carries `conversation_id`, which the page stores immediately. But
 * on a first turn that id only becomes real when the server commits the
 * transaction that created the conversation row. If the user hits Stop (or
 * the connection drops, or the server hits its `internal_error` path), the
 * backend rolls that whole transaction back -- INSERT included -- and the
 * row never existed. The client is then holding a `conversation_id` for a
 * conversation that does not exist, and every subsequent send posts it,
 * gets a pre-stream 404 (`ChatService.send` looks the conversation up before
 * its first yield), and renders an error. The playground wedges permanently:
 * the only escape is "New conversation".
 *
 * `message_end` is the signal that the turn -- and therefore the
 * conversation row -- is durable: `ChatService.send` writes the assistant
 * message and the `usage_events` row before yielding it, and the SSE layer
 * only commits a stream that reached its natural end. So: a turn that never
 * produced a `message_end` leaves the conversation unproven, and if nothing
 * in this conversation has ever been proven, the id is thrown away and the
 * next message starts a fresh conversation server-side.
 *
 * A handled mid-stream provider failure (an in-band `error` event) is
 * actually committed but has no `message_end`, so it is treated as
 * unproven too. That is deliberate: the cost of being wrong in that
 * direction is one lost thread of history, and the cost of being wrong in
 * the other is a playground that cannot send another message at all.
 */

export type TurnState = {
  /** The `conversation_id` to send with the next turn, or `null` to let the
   * server create one. */
  conversationId: string | null;
  /** True once some turn in this conversation has reached `message_end`, so
   * the conversation row is known to be committed. */
  committed: boolean;
};

/** The state a playground session starts in, and returns to on "New
 * conversation" or an agent switch. */
export const NEW_CONVERSATION: TurnState = { conversationId: null, committed: false };

export type TurnResult = {
  /** The id seen during the turn -- from its `message_start`, or the id the
   * turn was sent with if none arrived. */
  conversationId: string | null;
  /** Whether a `message_end` arrived for this turn. */
  sawMessageEnd: boolean;
};

export function resolveTurnOutcome(before: TurnState, turn: TurnResult): TurnState {
  if (turn.sawMessageEnd) {
    return { conversationId: turn.conversationId ?? before.conversationId, committed: true };
  }
  if (before.committed) {
    // An earlier turn in this conversation committed, so the row is durable
    // regardless of how this one ended -- keep the thread.
    return { conversationId: before.conversationId, committed: true };
  }
  // Nothing in this conversation has ever committed and this turn did not
  // either: whatever id we were handed refers to a row that was rolled back.
  return NEW_CONVERSATION;
}
