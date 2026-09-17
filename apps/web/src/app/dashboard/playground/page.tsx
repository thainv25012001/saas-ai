"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "urql";
import { ChatMessage, type ChatMessageData } from "@/components/chat/ChatMessage";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button, ButtonLink } from "@/components/ui/Button";
import { focusRing } from "@/components/ui/cn";
import { EmptyState } from "@/components/ui/EmptyState";
import { Icon } from "@/components/ui/icons";
import { Select, Textarea } from "@/components/ui/Input";
import { LoadingState } from "@/components/ui/Spinner";
import { AgentsDocument } from "@/graphql/generated";
import { agentStatusLabel, agentStatusTone } from "@/lib/agent-status";
import { API_URL } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { NEW_CONVERSATION, resolveTurnOutcome, type TurnState } from "@/lib/chat-turn";
import { sessionTotals } from "@/lib/chat-totals";
import { firstGraphQLError } from "@/lib/graphql-errors";
import { streamChat } from "@/lib/sse";

function newId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

const EXAMPLE_PROMPTS = [
  "What do you sell, and who is it for?",
  "How much does the starter plan cost?",
  "Can you compare your two cheapest options?",
];

function PlaygroundContent() {
  const { user, accessToken, setAccessToken, loading } = useAuth();
  const searchParams = useSearchParams();

  const [{ data, fetching, error }] = useQuery({
    query: AgentsDocument,
    pause: loading || !user,
  });
  const agents = useMemo(() => data?.agents ?? [], [data]);

  const [agentId, setAgentId] = useState<string | null>(null);
  // The conversation id AND whether the server has ever committed a turn in
  // it. Both are needed, and only together: see `@/lib/chat-turn` for why an
  // id from `message_start` alone is not yet a conversation that exists.
  const [conversation, setConversation] = useState<TurnState>(NEW_CONVERSATION);
  const [messages, setMessages] = useState<ChatMessageData[]>([]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  // Preselect from ?agentId=... (set by the "Test in playground" link on an
  // agent's detail page), falling back to the first agent once the query
  // resolves. Only ever runs while nothing is selected yet, so it never
  // fights the user's own choice from the dropdown below.
  useEffect(() => {
    if (agentId !== null) return;
    const fromQuery = searchParams.get("agentId");
    if (fromQuery && agents.some((agent) => String(agent.id) === fromQuery)) {
      setAgentId(fromQuery);
    } else if (agents.length > 0) {
      setAgentId(String(agents[0].id));
    }
  }, [agentId, agents, searchParams]);

  function onSelectAgent(nextAgentId: string) {
    if (nextAgentId === agentId) return;
    setAgentId(nextAgentId);
    // A conversation belongs to one agent's history/prompt; switching agents
    // starts a fresh thread rather than silently mixing two agents' turns
    // into one conversation_id.
    setConversation(NEW_CONVERSATION);
    setMessages([]);
  }

  function onNewConversation() {
    setConversation(NEW_CONVERSATION);
    setMessages([]);
  }

  const finalizeStreamingMessage = useCallback((id: string) => {
    setMessages((prev) =>
      prev.map((m) => (m.id === id && m.status === "streaming" ? { ...m, status: "done" } : m)),
    );
  }, []);

  async function sendMessage() {
    const text = input.trim();
    if (!text || isStreaming || !agentId || !accessToken) return;

    setInput("");

    const userMessage: ChatMessageData = {
      id: newId(),
      role: "user",
      text,
      status: "done",
    };
    const assistantId = newId();
    const assistantMessage: ChatMessageData = {
      id: assistantId,
      role: "assistant",
      text: "",
      status: "streaming",
    };
    setMessages((prev) => [...prev, userMessage, assistantMessage]);

    const controller = new AbortController();
    abortRef.current = controller;
    setIsStreaming(true);

    // What this turn learns about the conversation's identity, resolved once
    // in the `finally` below rather than written to state mid-stream. Seeded
    // with the id the request carried, so a turn that never gets a
    // `message_start` at all (a pre-stream error) still resolves against
    // something real.
    let seenConversationId = conversation.conversationId;
    let sawMessageEnd = false;

    try {
      await streamChat({
        agentId,
        message: text,
        conversationId: conversation.conversationId,
        accessToken,
        apiUrl: API_URL,
        signal: controller.signal,
        onAccessToken: setAccessToken,
        onEvent: (event) => {
          switch (event.type) {
            case "message_start":
              seenConversationId = event.conversation_id;
              break;
            case "citations":
              // Arrives after message_start and before the first
              // text_delta (see docs/PHASE-3.md and `ChatCitations`'s
              // docstring), so sources are on screen while the answer is
              // still streaming in rather than only once it ends.
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId
                    ? {
                        ...m,
                        citations: event.citations.map((citation) => ({
                          chunkId: citation.chunk_id,
                          documentId: citation.document_id,
                          documentTitle: citation.document_title,
                          rank: citation.rank,
                          score: citation.score,
                          excerpt: citation.excerpt,
                          page: citation.page,
                        })),
                      }
                    : m,
                ),
              );
              break;
            case "text_delta":
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId ? { ...m, text: m.text + event.text } : m,
                ),
              );
              break;
            case "message_end":
              sawMessageEnd = true;
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId
                    ? {
                        ...m,
                        status: "done",
                        meta: {
                          model: event.model,
                          usage: event.usage,
                          costUsd: event.cost_usd,
                          latencyMs: event.latency_ms,
                        },
                      }
                    : m,
                ),
              );
              break;
            case "error":
              // Keep whatever partial text already arrived -- the user saw
              // those tokens, and (for the handled-error case) the server
              // has already persisted them.
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId
                    ? { ...m, status: "error", error: { code: event.code, message: event.message } }
                    : m,
                ),
              );
              break;
          }
        },
      });
    } finally {
      // Covers the abort path (streamChat swallows AbortError and returns
      // silently, so no `error`/`message_end` event ever arrives to close
      // out the placeholder) and any stream that ends without a
      // `message_end` for some other reason.
      finalizeStreamingMessage(assistantId);
      // And the same two cases for the conversation's identity. A first turn
      // that never reached `message_end` was rolled back server-side, taking
      // the INSERT that created the conversation with it -- so holding on to
      // the id from its `message_start` makes every subsequent send fail
      // with a pre-stream 404 that nothing here clears. The functional form
      // is deliberate: this runs long after the render that read
      // `conversation` above.
      setConversation((prev) =>
        resolveTurnOutcome(prev, { conversationId: seenConversationId, sawMessageEnd }),
      );
      setIsStreaming(false);
      abortRef.current = null;
    }
  }

  function onStop() {
    abortRef.current?.abort();
  }

  const selectedAgent = agents.find((candidate) => String(candidate.id) === agentId);

  // The playground's job is to tell you what a real conversation costs. The
  // numbers were already arriving per turn and were never added up.
  const totals = sessionTotals(
    messages.flatMap((message) => (message.meta ? [message.meta] : [])),
  );

  if (loading || (fetching && !data)) {
    return <LoadingState label="Loading the playground…" />;
  }

  if (agents.length === 0) {
    return (
      <div className="mx-auto w-full max-w-6xl px-6 py-8">
        <h1 className="text-xl font-semibold text-ink">Playground</h1>
        <div className="mt-6">
          {error ? (
            <Alert tone="danger">{firstGraphQLError(error)}</Alert>
          ) : (
            <EmptyState
              icon="playground"
              title="No agents to test yet"
              description="The playground runs a real conversation against one of your agents, and streams back its answer with tokens, latency and cost."
              action={<ButtonLink href="/dashboard/agents">Create an agent</ButtonLink>}
            />
          )}
        </div>
      </div>
    );
  }

  return (
    // Three rows in a full-height grid: the transcript is the only scroll
    // container, so the composer stays put without any viewport arithmetic.
    <section className="grid h-full grid-rows-[auto_1fr_auto]">
      <div className="flex flex-wrap items-center gap-3 border-b border-line bg-surface px-6 py-3">
        <h1 className="text-sm font-semibold text-ink">Playground</h1>
        <label className="flex items-center gap-2 text-sm text-ink-muted">
          <span className="font-medium">Agent</span>
          <Select
            value={agentId ?? ""}
            onChange={(e) => onSelectAgent(e.target.value)}
            disabled={fetching || isStreaming}
            width="auto"
            className="min-w-48"
          >
            {agents.map((agent) => (
              <option key={String(agent.id)} value={String(agent.id)}>
                {agent.name}
              </option>
            ))}
          </Select>
        </label>

        {selectedAgent ? (
          <>
            <Badge tone={agentStatusTone(selectedAgent.status)}>
              {agentStatusLabel(selectedAgent.status)}
            </Badge>
            <Badge>{selectedAgent.model}</Badge>
          </>
        ) : null}

        <div className="ml-auto flex items-center gap-3">
          {totals.pricedTurns + totals.unpricedTurns > 0 ? (
            <p className="text-xs text-ink-muted">
              {totals.pricedTurns > 0 ? (
                <>
                  <span className="font-medium text-ink">${totals.costUsd.toFixed(4)}</span>{" "}
                  ·{" "}
                </>
              ) : null}
              {totals.inputTokens} in / {totals.outputTokens} out
              {totals.unpricedTurns > 0 ? ` · ${totals.unpricedTurns} unpriced` : ""}
            </p>
          ) : null}
          <Button
            variant="secondary"
            size="sm"
            onClick={onNewConversation}
            disabled={isStreaming || messages.length === 0}
          >
            New conversation
          </Button>
        </div>
      </div>

      <div className="min-h-0 space-y-5 overflow-y-auto bg-surface-muted px-6 py-6">
        {messages.length === 0 ? (
          <EmptyState
            icon="playground"
            title="Ask your agent something"
            description="Its answer streams back token by token, with the model, cost and latency of the turn."
            action={
              <div className="flex flex-wrap justify-center gap-2">
                {EXAMPLE_PROMPTS.map((prompt) => (
                  <button
                    key={prompt}
                    type="button"
                    onClick={() => setInput(prompt)}
                    className={`rounded-control border border-line bg-surface px-3 py-1.5 text-xs text-ink-muted hover:text-ink ${focusRing} focus-visible:ring-offset-2`}
                  >
                    {prompt}
                  </button>
                ))}
              </div>
            }
          />
        ) : (
          messages.map((message) => <ChatMessage key={message.id} message={message} />)
        )}
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          void sendMessage();
        }}
        className="border-t border-line bg-surface px-6 py-4"
      >
        <div className="flex items-end gap-3">
          <label className="flex-1">
            <span className="sr-only">Message</span>
            <Textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void sendMessage();
                }
              }}
              disabled={isStreaming || !agentId}
              rows={2}
              resize="y"
              className="max-h-48"
              placeholder="Ask the agent something…"
            />
          </label>
          {isStreaming ? (
            <Button type="button" variant="danger" onClick={onStop}>
              <Icon name="stop" size="md" />
              Stop
            </Button>
          ) : (
            <Button type="submit" disabled={!input.trim() || !agentId}>
              <Icon name="send" size="md" />
              Send
            </Button>
          )}
        </div>
        <p className="mt-1.5 text-xs text-ink-subtle">
          Enter to send · Shift+Enter for a new line
        </p>
      </form>
    </section>
  );
}

export default function PlaygroundPage() {
  return (
    <Suspense fallback={<LoadingState label="Loading the playground…" />}>
      <PlaygroundContent />
    </Suspense>
  );
}
