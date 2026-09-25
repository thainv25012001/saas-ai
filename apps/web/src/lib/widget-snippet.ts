/**
 * The one-line loader snippet a dashboard owner pastes into their site
 * (spec §7), and the reverse of the domains textarea: turning what someone
 * typed, one origin per line, into the list `updateWidgetSettings` takes.
 */

/** Both attribute values below can carry whatever the caller passes in --
 * `appOrigin` is `window.location.origin` today, but `publicKey` is server
 * data and this function makes no assumption that either is already safe to
 * drop into an HTML attribute. */
function escapeAttribute(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/** The exact `<script>` tag the widget card shows and the copy button
 * copies. A trailing slash on `appOrigin` (a pasted `https://example.com/`)
 * would otherwise produce `//widget.js`. */
export function widgetSnippet(appOrigin: string, publicKey: string): string {
  const origin = appOrigin.replace(/\/+$/, "");
  return `<script src="${escapeAttribute(origin)}/widget.js" data-key="${escapeAttribute(
    publicKey,
  )}" async></script>`;
}

/** The allowed-domains textarea holds one origin per line, but a pasted list
 * commonly carries commas too -- both are accepted. Blank lines (including a
 * trailing one from the last newline) are dropped rather than sent on to
 * `updateWidgetSettings`, which normalizes and validates every remaining
 * entry server-side. */
export function parseOriginLines(text: string): string[] {
  return text
    .split(/[\n,]+/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
}
