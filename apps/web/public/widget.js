/*
 * AI Sales Agent chat widget loader.
 *
 *   <script src="https://APP/widget.js" data-key="pk_..." async></script>
 *
 * Draws a launcher on the host page and, on first open, an iframe onto
 * APP/embed/<key>, which does all the chatting. Hand-written ES2017 with no
 * dependencies and no build step (spec
 * docs/superpowers/specs/2026-09-25-embeddable-widget-design.md §6).
 *
 * Rules this file keeps, because it runs inside someone else's page:
 * - Everything lives in a closed shadow root, so host CSS cannot restyle it
 *   and host scripts cannot reach into it.
 * - No innerHTML: every node is built with createElement and every string
 *   from a message is set as an attribute or a style property, never markup.
 * - Messages are accepted only from the iframe's own window at the app's own
 *   origin; the only thing posted back is {type: "open"}, targeted at it.
 * - The only global is the idempotence guard, window.__saWidgetLoaded.
 */
(function () {
  "use strict";

  if (window.__saWidgetLoaded) return;

  var script =
    document.currentScript ||
    document.querySelector('script[data-key][src$="/widget.js"]');
  var key = script ? script.getAttribute("data-key") : null;
  if (!script || !key) {
    console.warn("AI Sales Agent widget: the <script> tag needs a data-key attribute.");
    return;
  }

  var appOrigin;
  try {
    appOrigin = new URL(script.getAttribute("src") || "", window.location.href).origin;
    // A binding is required: optional catch binding is ES2019, this is ES2017.
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
  } catch (e) {
    console.warn("AI Sales Agent widget: could not read the script's src.");
    return;
  }
  window.__saWidgetLoaded = true;

  var HEX_COLOR = /^#[0-9a-fA-F]{6}$/;
  var CSS =
    ":host{all:initial;position:fixed;bottom:20px;right:20px;z-index:2147483000}" +
    ":host([data-position=left]){right:auto;left:20px}" +
    ".launcher{width:56px;height:56px;border:0;border-radius:50%;cursor:pointer;" +
    "background:#0f172a;color:#fff;display:flex;align-items:center;justify-content:center;" +
    "box-shadow:0 4px 14px rgba(15,23,42,.25)}" +
    ".launcher:focus-visible{outline:2px solid #0f172a;outline-offset:3px}" +
    ".launcher svg{width:26px;height:26px}" +
    "iframe{position:absolute;bottom:72px;right:0;width:400px;height:640px;" +
    "max-height:calc(100vh - 100px);border:0;border-radius:12px;background:transparent;" +
    "box-shadow:0 12px 40px rgba(15,23,42,.25)}" +
    ":host([data-position=left]) iframe{right:auto;left:0}" +
    "iframe[hidden]{display:none}" +
    "@media (max-width:480px){iframe{position:fixed;inset:0;width:100%;height:100%;" +
    "max-height:none;border-radius:0}}";

  var host = document.createElement("div");
  host.setAttribute("data-sa-widget", "");
  host.setAttribute("data-position", "right");
  var root = host.attachShadow({ mode: "closed" });

  var style = document.createElement("style");
  style.textContent = CSS;
  root.appendChild(style);

  var launcher = document.createElement("button");
  launcher.type = "button";
  launcher.className = "launcher";
  launcher.setAttribute("aria-label", "Open chat");
  launcher.setAttribute("aria-expanded", "false");
  launcher.appendChild(chatIcon());
  root.appendChild(launcher);

  var iframe = null;

  function chatIcon() {
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.75");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    var path = document.createElementNS(ns, "path");
    path.setAttribute("d", "M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z");
    svg.appendChild(path);
    return svg;
  }

  // Targeted at the app's origin, so a post made before the embed page has
  // loaded (the frame is still about:blank) is simply dropped by the browser;
  // the load listener below covers the first open.
  function tellFrameOpened() {
    if (iframe && iframe.contentWindow) {
      iframe.contentWindow.postMessage({ type: "open" }, appOrigin);
    }
  }

  function setOpen(open) {
    if (!iframe) return;
    iframe.hidden = !open;
    launcher.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) tellFrameOpened();
  }

  launcher.addEventListener("click", function () {
    if (!iframe) {
      iframe = document.createElement("iframe");
      iframe.setAttribute("src", appOrigin + "/embed/" + encodeURIComponent(key));
      iframe.setAttribute("title", "Chat");
      iframe.setAttribute("allow", "");
      iframe.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");
      iframe.addEventListener("load", function () {
        if (!iframe.hidden) tellFrameOpened();
      });
      root.appendChild(iframe);
      setOpen(true);
      return;
    }
    setOpen(iframe.hidden);
  });

  window.addEventListener("message", function (event) {
    if (event.origin !== appOrigin) return;
    if (!iframe || !iframe.contentWindow || event.source !== iframe.contentWindow) return;
    var data = event.data;
    if (!data || typeof data !== "object" || typeof data.type !== "string") return;

    if (data.type === "close") {
      setOpen(false);
    } else if (data.type === "ready") {
      if (typeof data.brand_color === "string" && HEX_COLOR.test(data.brand_color)) {
        launcher.style.backgroundColor = data.brand_color;
      }
      if (data.position === "left" || data.position === "right") {
        host.setAttribute("data-position", data.position);
      }
      if (typeof data.title === "string" && data.title) {
        launcher.setAttribute("aria-label", "Open chat: " + data.title.slice(0, 80));
      }
    }
  });

  (document.body || document.documentElement).appendChild(host);
})();
