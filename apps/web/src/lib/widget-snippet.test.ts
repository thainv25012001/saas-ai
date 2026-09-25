import { describe, expect, it } from "vitest";
import { parseOriginLines, widgetSnippet } from "./widget-snippet";

describe("widgetSnippet", () => {
  it("builds the exact loader tag for an origin and a public key", () => {
    expect(widgetSnippet("https://example.com", "pk_abc123")).toBe(
      '<script src="https://example.com/widget.js" data-key="pk_abc123" async></script>',
    );
  });

  it("trims a trailing slash off the origin", () => {
    expect(widgetSnippet("https://example.com/", "pk_abc123")).toBe(
      '<script src="https://example.com/widget.js" data-key="pk_abc123" async></script>',
    );
  });

  it("trims more than one trailing slash", () => {
    expect(widgetSnippet("https://example.com//", "pk_abc123")).toBe(
      '<script src="https://example.com/widget.js" data-key="pk_abc123" async></script>',
    );
  });

  it("HTML-escapes the origin attribute value", () => {
    expect(widgetSnippet('https://example.com"><script>alert(1)</script>', "pk_abc123")).toBe(
      '<script src="https://example.com&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;/widget.js" data-key="pk_abc123" async></script>',
    );
  });

  it("HTML-escapes the public key attribute value", () => {
    expect(widgetSnippet("https://example.com", 'pk_"><script>alert(1)</script>')).toBe(
      '<script src="https://example.com/widget.js" data-key="pk_&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;" async></script>',
    );
  });

  it("escapes an ampersand", () => {
    expect(widgetSnippet("https://example.com", "pk_a&b")).toBe(
      '<script src="https://example.com/widget.js" data-key="pk_a&amp;b" async></script>',
    );
  });
});

describe("parseOriginLines", () => {
  it("splits on newlines", () => {
    expect(parseOriginLines("https://a.com\nhttps://b.com")).toEqual([
      "https://a.com",
      "https://b.com",
    ]);
  });

  it("splits on commas", () => {
    expect(parseOriginLines("https://a.com, https://b.com")).toEqual([
      "https://a.com",
      "https://b.com",
    ]);
  });

  it("splits on a mix of newlines and commas", () => {
    expect(parseOriginLines("https://a.com,\nhttps://b.com\nhttps://c.com,https://d.com")).toEqual([
      "https://a.com",
      "https://b.com",
      "https://c.com",
      "https://d.com",
    ]);
  });

  it("trims each entry", () => {
    expect(parseOriginLines("  https://a.com  \n  https://b.com  ")).toEqual([
      "https://a.com",
      "https://b.com",
    ]);
  });

  it("drops blank lines", () => {
    expect(parseOriginLines("https://a.com\n\n\nhttps://b.com\n")).toEqual([
      "https://a.com",
      "https://b.com",
    ]);
  });

  it("returns an empty array for blank input", () => {
    expect(parseOriginLines("   \n  \n")).toEqual([]);
    expect(parseOriginLines("")).toEqual([]);
  });
});
