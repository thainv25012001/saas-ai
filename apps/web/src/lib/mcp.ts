/**
 * Helpers for the agent page's MCP access card (docs/PHASE-7.md §6): turning
 * a freshly created key into the two copy-ready snippets a person pastes
 * into their own MCP client, plus the one clipboard helper both buttons and
 * the token's own "Copy" button share.
 *
 * Kept here rather than inline in the component so the quoting and slugging
 * rules -- easy to get subtly wrong -- are each written, and tested, once.
 */

/**
 * A key's own name, as typed into the create form, becomes the identifier a
 * local MCP client uses for this server -- both as the `claude mcp add`
 * positional argument and as the JSON config's `mcpServers` key. Neither
 * accepts arbitrary text: `claude mcp add` takes a bare shell argument (a
 * space would end it early) and a JSON object key that could be anything is
 * a worse identifier than a short slug. Non-`[a-z0-9-]` runs collapse to one
 * `-`, and a name with nothing left over (all punctuation, or empty) falls
 * back to `agent` rather than producing an empty or all-dash identifier.
 */
export function slugifyMcpName(name: string): string {
  const slug = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
  return slug || "agent";
}

/** The one-line command that registers this MCP server with the Claude Code
 * CLI. The token sits inside a double-quoted shell argument, which is why
 * `name` is slugified first -- an unquoted, unslugged name could break the
 * argument list in ways this string can't recover from. */
export function claudeMcpAddCommand(name: string, url: string, token: string): string {
  const slug = slugifyMcpName(name);
  return `claude mcp add --transport http ${slug} ${url} --header "Authorization: Bearer ${token}"`;
}

/** The equivalent `mcpServers` JSON block for a client (e.g. Claude Desktop)
 * that reads its MCP config from a file rather than a CLI. `JSON.stringify`
 * -- not string concatenation -- is what makes the token and url safe inside
 * the JSON string regardless of what characters they contain. */
export function mcpJsonConfig(name: string, url: string, token: string): string {
  const slug = slugifyMcpName(name);
  const config = {
    mcpServers: {
      [slug]: {
        type: "http",
        url,
        headers: { Authorization: `Bearer ${token}` },
      },
    },
  };
  return JSON.stringify(config, null, 2);
}

/**
 * Copies `text` to the clipboard, reporting whether it actually worked.
 * `navigator.clipboard` does not exist over plain HTTP, in some embedded
 * webviews, or in a test's jsdom/happy-dom environment, and `writeText`
 * itself can reject (permission denied) even where the API exists -- both
 * are the caller's cue to show "couldn't copy, select the text yourself"
 * rather than silently claim success.
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  if (typeof navigator === "undefined" || !navigator.clipboard?.writeText) return false;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
