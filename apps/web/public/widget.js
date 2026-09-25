/*
 * AI Sales Agent chat widget loader.
 *
 *   <script src="https://APP/widget.js" data-key="pk_..." async></script>
 *
 * Asks APP/api/v1/widget/<key>/config whether the widget is on and how it
 * looks, then draws a launcher in the owner's colour on the owner's side and,
 * on first open, an iframe onto APP/embed/<key>, which does all the chatting.
 * When the widget is off (or the check fails) it draws nothing at all.
 * Hand-written ES2017 with no dependencies and no build step (spec
 * docs/superpowers/specs/2026-09-25-embeddable-widget-design.md §6).
 *
 * Rules this file keeps, because it runs inside someone else's page:
 * - Everything lives in a closed shadow root, so host CSS cannot restyle it
 *   and host scripts cannot reach into it.
 * - No innerHTML: every node is built with createElement and every string
 *   from the API or a message is set as an attribute or a style property,
 *   never markup. A colour is used only if it is a #rrggbb hex.
 * - Messages are accepted only from the iframe's own window at the app's own
 *   origin; the only thing posted back is {type: "open"}, targeted at it.
 * - The only global is the idempotence guard, window.__saWidgetLoaded.
 * - The launcher is always the visitor's way out: while the panel is open it
 *   is an "x" that closes it, and on a small screen, where the panel covers
 *   the whole page, it sits above the panel -- so a frame that never loads,
 *   hangs or is refused can still be closed.
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
  var DEFAULT_COLOR = "#0f172a";
  var CSS =
    ":host{all:initial;position:fixed;bottom:20px;right:20px;z-index:2147483000}" +
    ":host([data-position=left]){right:auto;left:20px}" +
    ".launcher{position:relative;z-index:1;width:56px;height:56px;border:0;" +
    "border-radius:50%;cursor:pointer;background:#0f172a;color:#fff;display:flex;" +
    "align-items:center;justify-content:center;box-shadow:0 4px 14px rgba(15,23,42,.25)}" +
    ".launcher:focus-visible{outline:2px solid #0f172a;outline-offset:3px}" +
    ".launcher svg{width:26px;height:26px}" +
    "iframe{position:absolute;bottom:72px;right:0;width:400px;height:640px;" +
    "max-height:calc(100vh - 100px);border:0;border-radius:12px;background:transparent;" +
    "box-shadow:0 12px 40px rgba(15,23,42,.25)}" +
    ":host([data-position=left]) iframe{right:auto;left:0}" +
    "iframe[hidden]{display:none}" +
    "@media (max-width:480px){iframe{position:fixed;inset:0;width:100%;height:100%;" +
    "max-height:none;border-radius:0}" +
    // Full screen: the close control moves to the top corner, above the frame.
    ":host([data-open]) .launcher{position:fixed;top:12px;right:16px;bottom:auto;" +
    "left:auto;z-index:2;width:36px;height:36px;box-shadow:0 2px 8px rgba(15,23,42,.3)}" +
    ":host([data-open]) .launcher svg{width:20px;height:20px}}";

  function unavailable() {
    console.info("AI Sales Agent widget: this assistant is not available right now.");
  }

  var request;
  try {
    request = window.fetch(
      appOrigin + "/api/v1/widget/" + encodeURIComponent(key) + "/config",
      { credentials: "omit" },
    );
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
  } catch (e) {
    unavailable();
    return;
  }
  request
    .then(function (response) {
      return response.ok ? response.json() : null;
    })
    .then(
      function (config) {
        if (!config || typeof config !== "object" || config.available !== true) {
          unavailable();
          return;
        }
        draw(config);
      },
      function () {
        unavailable();
      },
    );

  function svgIcon(d) {
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
    path.setAttribute("d", d);
    svg.appendChild(path);
    return svg;
  }

  var CHAT_ICON = "M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z";
  var CLOSE_ICON = "M6 6l12 12M18 6L6 18";

  function draw(config) {
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
    launcher.setAttribute("aria-expanded", "false");
    root.appendChild(launcher);

    var iframe = null;
    var openLabel = "Open chat";

    function applyLook(color, position, title) {
      if (typeof color === "string" && HEX_COLOR.test(color)) {
        launcher.style.backgroundColor = color;
      }
      if (position === "left" || position === "right") {
        host.setAttribute("data-position", position);
      }
      if (typeof title === "string" && title) {
        openLabel = "Open chat: " + title.slice(0, 80);
      }
      renderLauncher();
    }

    function isOpen() {
      return !!iframe && !iframe.hidden;
    }

    function renderLauncher() {
      var open = isOpen();
      while (launcher.firstChild) launcher.removeChild(launcher.firstChild);
      launcher.appendChild(svgIcon(open ? CLOSE_ICON : CHAT_ICON));
      launcher.setAttribute("aria-label", open ? "Close chat" : openLabel);
      launcher.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) host.setAttribute("data-open", "");
      else host.removeAttribute("data-open");
    }

    // Targeted at the app's origin, so a post made before the embed page has
    // loaded (the frame is still about:blank) is simply dropped by the
    // browser; the load listener below covers the first open.
    function tellFrameOpened() {
      if (iframe && iframe.contentWindow) {
        iframe.contentWindow.postMessage({ type: "open" }, appOrigin);
      }
    }

    function setOpen(open) {
      if (!iframe) return;
      iframe.hidden = !open;
      renderLauncher();
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
        // The frame's copy is the freshest (the config above may be up to a
        // minute old), so it wins.
        applyLook(data.brand_color, data.position, data.title);
      }
    });

    applyLook(
      typeof config.brand_color === "string" && HEX_COLOR.test(config.brand_color)
        ? config.brand_color
        : DEFAULT_COLOR,
      config.position,
      config.title,
    );
    (document.body || document.documentElement).appendChild(host);
  }
})();
