"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "urql";
import { ChatMessage } from "@/components/chat/ChatMessage";
import { Alert } from "@/components/ui/Alert";
import { Button, ButtonLink } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { focusRing } from "@/components/ui/cn";
import { PageHeader } from "@/components/ui/PageHeader";
import { Select } from "@/components/ui/Input";
import { LoadingState } from "@/components/ui/Spinner";
import { AgentsDocument, ConversationDocument, ConversationsDocument } from "@/graphql/generated";
import { useAuth } from "@/lib/auth";
import {
  CHANNEL_FILTERS,
  channelFilterLabel,
  channelLabel,
  channelQueryValue,
  parseChannelFilter,
  pickDefaultAgentId,
  type ChannelFilter,
} from "@/lib/conversations-page";
import { conversationLabel, toTranscript } from "@/lib/conversation-transcript";
import { firstGraphQLError } from "@/lib/graphql-errors";
import { formatRelativeTime } from "@/lib/format";

const PAGE_SIZE = 50;

function ConversationsContent() {
  const { user, loading } = useAuth();
  const searchParams = useSearchParams();

  const [{ data: agentsData, fetching: agentsFetching, error: agentsError }] = useQuery({
    query: AgentsDocument,
    pause: loading || !user,
  });
  const agents = useMemo(() => agentsData?.agents ?? [], [agentsData]);

  const [agentId, setAgentId] = useState<string | null>(null);
  const [channel, setChannel] = useState<ChannelFilter>(() =>
    parseChannelFilter(searchParams.get("channel")),
  );
  const [offset, setOffset] = useState(0);
  const [rows, setRows] = useState<
    { id: string; title: string | null; preview: string | null; channel: string; lastMessageAt: string | null; createdAt: string }[]
  >([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // `?agent=...` (a link from the Leads page) picked once the agent list
  // resolves, else the first agent -- same precedent as Leads' and the
  // Playground's own pickers. Only runs while nothing has been chosen yet, so
  // it never fights a deliberate switch from the dropdown.
  useEffect(() => {
    if (agentId !== null) return;
    const next = pickDefaultAgentId(agents, searchParams.get("agent"));
    if (next !== null) setAgentId(next);
  }, [agentId, agents, searchParams]);

  // `?conversation=...` opens once, on the very first render -- not tied to
  // `selectedId === null`, which a later deliberate switch of agent or
  // channel also produces, and which must not bring the old query-string
  // conversation back.
  const appliedInitialConversation = useRef(false);
  useEffect(() => {
    if (appliedInitialConversation.current) return;
    appliedInitialConversation.current = true;
    const fromQuery = searchParams.get("conversation");
    if (fromQuery) setSelectedId(fromQuery);
  }, [searchParams]);

  function onSelectAgent(nextAgentId: string) {
    setAgentId(nextAgentId);
    setSelectedId(null);
  }

  function onSelectChannel(nextChannel: ChannelFilter) {
    setChannel(nextChannel);
    setSelectedId(null);
  }

  // A different agent or channel starts the list over from the first page.
  useEffect(() => {
    setOffset(0);
    setRows([]);
  }, [agentId, channel]);

  const [{ data: listData, fetching: listFetching, error: listError }] = useQuery({
    query: ConversationsDocument,
    variables: {
      agentId: agentId ?? "",
      channel: channelQueryValue(channel),
      limit: PAGE_SIZE,
      offset,
    },
    pause: agentId === null,
  });

  useEffect(() => {
    if (!listData) return;
    setRows((prev) =>
      offset === 0 ? listData.conversations : [...prev, ...listData.conversations],
    );
  }, [listData, offset]);

  const hasMore = (listData?.conversations.length ?? 0) === PAGE_SIZE;

  const [{ data: conversationData, fetching: conversationFetching, error: conversationError }] =
    useQuery({
      query: ConversationDocument,
      variables: { id: selectedId ?? "" },
      pause: selectedId === null,
    });

  const transcript = useMemo(
    () => (conversationData?.conversation ? toTranscript(conversationData.conversation.messages) : []),
    [conversationData],
  );

  if (loading || (agentsFetching && !agentsData)) {
    return <LoadingState label="Loading conversations…" />;
  }

  if (agents.length === 0) {
    return (
      <div className="mx-auto w-full max-w-6xl px-6 py-8">
        <PageHeader
          title="Conversations"
          description="Every conversation your agents have had — on the website widget, the playground and the API."
        />
        {agentsError ? (
          <Alert tone="danger">{firstGraphQLError(agentsError)}</Alert>
        ) : (
          <EmptyState
            icon="conversation"
            title="No agents yet"
            description="Conversations are grouped per agent. Create one first."
            action={<ButtonLink href="/dashboard/agents">Create an agent</ButtonLink>}
          />
        )}
      </div>
    );
  }

  const listQueryError = firstGraphQLError(listError);
  const conversationQueryError = firstGraphQLError(conversationError);

  return (
    <div className="space-y-4">
      <PageHeader
        title="Conversations"
        description="Every conversation your agents have had — on the website widget, the playground and the API."
        meta={
          <>
            <label className="flex items-center gap-2 text-sm text-ink-muted">
              <span className="font-medium">Agent</span>
              <Select
                value={agentId ?? ""}
                onChange={(e) => onSelectAgent(e.target.value)}
                width="auto"
                className="min-w-44"
              >
                {agents.map((agent) => (
                  <option key={String(agent.id)} value={String(agent.id)}>
                    {agent.name}
                  </option>
                ))}
              </Select>
            </label>
            <label className="flex items-center gap-2 text-sm text-ink-muted">
              <span className="font-medium">Channel</span>
              <Select
                value={channel}
                onChange={(e) => onSelectChannel(e.target.value as ChannelFilter)}
                width="auto"
              >
                {CHANNEL_FILTERS.map((filter) => (
                  <option key={filter} value={filter}>
                    {channelFilterLabel(filter)}
                  </option>
                ))}
              </Select>
            </label>
          </>
        }
      />

      {listQueryError ? <Alert tone="danger">{listQueryError}</Alert> : null}

      <div className="grid gap-4 lg:grid-cols-[22rem_1fr]">
        <Card className="min-h-0">
          {listFetching && rows.length === 0 ? (
            <LoadingState label="Loading conversations…" />
          ) : rows.length === 0 ? (
            channel === "WIDGET" ? (
              <EmptyState
                icon="conversation"
                title="No widget conversations yet"
                description="Once a visitor chats through this agent's embedded widget, it shows up here."
                action={
                  agentId ? (
                    <ButtonLink href={`/dashboard/agents/${agentId}#widget-card`}>
                      Open the widget card
                    </ButtonLink>
                  ) : undefined
                }
              />
            ) : (
              <EmptyState
                icon="conversation"
                title="No conversations yet"
                description="Conversations on this channel will show up here once there are any."
              />
            )
          ) : (
            <>
              <ul className="divide-y divide-line">
                {rows.map((row) => {
                  const isOpen = row.id === selectedId;
                  return (
                    <li key={row.id}>
                      <button
                        type="button"
                        onClick={() => setSelectedId(row.id)}
                        aria-current={isOpen ? "true" : undefined}
                        className={`block w-full px-4 py-3 text-left ${focusRing} focus-visible:ring-offset-1 ${
                          isOpen ? "bg-surface-muted" : "hover:bg-surface-muted"
                        }`}
                      >
                        <span className="block truncate text-sm font-medium text-ink">
                          {conversationLabel(row)}
                        </span>
                        <span className="mt-0.5 block text-xs text-ink-subtle">
                          {formatRelativeTime(row.lastMessageAt ?? row.createdAt)}
                          {channel === "ALL" ? ` · ${channelLabel(row.channel)}` : ""}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
              {hasMore ? (
                <div className="border-t border-line px-4 py-3">
                  <Button
                    variant="secondary"
                    size="sm"
                    loading={listFetching}
                    loadingLabel="Loading…"
                    onClick={() => setOffset((prev) => prev + PAGE_SIZE)}
                  >
                    Load more
                  </Button>
                </div>
              ) : null}
            </>
          )}
        </Card>

        <Card className="min-h-[20rem]">
          {selectedId === null ? (
            <EmptyState
              icon="conversation"
              title="No conversation selected"
              description="Choose a conversation on the left to read its transcript."
            />
          ) : conversationFetching && !conversationData ? (
            <LoadingState label="Loading conversation…" />
          ) : conversationQueryError ? (
            <div className="p-5">
              <Alert tone="danger">{conversationQueryError}</Alert>
            </div>
          ) : !conversationData?.conversation ? (
            <EmptyState
              icon="conversation"
              title="Conversation not found"
              description="It may have been deleted since this list was loaded."
            />
          ) : transcript.length === 0 ? (
            <EmptyState
              icon="conversation"
              title="No messages yet"
              description="This conversation has no messages to show."
            />
          ) : (
            <div className="space-y-5 overflow-y-auto p-5">
              {transcript.map((message) => (
                <ChatMessage key={message.id} message={message} />
              ))}
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

export default function ConversationsPage() {
  return (
    <Suspense fallback={<LoadingState label="Loading conversations…" />}>
      <ConversationsContent />
    </Suspense>
  );
}
