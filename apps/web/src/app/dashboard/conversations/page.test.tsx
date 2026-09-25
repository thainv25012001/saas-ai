// @vitest-environment happy-dom
/**
 * The one page-level test in this app (see the fix report in
 * `.superpowers/sdd/2026-09-25-phase-8-widget/task-6-report.md` for why no
 * other `page.tsx` here has one): a controller ruling required exercising
 * the page's rendering and its once-only `?conversation=` guard directly,
 * not just the pure helpers behind it (`conversations-page.test.ts`).
 *
 * `urql`'s `useQuery` and `next/navigation`'s `useSearchParams` are mocked
 * wholesale -- there is no `Provider`/router tree to mount here, only the
 * page component. The mock keys off each query document's own operation
 * name (`query.definitions[0].name.value`, the same GraphQL AST every
 * generated document already carries) rather than importing the generated
 * `*Document` consts into the mock factory, which `vi.mock`'s hoisting
 * would otherwise make unavailable.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ConversationsPage from "./page";

type ConversationRow = {
  id: string;
  title: string | null;
  preview: string | null;
  channel: string;
  lastMessageAt: string | null;
  createdAt: string;
};

type StoredMessage = {
  id: string;
  seq: number;
  role: "USER" | "ASSISTANT";
  content: string | null;
  provider: null;
  model: null;
  inputTokens: null;
  outputTokens: null;
  costUsd: null;
  latencyMs: null;
  finishReason: null;
  error: null;
  createdAt: string;
  citations: [];
};

function message(overrides: Partial<StoredMessage> & { id: string; role: "USER" | "ASSISTANT"; content: string }): StoredMessage {
  return {
    seq: 1,
    provider: null,
    model: null,
    inputTokens: null,
    outputTokens: null,
    costUsd: null,
    latencyMs: null,
    finishReason: null,
    error: null,
    createdAt: "2026-02-01T12:00:00Z",
    citations: [],
    ...overrides,
  };
}

// `vi.hoisted` so this fixture state is available inside the `vi.mock`
// factories below, which vitest hoists above every import (including a
// plain top-level const) in this file.
const { AGENTS, CONVERSATIONS_BY_AGENT, CONVERSATION_DETAIL, searchParamsStore, mockSearchParams } =
  vi.hoisted(() => {
    const AGENTS = [
      {
        id: "agent-1",
        name: "Agent One",
        slug: "agent-one",
        status: "ACTIVE",
        model: "gpt-x",
        provider: "openai",
        createdAt: "2026-01-01T00:00:00Z",
      },
      {
        id: "agent-2",
        name: "Agent Two",
        slug: "agent-two",
        status: "ACTIVE",
        model: "gpt-x",
        provider: "openai",
        createdAt: "2026-01-01T00:00:00Z",
      },
    ];

    const CONVERSATIONS_BY_AGENT: Record<string, ConversationRow[]> = {
      "agent-1": [
        {
          id: "c1",
          title: "Pricing questions",
          preview: null,
          channel: "WIDGET",
          lastMessageAt: "2026-02-01T12:00:00Z",
          createdAt: "2026-02-01T11:00:00Z",
        },
        {
          id: "c2",
          title: null,
          preview: "How much is shipping?",
          channel: "WIDGET",
          lastMessageAt: "2026-02-01T10:00:00Z",
          createdAt: "2026-02-01T09:00:00Z",
        },
        {
          id: "c3",
          title: null,
          preview: null,
          channel: "WIDGET",
          lastMessageAt: null,
          createdAt: "2026-02-01T08:00:00Z",
        },
      ],
      "agent-2": [],
    };

    const CONVERSATION_DETAIL: Record<string, { id: string; title: string | null; messages: StoredMessage[] }> = {
      c1: {
        id: "c1",
        title: "Pricing questions",
        messages: [
          message({ id: "m1", role: "USER", content: "Hello from c1" }),
          message({ id: "m2", seq: 2, role: "ASSISTANT", content: "Reply for c1" }),
        ],
      },
      c2: {
        id: "c2",
        title: null,
        messages: [
          message({ id: "m3", role: "USER", content: "Hello from c2" }),
          message({ id: "m4", seq: 2, role: "ASSISTANT", content: "Reply for c2" }),
        ],
      },
    };

    const searchParamsStore = new Map<string, string>();
    // A single stable object, the same as real `next/navigation` returns
    // across re-renders of the same URL -- a fresh object every call would
    // make every effect keyed on `searchParams` re-run every render, which
    // is not what this page (or a real page) ever sees.
    const mockSearchParams = { get: (key: string) => searchParamsStore.get(key) ?? null };

    return { AGENTS, CONVERSATIONS_BY_AGENT, CONVERSATION_DETAIL, searchParamsStore, mockSearchParams };
  });

vi.mock("next/navigation", () => ({
  useSearchParams: () => mockSearchParams,
}));

vi.mock("@/lib/auth", () => ({
  useAuth: () => ({
    user: {
      user_id: "u1",
      email: "owner@example.com",
      full_name: "Owner",
      organization_id: "org-1",
      organization_name: "Acme",
      role: "owner",
    },
    loading: false,
    accessToken: "token",
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    setAccessToken: vi.fn(),
  }),
}));

vi.mock("urql", async (importOriginal) => {
  const actual = await importOriginal<typeof import("urql")>();

  function operationName(query: unknown): string | undefined {
    const doc = query as { definitions?: { name?: { value?: string } }[] };
    return doc.definitions?.[0]?.name?.value;
  }

  const useQuery = (args: { query: unknown; variables?: Record<string, unknown>; pause?: boolean }) => {
    const result = vi.fn();
    if (args.pause) {
      return [{ data: undefined, fetching: false, error: undefined }, result];
    }
    switch (operationName(args.query)) {
      case "Agents":
        return [{ data: { agents: AGENTS }, fetching: false, error: undefined }, result];
      case "Conversations": {
        const agentId = args.variables?.agentId as string;
        return [
          { data: { conversations: CONVERSATIONS_BY_AGENT[agentId] ?? [] }, fetching: false, error: undefined },
          result,
        ];
      }
      case "Conversation": {
        const id = args.variables?.id as string;
        return [
          { data: { conversation: CONVERSATION_DETAIL[id] ?? null }, fetching: false, error: undefined },
          result,
        ];
      }
      default:
        return [{ data: undefined, fetching: false, error: undefined }, result];
    }
  };

  return { ...actual, useQuery: useQuery as unknown as typeof actual.useQuery };
});

describe("ConversationsPage", () => {
  beforeEach(() => {
    searchParamsStore.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the first agent's conversations by default, labelled by title, preview fallback, or Untitled", async () => {
    render(<ConversationsPage />);

    expect(await screen.findByText("Pricing questions")).toBeInTheDocument();
    expect(screen.getByText("How much is shipping?")).toBeInTheDocument();
    expect(screen.getByText("Untitled conversation")).toBeInTheDocument();
    // Agent Two's (empty) list must never leak into the default view.
    expect(screen.queryByText(/no conversations yet/i)).not.toBeInTheDocument();
  });

  it("opens the conversation named in ?conversation= on load, with no click", async () => {
    searchParamsStore.set("conversation", "c1");

    render(<ConversationsPage />);

    expect(await screen.findByText("Reply for c1")).toBeInTheDocument();
  });

  it("does not resurrect the URL's conversation once switching agents has cleared the selection", async () => {
    // The scenario the once-only guard exists for (see the page's own
    // comment above the effect): switching agent or channel calls
    // `setSelectedId(null)`, which -- with a naive "reopen while
    // `selectedId === null`" guard instead of the ref -- would immediately
    // reopen the stale `?conversation=` id again on the very next render.
    searchParamsStore.set("conversation", "c1");

    render(<ConversationsPage />);
    await screen.findByText("Reply for c1");

    fireEvent.change(screen.getByLabelText(/agent/i), { target: { value: "agent-2" } });

    await waitFor(() => expect(screen.getByText(/no conversation selected/i)).toBeInTheDocument());
    expect(screen.queryByText("Reply for c1")).not.toBeInTheDocument();
  });

  it("keeps a newly selected row's conversation rather than the URL's original one", async () => {
    searchParamsStore.set("conversation", "c1");

    render(<ConversationsPage />);
    await screen.findByText("Reply for c1");

    fireEvent.click(screen.getByText("How much is shipping?"));

    await waitFor(() => expect(screen.getByText("Reply for c2")).toBeInTheDocument());
    expect(screen.queryByText("Reply for c1")).not.toBeInTheDocument();
    expect(screen.queryByText("Hello from c1")).not.toBeInTheDocument();
  });
});
