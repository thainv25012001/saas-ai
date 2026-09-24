import { afterEach, describe, expect, it, vi } from "vitest";
import { claudeMcpAddCommand, copyToClipboard, mcpJsonConfig, slugifyMcpName } from "./mcp";

describe("slugifyMcpName", () => {
  it("lowercases and keeps a name already made of a-z0-9- unchanged", () => {
    expect(slugifyMcpName("zapier-integration-1")).toBe("zapier-integration-1");
  });

  it("collapses spaces and punctuation to single dashes", () => {
    expect(slugifyMcpName("Zapier Integration!!")).toBe("zapier-integration");
  });

  it("trims leading and trailing dashes left by leading/trailing punctuation", () => {
    expect(slugifyMcpName("  (support bot)  ")).toBe("support-bot");
  });

  it("falls back to a fixed slug when nothing valid is left", () => {
    expect(slugifyMcpName("!!!")).toBe("agent");
    expect(slugifyMcpName("")).toBe("agent");
  });
});

describe("claudeMcpAddCommand", () => {
  it("builds the exact CLI command, slugging the name and quoting the token", () => {
    expect(claudeMcpAddCommand("Support Bot", "http://localhost:8000/mcp", "sa_mcp_abc123")).toBe(
      'claude mcp add --transport http support-bot http://localhost:8000/mcp --header "Authorization: Bearer sa_mcp_abc123"',
    );
  });

  it("never lets an unsafe name character reach the shell argument", () => {
    const command = claudeMcpAddCommand(
      "my server; rm -rf /",
      "http://localhost:8000/mcp",
      "sa_mcp_x",
    );
    expect(command).toBe(
      'claude mcp add --transport http my-server-rm-rf http://localhost:8000/mcp --header "Authorization: Bearer sa_mcp_x"',
    );
  });
});

describe("mcpJsonConfig", () => {
  it("builds pretty JSON with the slugged name as the mcpServers key", () => {
    const json = mcpJsonConfig("Support Bot", "http://localhost:8000/mcp", "sa_mcp_abc123");
    expect(json).toBe(
      JSON.stringify(
        {
          mcpServers: {
            "support-bot": {
              type: "http",
              url: "http://localhost:8000/mcp",
              headers: { Authorization: "Bearer sa_mcp_abc123" },
            },
          },
        },
        null,
        2,
      ),
    );
    expect(JSON.parse(json)).toEqual({
      mcpServers: {
        "support-bot": {
          type: "http",
          url: "http://localhost:8000/mcp",
          headers: { Authorization: "Bearer sa_mcp_abc123" },
        },
      },
    });
  });

  it("keeps a token containing quotes and backslashes valid JSON", () => {
    const json = mcpJsonConfig("bot", "http://localhost:8000/mcp", 'weird"token\\here');
    expect(() => JSON.parse(json)).not.toThrow();
    expect(JSON.parse(json).mcpServers.bot.headers.Authorization).toBe(
      'Bearer weird"token\\here',
    );
  });
});

describe("copyToClipboard", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("resolves true when the clipboard API succeeds", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    await expect(copyToClipboard("hello")).resolves.toBe(true);
    expect(writeText).toHaveBeenCalledWith("hello");
  });

  it("resolves false when the clipboard API is unavailable", async () => {
    vi.stubGlobal("navigator", {});

    await expect(copyToClipboard("hello")).resolves.toBe(false);
  });

  it("resolves false, rather than throwing, when writeText rejects", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    await expect(copyToClipboard("hello")).resolves.toBe(false);
  });
});
