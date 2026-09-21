"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useClient, useQuery } from "urql";
import { ModelPicker } from "@/components/agents/ModelPicker";
import { ChatMessage, type ChatMessageData } from "@/components/chat/ChatMessage";
import { ConversationPanel } from "@/components/chat/ConversationPanel";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button, ButtonLink } from "@/components/ui/Button";
import { focusRing } from "@/components/ui/cn";
import { EmptyState } from "@/components/ui/EmptyState";
import { Icon } from "@/components/ui/icons";
import { Select, Textarea } from "@/components/ui/Input";
import { LoadingState } from "@/components/ui/Spinner";
import {
  AgentsDocument,
  ConfiguredProvidersDocument,
  ConversationDocument,
  ConversationsDocument,
  ProviderModelsDocument,
} from "@/graphql/generated";
import { agentStatusLabel, agentStatusTone } from "@/lib/agent-status";
import { API_URL } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { NEW_CONVERSATION, resolveTurnOutcome, type TurnState } from "@/lib/chat-turn";
import { sessionTotals } from "@/lib/chat-totals";
import { toTranscript } from "@/lib/conversation-transcript";
import { firstGraphQLError } from "@/lib/graphql-errors";
import { isOverridden, type ModelSelection, overrideFields } from "@/lib/model-selection";
import { editableProviders, providerLabel } from "@/lib/providers";
import { streamChat } from "@/lib/sse";
import { useComposerFocus } from "@/lib/use-composer-focus";
import { useRememberedFlag } from "@/lib/use-remembered-flag";
import { useStickToBottom } from "@/lib/use-stick-to-bottom";

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

  // The provider/model this playground is answering on. Seeded from the
  // selected agent by the effect below, and never written back to it: the
  // point of a playground is to try a model on a real conversation *before*
  // committing the agent to it. `sendMessage` carries the difference as a
  // per-request override; the agent row is untouched either way.
  const [selection, setSelection] = useState<ModelSelection>({ provider: "", model: "" });

  const [providersResult] = useQuery({
    query: ConfiguredProvidersDocument,
    pause: loading || !user,
  });
  const providers = useMemo(
    () => providersResult.data?.configuredProviders ?? [],
    [providersResult.data],
  );
  const [modelsResult] = useQuery({
    query: ProviderModelsDocument,
    variables: { provider: selection.provider },
    pause: selection.provider === "",
  });

  // Follows the transcript as tokens arrive, but only while the reader is at
  // the bottom -- see `useStickToBottom` for why the unconditional version is
  // worse than none on a stream.
  const transcript = useStickToBottom(messages);
  const stickToBottom = transcript.stick;

  // The composer is disabled while a turn streams, and a disabled element
  // loses focus without getting it back -- see `useComposerFocus`.
  const composerRef = useComposerFocus(isStreaming);

  // Past conversations for the selected agent. Filtered to the playground's
  // own channel: once the embedded widget ships, real customer traffic on the
  // same agent would otherwise bury the threads you are testing with.
  const [historyResult, refetchHistory] = useQuery({
    query: ConversationsDocument,
    variables: { agentId: agentId ?? "", channel: "PLAYGROUND" },
    pause: agentId === null,
  });
  const history = useMemo(
    () => (historyResult.data?.conversations ?? []).map((row) => ({
      id: String(row.id),
      title: row.title,
      preview: row.preview,
    })),
    [historyResult.data],
  );

  // Collapsed by default on a narrow viewport -- below `lg` the app's own nav
  // is already a drawer, and a 15rem panel beside the transcript leaves
  // neither of them usable. Remembered afterwards, so the choice survives
  // navigating away.
  const [panelCollapsed, setPanelCollapsed] = useRememberedFlag(
    "playground:conversations-collapsed",
    () => window.innerWidth < 1024,
  );

  // Fetched in the handler, not through a paused query and an effect. Opening
  // a conversation is an event, and expressing it as state to subscribe to had
  // a bug in it: urql re-emits the same `data` object for a repeated query, so
  // "open a row, start a new conversation, click that row again" changed no
  // dependency and the effect never re-ran -- the click did nothing.
  const client = useClient();
  // Only so the panel can highlight the row you clicked while its transcript
  // is still in flight. Not a second source of truth for what is on screen.
  const [openingId, setOpeningId] = useState<string | null>(null);

  async function onOpenConversation(conversationId: string) {
    if (conversationId === conversation.conversationId) return;
    abortRef.current?.abort();
    setOpeningId(conversationId);
    try {
      const result = await client
        .query(ConversationDocument, { id: conversationId })
        .toPromise();
      const loaded = result.data?.conversation;
      // Deleted, or never yours: the list is a snapshot and the row may be
      // stale. Leaving the transcript alone is the honest response.
      if (!loaded) return;
      setMessages(toTranscript(loaded.messages));
      // Committed by definition: it is a conversation the server has already
      // written turns into, so the next message continues this thread rather
      // than opening a new one.
      setConversation({ conversationId: String(loaded.id), committed: true });
      stickToBottom();
    } finally {
      setOpeningId(null);
    }
  }

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

  const selectedAgent = agents.find((candidate) => String(candidate.id) === agentId);
  const agentSelection: ModelSelection = {
    provider: selectedAgent?.provider ?? "",
    model: selectedAgent?.model ?? "",
  };

  // Seed the picker from whichever agent is selected, and re-seed whenever
  // that changes -- switching agents already clears the transcript, so
  // carrying the previous agent's model across would leave the header
  // describing a turn nobody sent.
  const seededFor = useRef<string | null>(null);
  useEffect(() => {
    if (selectedAgent === undefined || seededFor.current === agentId) return;
    seededFor.current = agentId;
    setSelection({ provider: selectedAgent.provider, model: selectedAgent.model });
  }, [agentId, selectedAgent]);

  function onNewConversation() {
    setConversation(NEW_CONVERSATION);
    setMessages([]);
    stickToBottom();
  }

  function onSelectAgent(nextAgentId: string) {
    if (nextAgentId === agentId) return;
    setAgentId(nextAgentId);
    // A conversation belongs to one agent's history/prompt; switching agents
    // starts a fresh thread rather than silently mixing two agents' turns
    // into one conversation_id.
    onNewConversation();
  }

  function onSelectProvider(nextProvider: string) {
    // The model belongs to the provider that serves it, so it cannot survive
    // the switch. Blank until the new provider's list resolves, which
    // `ModelPicker` renders as a placeholder rather than a silently wrong id.
    setSelection({ provider: nextProvider, model: "" });
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
    // Sending is an unambiguous request to see the answer, so it re-arms
    // following even for a reader who had scrolled up to re-read something.
    stickToBottom();

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
    // Whether this turn can add a row to the list. A continuation only
    // reorders it, and the current conversation is already the highlighted
    // row -- refetching for that spent a GraphQL round trip and two queries
    // per turn to change nothing visible.
    const startsConversation = conversation.conversationId === null;

    try {
      await streamChat({
        agentId,
        message: text,
        conversationId: conversation.conversationId,
        accessToken,
        apiUrl: API_URL,
        signal: controller.signal,
        onAccessToken: setAccessToken,
        // Empty unless the picker has been moved off the agent's own pair, so
        // an untouched playground sends exactly the request it always did.
        override: overrideFields(selection, agentSelection),
        onEvent: (event) => {
          switch (event.type) {
            case "message_start":
              seenConversationId = event.conversation_id;
              break;
            case "citations":
              // Phase 3 guaranteed this arrived before the first
              // text_delta; Phase 4 makes retrieval a tool the model can
              // call after already speaking, so this may now land
              // mid-stream or after the visible answer (see
              // `ChatMessageData.citations`'s docstring in
              // `ChatMessage.tsx` and `ChatCitations`'s own docstring in
              // `apps/api/app/chat/service.py`). Merged in wherever it
              // arrives rather than assumed to be first.
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
            case "tool_call_start":
              // Appended in the order the calls started; `tool_call_end`
              // below matches each one back by id, so two calls in the same
              // step (a step can gather more than one) never get mixed up.
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId
                    ? {
                        ...m,
                        toolCalls: [
                          ...(m.toolCalls ?? []),
                          ...event.calls.map((call) => ({
                            id: call.id,
                            name: call.name,
                            arguments: call.arguments,
                            status: "running" as const,
                          })),
                        ],
                      }
                    : m,
                ),
              );
              break;
            case "tool_call_end":
              setMessages((prev) =>
                prev.map((m) => {
                  if (m.id !== assistantId || !m.toolCalls) return m;
                  const byId = new Map(event.results.map((result) => [result.tool_call_id, result]));
                  return {
                    ...m,
                    toolCalls: m.toolCalls.map((call) => {
                      const result = byId.get(call.id);
                      return result
                        ? {
                            ...call,
                            status: "done" as const,
                            result: result.result,
                            isError: result.is_error,
                          }
                        : call;
                    }),
                  };
                }),
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
      // Deliberately not polled afterwards for the title: it is written by a
      // background job, and until it lands the row reads as its first question
      // rather than as nothing.
      if (startsConversation && sawMessageEnd) {
        refetchHistory({ requestPolicy: "network-only" });
      }
    }
  }

  function onStop() {
    abortRef.current?.abort();
  }

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
    // Two columns: the conversation panel, then the playground itself as three
    // rows -- the transcript is the only scroll container in that column, so
    // the composer stays put without any viewport arithmetic.
    <section className="flex h-full">
      <ConversationPanel
        conversations={history}
        currentId={openingId ?? conversation.conversationId}
        fetching={historyResult.fetching}
        collapsed={panelCollapsed}
        onToggle={() => setPanelCollapsed(!panelCollapsed)}
        onOpen={onOpenConversation}
        onNew={onNewConversation}
        canStartNew={!isStreaming && messages.length > 0}
      />

      <div className="grid min-h-0 min-w-0 flex-1 grid-rows-[auto_1fr_auto]">
      {/* Three groups, not seven: what is answering (Agent), what it is
        * answering with (Model), and what that is costing. "New conversation"
        * moved into the panel, where the thread it acts on lives, and the
        * "Playground" heading moved to `sr-only` -- the nav already says
        * which page this is, and the toolbar was repeating it. */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-line bg-surface px-6 py-2.5">
        <h1 className="sr-only">Playground</h1>

        <label className="flex items-center gap-2 text-sm text-ink-muted">
          <span className="font-medium">Agent</span>
          <Select
            value={agentId ?? ""}
            onChange={(e) => onSelectAgent(e.target.value)}
            disabled={fetching || isStreaming}
            width="auto"
            className="min-w-44"
          >
            {agents.map((agent) => (
              <option key={String(agent.id)} value={String(agent.id)}>
                {agent.name}
              </option>
            ))}
          </Select>
          {selectedAgent ? (
            <Badge tone={agentStatusTone(selectedAgent.status)}>
              {agentStatusLabel(selectedAgent.status)}
            </Badge>
          ) : null}
        </label>

        {selectedAgent ? (
          /* A div, not a label: this group holds two controls, and a label
           * can only ever name one of them. Each carries its own
           * `aria-label` instead, and the heading is decoration. */
          <div className="flex flex-wrap items-center gap-2 text-sm text-ink-muted">
            <span className="font-medium">Model</span>
            {/* Not disabled while streaming, unlike Agent above: the override
              * is read when a turn is sent, so changing it mid-stream affects
              * only the next one -- there is nothing to protect by locking
              * it, and locking a control that works is its own small lie. */}
            <Select
              value={selection.provider}
              onChange={(e) => onSelectProvider(e.target.value)}
              width="auto"
              aria-label="Provider"
            >
              {/* Same list the agent form offers, disabled entries and all: a
                * provider with no API key is named and greyed out rather than
                * hidden, so picking it is impossible and its absence is never
                * a mystery. */}
              {editableProviders(providers, agentSelection.provider).map((option) => (
                <option key={option.id} value={option.id} disabled={!option.configured}>
                  {option.configured
                    ? providerLabel(option.id)
                    : `${providerLabel(option.id)} — no API key`}
                </option>
              ))}
            </Select>
            <ModelPicker
              value={selection.model}
              onChange={(model) => setSelection((prev) => ({ ...prev, model }))}
              options={modelsResult.data?.providerModels ?? []}
              fetching={modelsResult.fetching}
              failed={modelsResult.error !== undefined}
              aria-label="Model"
            />

            {isOverridden(selection, agentSelection) ? (
              // A badge and a verb, where a sentence used to be. It still has
              // to say the agent itself is unchanged -- that is the one thing
              // this picker deliberately does not do -- but "Session only"
              // says it in two words, and the agent's own model is on the
              // reset control where it explains what reset means.
              <>
                <Badge tone="info">Session only</Badge>
                <button
                  type="button"
                  onClick={() => setSelection(agentSelection)}
                  title={`Back to the agent's model (${agentSelection.model})`}
                  className={`rounded-control px-1.5 py-0.5 text-xs text-ink-subtle underline underline-offset-2 hover:text-ink ${focusRing}`}
                >
                  Reset
                </button>
              </>
            ) : null}
          </div>
        ) : null}

        {totals.pricedTurns + totals.unpricedTurns > 0 ? (
          <p className="ml-auto text-xs text-ink-muted">
            {totals.pricedTurns > 0 ? (
              <>
                <span className="font-medium text-ink">${totals.costUsd.toFixed(4)}</span> ·{" "}
              </>
            ) : null}
            {totals.inputTokens} in / {totals.outputTokens} out
            {totals.unpricedTurns > 0 ? ` · ${totals.unpricedTurns} unpriced` : ""}
          </p>
        ) : null}
      </div>

      <div
        ref={transcript.ref}
        onScroll={transcript.onScroll}
        className="min-h-0 space-y-5 overflow-y-auto bg-surface-muted px-6 py-6"
      >
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
              ref={composerRef}
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
      </div>
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
