/**
 * Turning a stored conversation back into the transcript the playground
 * renders.
 *
 * The target shape is `ChatMessageData` -- the same type a streamed turn
 * produces -- so a reopened conversation looks identical to one that just
 * arrived, cost chips and citations included, with nothing in `ChatMessage`
 * needing to know which it is looking at.
 */

import type { ChatMessageData } from "@/components/chat/ChatMessage";

/** The message fields the `Conversation` query asks for. Declared
 * structurally rather than imported from the generated types: the generated
 * `role` is an enum and `id` is `unknown`, neither of which a test can
 * construct cheaply, and this module only ever reads these fields. */
export type StoredMessage = {
  id: unknown;
  seq: number;
  role: string;
  content: string | null;
  model: string | null;
  inputTokens: number | null;
  outputTokens: number | null;
  costUsd: string | null;
  latencyMs: number | null;
  error: string | null;
  citations: readonly {
    chunkId: unknown;
    documentId: unknown;
    documentTitle: string;
    excerpt: string;
    rank: number;
    score: number;
  }[];
};

/** Only the two roles the transcript has a rendering for. A `system` turn is
 * the prompt, already accounted for elsewhere, and a `tool` turn (Phase 4)
 * has no display defined yet -- skipped rather than guessed at, matching how
 * `ChatService` builds history for the provider. */
const RENDERED_ROLES: Record<string, "user" | "assistant"> = {
  USER: "user",
  ASSISTANT: "assistant",
};

export function toTranscript(messages: readonly StoredMessage[]): ChatMessageData[] {
  const transcript: ChatMessageData[] = [];
  for (const message of messages) {
    const role = RENDERED_ROLES[message.role];
    if (role === undefined) continue;

    // Partial text is kept alongside the error: the API persists what
    // streamed before a failure precisely so history agrees with what the
    // user watched happen.
    const text = message.content ?? "";
    const failed = message.error !== null;

    const entry: ChatMessageData = {
      id: String(message.id),
      role,
      text,
      status: failed ? "error" : "done",
    };
    if (failed) {
      // The stored row keeps the message but not the machine-readable code
      // the SSE `error` event carried, so this is the honest reconstruction:
      // the text that was shown, under a generic code.
      entry.error = { code: "error", message: message.error as string };
    }
    // Only when there is something real to report. Zeroes here would make a
    // failed turn claim it used no tokens rather than that nothing was
    // measured.
    if (message.model !== null && message.inputTokens !== null && message.outputTokens !== null) {
      entry.meta = {
        model: message.model,
        usage: { input_tokens: message.inputTokens, output_tokens: message.outputTokens },
        costUsd: message.costUsd,
        latencyMs: message.latencyMs ?? 0,
      };
    }
    // `undefined` and `[]` are different states to the transcript: no
    // retrieval happened, versus retrieval that found nothing.
    if (message.citations.length > 0) {
      entry.citations = message.citations.map((citation) => ({
        chunkId: String(citation.chunkId),
        documentId: String(citation.documentId),
        documentTitle: citation.documentTitle,
        excerpt: citation.excerpt,
        rank: citation.rank,
        score: citation.score,
        // Stored citations do not carry the page number the live event does;
        // `null` is the same "no page concept" this field already means.
        page: null,
      }));
    }
    transcript.push(entry);
  }
  return transcript;
}

/** What a conversation with neither a title nor a question is called. */
const UNTITLED = "Untitled conversation";

/**
 * What the list shows for a conversation.
 *
 * Titles are written by a background job, so a conversation started seconds
 * ago has none -- which is exactly the one being looked at. Falling back to
 * its first question means a row is never blank and never has to be polled
 * for.
 */
export function conversationLabel(conversation: {
  title: string | null;
  preview: string | null;
}): string {
  return conversation.title?.trim() || conversation.preview?.trim() || UNTITLED;
}
