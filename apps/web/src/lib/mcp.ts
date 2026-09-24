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

/**
 * Wraps `value` in POSIX single quotes so it reaches the shell as one
 * literal argument regardless of what it contains -- spaces, `"`, `` ` ``,
 * `$(...)`, or a single quote of its own. Single quotes admit no escape
 * sequence at all, so the one character that needs special handling is a
 * literal `'`: it ends the quoted string, contributes an escaped quote
 * (`'\''`), and reopens a new quoted string, which is the standard POSIX
 * idiom for "a single quote inside single quotes".
 */
function shellQuote(value: string): string {
  return `'${value.replace(/'/g, "'\\''")}'`;
}

/** The one-line command that registers this MCP server with the Claude Code
 * CLI. `url` and the whole `Authorization: Bearer <token>` header value are
 * each shell-quoted independently of what they contain -- the token is
 * server-generated and the url comes from config today, but the function
 * does not lean on either of those as a safety net. `name` is slugified
 * first and left unquoted, since a slug is already a safe bare argument by
 * construction (`slugifyMcpName` only ever emits `[a-z0-9-]`). */
export function claudeMcpAddCommand(name: string, url: string, token: string): string {
  const slug = slugifyMcpName(name);
  const header = `Authorization: Bearer ${token}`;
  return `claude mcp add --transport http ${slug} ${shellQuote(url)} --header ${shellQuote(header)}`;
}

/** The equivalent `mcpServers` block in the `.mcp.json` format -- read by
 * Claude Code, Cursor and other clients that speak MCP over HTTP with
 * custom headers. Not Claude Desktop: its config file does not take a remote
 * HTTP server with headers (docs/PHASE-7.md §9). `JSON.stringify`
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

/** The `/mcp` endpoint under the API's base url. A trailing slash on the
 * configured url (`NEXT_PUBLIC_API_URL=https://api.example.com/`) would
 * otherwise produce `//mcp`, which the API does not route. */
export function mcpEndpointUrl(apiUrl: string): string {
  return `${apiUrl.replace(/\/+$/, "")}/mcp`;
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
