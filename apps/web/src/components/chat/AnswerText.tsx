import Markdown, { type Components } from "react-markdown";

/**
 * The only elements an assistant answer may become. Everything else is
 * unwrapped to its text (`unwrapDisallowed`), so nothing the model writes
 * is ever lost -- it just loses its formatting.
 *
 * Deliberately absent:
 * - `a`: a document or a visitor can steer the model into printing a
 *   phishing link; the link text stays, the click target does not.
 * - `img`: rendering one makes the visitor's browser fetch a URL the model
 *   chose, which is a data-exfiltration channel. An image has no children,
 *   so unwrapping drops it entirely.
 * - raw HTML: react-markdown never turns it into elements unless
 *   `rehype-raw` is added, which it must not be. `<script>` in an answer
 *   prints as those literal characters.
 */
const ALLOWED_ELEMENTS = [
  "p",
  "br",
  "strong",
  "em",
  "ol",
  "ul",
  "li",
  "code",
  "pre",
  "blockquote",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
];

// A chat bubble has no room for a page-sized heading, so every level reads
// as a bold line, the same weight the model would get from `**...**`.
const heading: Components["h1"] = ({ children }) => (
  <p className="font-semibold">{children}</p>
);

const components: Components = {
  // `pre-wrap` keeps the model's single newlines, which markdown would
  // otherwise fold into one line -- models use them for line-per-item lists.
  p: ({ children }) => <p className="whitespace-pre-wrap">{children}</p>,
  ol: ({ children }) => <ol className="list-decimal space-y-1 pl-5">{children}</ol>,
  ul: ({ children }) => <ul className="list-disc space-y-1 pl-5">{children}</ul>,
  code: ({ children }) => (
    <code className="rounded bg-surface-muted px-1 py-0.5 font-mono text-[0.85em]">{children}</code>
  ),
  pre: ({ children }) => (
    <pre className="overflow-x-auto rounded bg-surface-muted p-2 [&_code]:bg-transparent [&_code]:p-0">
      {children}
    </pre>
  ),
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 border-line pl-3 text-ink-muted">{children}</blockquote>
  ),
  h1: heading,
  h2: heading,
  h3: heading,
  h4: heading,
  h5: heading,
  h6: heading,
};

/**
 * An assistant answer, rendered as a small, safe subset of markdown.
 *
 * This is the one exception to "untrusted text is JSX text only"
 * (docs/DESIGN.md, "Untrusted text"): models write markdown by default, and
 * printing `**` verbatim made every answer look broken. The exception is
 * held to `ALLOWED_ELEMENTS` above. User messages, citations, tool calls and
 * every other untrusted field stay plain text.
 */
export function AnswerText({ text }: { text: string }) {
  return (
    <div className="space-y-2 break-words">
      <Markdown allowedElements={ALLOWED_ELEMENTS} unwrapDisallowed components={components}>
        {text}
      </Markdown>
    </div>
  );
}
