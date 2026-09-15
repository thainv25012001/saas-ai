"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "urql";
import { ChatMessage, type ChatMessageData } from "@/components/chat/ChatMessage";
import { AgentsDocument } from "@/graphql/generated";
import { API_URL } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { NEW_CONVERSATION, resolveTurnOutcome, type TurnState } from "@/lib/chat-turn";
import { streamChat } from "@/lib/sse";

function newId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function PlaygroundContent() {
  const { user, accessToken, setAccessToken, loading } = useAuth();
  const searchParams = useSearchParams();

  const [{ data, fetching }] = useQuery({
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

  if (loading || fetching) {
    return <p className="text-slate-500">Loading…</p>;
  }

  if (agents.length === 0) {
    return (
      <section className="space-y-4">
        <h1 className="text-2xl font-semibold">Playground</h1>
        <div className="rounded-xl border border-slate-200 bg-white p-6 text-sm text-slate-600">
          <p>You don&apos;t have any agents yet. Create one to try it out here.</p>
          <Link
            href="/dashboard/agents"
            className="mt-3 inline-block rounded-md bg-slate-900 px-4 py-2 text-sm text-white"
          >
            Go to Agents
          </Link>
        </div>
      </section>
    );
  }

  return (
    <section className="flex h-[calc(100vh-4rem)] max-h-[900px] flex-col space-y-4">
      <div className="flex items-center justify-between gap-4">
        <h1 className="text-2xl font-semibold">Playground</h1>
        <div className="flex items-center gap-3">
          <label className="text-sm font-medium text-slate-600">
            Agent
            <select
              value={agentId ?? ""}
              onChange={(e) => onSelectAgent(e.target.value)}
              disabled={fetching || isStreaming}
              className="ml-2 rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:opacity-50"
            >
              {agents.map((agent) => (
                <option key={String(agent.id)} value={String(agent.id)}>
                  {agent.name} ({agent.status})
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            onClick={onNewConversation}
            disabled={isStreaming || messages.length === 0}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:opacity-50"
          >
            New conversation
          </button>
        </div>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto rounded-xl border border-slate-200 bg-slate-50 p-4">
        {messages.length === 0 ? (
          <p className="p-6 text-center text-sm text-slate-500">
            Send a message to see the agent respond.
          </p>
        ) : (
          messages.map((message) => <ChatMessage key={message.id} message={message} />)
        )}
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          void sendMessage();
        }}
        className="flex items-end gap-3"
      >
        <label className="flex-1 text-sm font-medium">
          <span className="sr-only">Message</span>
          <textarea
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
            placeholder="Ask the agent something…"
            className="w-full resize-none rounded-md border border-slate-300 px-3 py-2 text-sm disabled:opacity-50"
          />
        </label>
        {isStreaming ? (
          <button
            type="button"
            onClick={onStop}
            className="rounded-md border border-red-300 px-4 py-2 text-sm text-red-700"
          >
            Stop
          </button>
        ) : (
          <button
            type="submit"
            disabled={!input.trim() || !agentId}
            className="rounded-md bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-50"
          >
            Send
          </button>
        )}
      </form>
    </section>
  );
}

export default function PlaygroundPage() {
  return (
    <Suspense fallback={<p className="text-slate-500">Loading…</p>}>
      <PlaygroundContent />
    </Suspense>
  );
}
