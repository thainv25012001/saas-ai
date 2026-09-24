// @vitest-environment happy-dom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { McpAccessCard, type ApiKeyRow } from "./McpAccessCard";

function key(overrides: Partial<ApiKeyRow> = {}): ApiKeyRow {
  return {
    id: "k1",
    name: "Zapier integration",
    keyPrefix: "sa_mcp_3f9a1c2e",
    createdByName: "Jamie Chen",
    createdAt: "2026-01-01T00:00:00Z",
    lastUsedAt: null,
    revokedAt: null,
    ...overrides,
  };
}

const ENDPOINT = "http://localhost:8000/mcp";

describe("McpAccessCard", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows the endpoint and the exposed tool names", () => {
    render(
      <McpAccessCard
        endpointUrl={ENDPOINT}
        exposedToolNames={["search_products", "retrieve_knowledge"]}
        keys={[]}
        canManageKeys
        onCreate={vi.fn()}
        onRevoke={vi.fn()}
      />,
    );
    expect(screen.getByText(ENDPOINT)).toBeInTheDocument();
    expect(screen.getByText("search_products")).toBeInTheDocument();
    expect(screen.getByText("retrieve_knowledge")).toBeInTheDocument();
  });

  it("says no tools are exposed rather than rendering an empty list silently", () => {
    render(
      <McpAccessCard
        endpointUrl={ENDPOINT}
        exposedToolNames={[]}
        keys={[]}
        canManageKeys
        onCreate={vi.fn()}
        onRevoke={vi.fn()}
      />,
    );
    expect(screen.getByText(/no tools are exposed yet/i)).toBeInTheDocument();
  });

  it("says there are no API keys rather than rendering an empty list silently", () => {
    render(
      <McpAccessCard
        endpointUrl={ENDPOINT}
        exposedToolNames={[]}
        keys={[]}
        canManageKeys
        onCreate={vi.fn()}
        onRevoke={vi.fn()}
      />,
    );
    expect(screen.getByText(/no api keys yet/i)).toBeInTheDocument();
  });

  it("lists a key's name, prefix, creator, created and last-used-never", () => {
    render(
      <McpAccessCard
        endpointUrl={ENDPOINT}
        exposedToolNames={[]}
        keys={[key()]}
        canManageKeys
        onCreate={vi.fn()}
        onRevoke={vi.fn()}
      />,
    );
    expect(screen.getByText("Zapier integration")).toBeInTheDocument();
    expect(screen.getByText("sa_mcp_3f9a1c2e…")).toBeInTheDocument();
    expect(screen.getByText(/Jamie Chen/)).toBeInTheDocument();
    expect(screen.getByText(/Never/)).toBeInTheDocument();
  });

  it("renders a key's own name as text, never as markup", () => {
    const { container } = render(
      <McpAccessCard
        endpointUrl={ENDPOINT}
        exposedToolNames={[]}
        keys={[key({ name: '<script>alert("x")</script>' })]}
        canManageKeys
        onCreate={vi.fn()}
        onRevoke={vi.fn()}
      />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getAllByText('<script>alert("x")</script>').length).toBeGreaterThan(0);
  });

  it("hides the create form and every Revoke button for a member", () => {
    render(
      <McpAccessCard
        endpointUrl={ENDPOINT}
        exposedToolNames={[]}
        keys={[key()]}
        canManageKeys={false}
        onCreate={vi.fn()}
        onRevoke={vi.fn()}
      />,
    );
    expect(screen.queryByLabelText(/new key name/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Revoke" })).not.toBeInTheDocument();
    // The list itself is still visible to a member.
    expect(screen.getByText("Zapier integration")).toBeInTheDocument();
  });

  it("reveals the token once on create, with copy-ready snippets, and clears it on dismiss", async () => {
    const onCreate = vi.fn().mockResolvedValue({ name: "Zapier integration", token: "sa_mcp_secret" });
    render(
      <McpAccessCard
        endpointUrl={ENDPOINT}
        exposedToolNames={[]}
        keys={[]}
        canManageKeys
        onCreate={onCreate}
        onRevoke={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByLabelText(/new key name/i), {
      target: { value: "Zapier integration" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create key" }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledWith("Zapier integration"));
    await waitFor(() => expect(screen.getByText("sa_mcp_secret")).toBeInTheDocument());

    expect(screen.getByText(/will not be shown again/i)).toBeInTheDocument();
    expect(
      screen.getByText(
        'claude mcp add --transport http zapier-integration http://localhost:8000/mcp --header "Authorization: Bearer sa_mcp_secret"',
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/"mcpServers"/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Saved it — dismiss" }));

    expect(screen.queryByText("sa_mcp_secret")).not.toBeInTheDocument();
    expect(screen.queryByText(/will not be shown again/i)).not.toBeInTheDocument();
  });

  it("does not reveal a panel when create fails (the page turns the error into createError)", async () => {
    const onCreate = vi.fn().mockResolvedValue(null);
    render(
      <McpAccessCard
        endpointUrl={ENDPOINT}
        exposedToolNames={[]}
        keys={[]}
        canManageKeys
        createError="agent already has 10 active API keys"
        onCreate={onCreate}
        onRevoke={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText(/new key name/i), { target: { value: "one more" } });
    fireEvent.click(screen.getByRole("button", { name: "Create key" }));
    await waitFor(() => expect(onCreate).toHaveBeenCalled());
    expect(screen.getByText("agent already has 10 active API keys")).toBeInTheDocument();
    expect(screen.queryByText(/will not be shown again/i)).not.toBeInTheDocument();
  });

  it("asks for confirmation before revoking, and does not call onRevoke when declined", () => {
    const onRevoke = vi.fn();
    vi.spyOn(window, "confirm").mockReturnValue(false);
    render(
      <McpAccessCard
        endpointUrl={ENDPOINT}
        exposedToolNames={[]}
        keys={[key()]}
        canManageKeys
        onCreate={vi.fn()}
        onRevoke={onRevoke}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
    expect(window.confirm).toHaveBeenCalled();
    expect(onRevoke).not.toHaveBeenCalled();
  });

  it("calls onRevoke with the key's id once confirmed", () => {
    const onRevoke = vi.fn();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(
      <McpAccessCard
        endpointUrl={ENDPOINT}
        exposedToolNames={[]}
        keys={[key({ id: "k9" })]}
        canManageKeys
        onCreate={vi.fn()}
        onRevoke={onRevoke}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
    expect(onRevoke).toHaveBeenCalledWith("k9");
  });

  it("shows a Revoked badge and no Revoke button for a revoked key", () => {
    render(
      <McpAccessCard
        endpointUrl={ENDPOINT}
        exposedToolNames={[]}
        keys={[key({ revokedAt: "2026-02-01T00:00:00Z" })]}
        canManageKeys
        onCreate={vi.fn()}
        onRevoke={vi.fn()}
      />,
    );
    expect(screen.getByText("Revoked")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Revoke" })).not.toBeInTheDocument();
  });

  it("surfaces a server permission error even though the control that caused it is hidden client-side", () => {
    render(
      <McpAccessCard
        endpointUrl={ENDPOINT}
        exposedToolNames={[]}
        keys={[key()]}
        canManageKeys={false}
        revokeError="owner or admin role required"
        onCreate={vi.fn()}
        onRevoke={vi.fn()}
      />,
    );
    expect(screen.getByText("owner or admin role required")).toBeInTheDocument();
  });

  it("shows a loading state on only the key currently mid-revoke", () => {
    render(
      <McpAccessCard
        endpointUrl={ENDPOINT}
        exposedToolNames={[]}
        keys={[key({ id: "k1" }), key({ id: "k2", name: "Second key" })]}
        revokingId="k2"
        canManageKeys
        onCreate={vi.fn()}
        onRevoke={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Revoke" })).not.toHaveAttribute("aria-busy");
    expect(screen.getByText("Revoking…")).toBeInTheDocument();
  });
});
