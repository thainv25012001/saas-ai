// @vitest-environment happy-dom
/**
 * The loader in `public/widget.js` is plain, un-bundled browser JavaScript,
 * so it is tested the way a host page runs it: its source text evaluated in
 * the page, with `document.currentScript` pointing at a `<script>` carrying
 * `data-key`. Its shadow root is closed, so `attachShadow` is wrapped to keep
 * a handle on it -- exactly the handle no host-page script ever gets.
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// A string, not `new URL(...)`: under happy-dom `URL` is the DOM's, which
// Node's `fileURLToPath` refuses.
const LOADER = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), "../../public/widget.js"),
  "utf8",
);
const APP_ORIGIN = "https://app.example.com";

type FakeFrameWindow = { postMessage: ReturnType<typeof vi.fn> };

let roots: ShadowRoot[];
let frameWindows: WeakMap<HTMLIFrameElement, FakeFrameWindow>;
let originalAttachShadow: typeof Element.prototype.attachShadow;
const originalContentWindow = Object.getOwnPropertyDescriptor(
  HTMLIFrameElement.prototype,
  "contentWindow",
);

function runLoader(attributes: Record<string, string>) {
  // Unattached, so happy-dom never tries to fetch it.
  const script = document.createElement("script");
  for (const [name, value] of Object.entries(attributes)) script.setAttribute(name, value);
  Object.defineProperty(document, "currentScript", { value: script, configurable: true });
  new Function(LOADER)();
  Object.defineProperty(document, "currentScript", { value: null, configurable: true });
}

function hosts() {
  return document.querySelectorAll("[data-sa-widget]");
}

function root(): ShadowRoot {
  expect(roots).toHaveLength(1);
  return roots[0];
}

function launcher(): HTMLButtonElement {
  const button = root().querySelector("button");
  if (!button) throw new Error("no launcher");
  return button;
}

function iframe(): HTMLIFrameElement | null {
  return root().querySelector("iframe");
}

function post(data: unknown, init: { origin?: string; source?: unknown } = {}) {
  window.dispatchEvent(
    new MessageEvent("message", {
      data,
      origin: init.origin ?? APP_ORIGIN,
      source: (init.source ?? null) as Window | null,
    }),
  );
}

beforeEach(() => {
  roots = [];
  frameWindows = new WeakMap();
  originalAttachShadow = Element.prototype.attachShadow;
  Element.prototype.attachShadow = function (this: Element, init: ShadowRootInit) {
    const shadow = originalAttachShadow.call(this, init);
    roots.push(shadow);
    return shadow;
  };
  // A cross-origin frame's window, without happy-dom loading the page.
  Object.defineProperty(HTMLIFrameElement.prototype, "contentWindow", {
    configurable: true,
    get(this: HTMLIFrameElement) {
      let fake = frameWindows.get(this);
      if (!fake) {
        fake = { postMessage: vi.fn() };
        frameWindows.set(this, fake);
      }
      return fake;
    },
  });
  const settings = (window as unknown as { happyDOM: { settings: Record<string, unknown> } })
    .happyDOM.settings;
  settings.disableIframePageLoading = true;
  // happy-dom reports the refused page load through console.error.
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  Element.prototype.attachShadow = originalAttachShadow;
  if (originalContentWindow) {
    Object.defineProperty(HTMLIFrameElement.prototype, "contentWindow", originalContentWindow);
  }
  delete (window as { __saWidgetLoaded?: boolean }).__saWidgetLoaded;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("widget.js", () => {
  it("creates one host element with a closed shadow root and a launcher", () => {
    runLoader({ src: `${APP_ORIGIN}/widget.js`, "data-key": "pk_abc" });

    expect(hosts()).toHaveLength(1);
    expect(launcher().getAttribute("aria-label")).toBe("Open chat");
    expect(iframe()).toBeNull();
  });

  it("does nothing on a second load", () => {
    runLoader({ src: `${APP_ORIGIN}/widget.js`, "data-key": "pk_abc" });
    runLoader({ src: `${APP_ORIGIN}/widget.js`, "data-key": "pk_abc" });

    expect(hosts()).toHaveLength(1);
    expect(roots).toHaveLength(1);
  });

  it("finds its script by selector when currentScript is unavailable", () => {
    const script = document.createElement("script");
    script.setAttribute("data-key", "pk_sel");
    // `type` keeps happy-dom from trying to fetch and run it.
    script.setAttribute("type", "text/plain");
    script.setAttribute("src", `${APP_ORIGIN}/widget.js`);
    document.body.appendChild(script);
    Object.defineProperty(document, "currentScript", { value: null, configurable: true });

    new Function(LOADER)();

    expect(hosts()).toHaveLength(1);
  });

  it("opens the embed page for its key on the first click, and toggles after", () => {
    runLoader({ src: `${APP_ORIGIN}/widget.js`, "data-key": "pk_abc" });

    launcher().click();

    const frame = iframe();
    expect(frame).not.toBeNull();
    expect(frame?.getAttribute("src")).toBe(`${APP_ORIGIN}/embed/pk_abc`);
    expect(frame?.getAttribute("title")).toBe("Chat");
    expect(frame?.getAttribute("allow")).toBe("");
    expect(frame?.getAttribute("referrerpolicy")).toBe("strict-origin-when-cross-origin");
    expect(launcher().getAttribute("aria-expanded")).toBe("true");

    launcher().click();
    expect(frame?.hidden).toBe(true);
    expect(launcher().getAttribute("aria-expanded")).toBe("false");

    launcher().click();
    expect(root().querySelectorAll("iframe")).toHaveLength(1);
    expect(frame?.hidden).toBe(false);
    // Reopening tells the frame, targeted at the app's origin only.
    expect(frameWindows.get(frame as HTMLIFrameElement)?.postMessage).toHaveBeenCalledWith(
      { type: "open" },
      APP_ORIGIN,
    );
  });

  it("hides the frame on a close from the frame itself", () => {
    runLoader({ src: `${APP_ORIGIN}/widget.js`, "data-key": "pk_abc" });
    launcher().click();
    const frame = iframe() as HTMLIFrameElement;

    post({ type: "close" }, { source: frame.contentWindow });

    expect(frame.hidden).toBe(true);
    expect(launcher().getAttribute("aria-expanded")).toBe("false");
  });

  it("ignores a message from the wrong origin", () => {
    runLoader({ src: `${APP_ORIGIN}/widget.js`, "data-key": "pk_abc" });
    launcher().click();
    const frame = iframe() as HTMLIFrameElement;

    post({ type: "close" }, { origin: "https://evil.example", source: frame.contentWindow });

    expect(frame.hidden).toBe(false);
  });

  it("ignores a message from the right origin but another window", () => {
    runLoader({ src: `${APP_ORIGIN}/widget.js`, "data-key": "pk_abc" });
    launcher().click();
    const frame = iframe() as HTMLIFrameElement;

    post({ type: "close" }, { source: window });
    post({ type: "close" }, { source: null });

    expect(frame.hidden).toBe(false);
  });

  it("applies the brand colour and side from ready, rejecting a non-hex colour", () => {
    runLoader({ src: `${APP_ORIGIN}/widget.js`, "data-key": "pk_abc" });
    launcher().click();
    const frame = iframe() as HTMLIFrameElement;
    const host = hosts()[0] as HTMLElement;

    post(
      { type: "ready", brand_color: "#0F766E", position: "left", title: "Ask Acme" },
      { source: frame.contentWindow },
    );
    expect(launcher().style.backgroundColor).not.toBe("");
    expect(host.getAttribute("data-position")).toBe("left");
    expect(launcher().getAttribute("aria-label")).toBe("Open chat: Ask Acme");

    const before = launcher().style.backgroundColor;
    post(
      { type: "ready", brand_color: "red;background:url(x)", position: "top" },
      { source: frame.contentWindow },
    );
    expect(launcher().style.backgroundColor).toBe(before);
    expect(host.getAttribute("data-position")).toBe("left");
  });

  it("warns once and draws nothing without data-key", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    runLoader({ src: `${APP_ORIGIN}/widget.js` });

    expect(hosts()).toHaveLength(0);
    expect(roots).toHaveLength(0);
    expect(warn).toHaveBeenCalledTimes(1);
  });
});
