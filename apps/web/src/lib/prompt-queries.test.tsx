// @vitest-environment happy-dom
import { renderHook, waitFor } from "@testing-library/react";
import { Client, Provider, cacheExchange, fetchExchange } from "urql";
import { afterEach, describe, expect, it, vi } from "vitest";
import { usePromptDetailQuery, usePromptOptionsQuery } from "./prompt-queries";

function stubFetch(data: unknown) {
  const fetchMock = vi.fn(async () =>
    new Response(JSON.stringify({ data }), { headers: { "Content-Type": "application/json" } }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function wrapperFor(client: Client) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return <Provider value={client}>{children}</Provider>;
  };
}

function newClient() {
  return new Client({ url: "http://test/graphql", exchanges: [cacheExchange, fetchExchange] });
}

afterEach(() => vi.unstubAllGlobals());

/**
 * urql's document cache invalidates a result only by the typenames it
 * contains, and an empty list contains none -- so a cached "no agents" or
 * "no prompts" would survive the very mutation that changed it. Both hooks
 * must ask the network again each time a page mounts.
 */
describe("prompt queries", () => {
  it("refetches a prompt's detail on every mount", async () => {
    const fetchMock = stubFetch({
      prompt: { __typename: "Prompt", id: "p1", name: "Sales", key: "sales", description: null, versions: [], agents: [] },
    });
    const client = newClient();
    const first = renderHook(() => usePromptDetailQuery("p1", false), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(first.result.current[0].data).toBeDefined());
    first.unmount();

    const second = renderHook(() => usePromptDetailQuery("p1", false), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    second.unmount();
  });

  it("refetches the prompt options on every mount", async () => {
    const fetchMock = stubFetch({ prompts: [] });
    const client = newClient();
    const first = renderHook(() => usePromptOptionsQuery(false), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(first.result.current[0].data).toBeDefined());
    first.unmount();

    const second = renderHook(() => usePromptOptionsQuery(false), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    second.unmount();
  });
});
