import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchFrameOrigins, frameAncestorsHeader, isBareOrigin } from "./frame-policy";

describe("isBareOrigin", () => {
  it.each([
    "https://shop.example.com",
    "http://localhost:3000",
    "https://A.Example.COM",
    "https://127.0.0.1:8443",
  ])("accepts %s", (value) => {
    expect(isBareOrigin(value)).toBe(true);
  });

  it.each([
    "https://a.com/path",
    "https://a.com/",
    "*",
    "https://*.a.com",
    "a.com",
    "ftp://a.com",
    "javascript:alert(1)",
    "https://a.com; script-src *",
    "https://a.com 'unsafe-inline'",
    "https://a.com:123456",
    "",
  ])("rejects %j", (value) => {
    expect(isBareOrigin(value)).toBe(false);
  });
});

describe("frameAncestorsHeader", () => {
  it("allows only this app with no origins", () => {
    expect(frameAncestorsHeader([])).toBe("frame-ancestors 'self'");
  });

  it("lists one origin after 'self'", () => {
    expect(frameAncestorsHeader(["https://a.com"])).toBe("frame-ancestors 'self' https://a.com");
  });

  it("lists two origins in order", () => {
    expect(frameAncestorsHeader(["https://a.com", "http://localhost:4000"])).toBe(
      "frame-ancestors 'self' https://a.com http://localhost:4000",
    );
  });

  it("drops anything that is not a bare origin, even if the API returned it (Review Focus 4)", () => {
    expect(
      frameAncestorsHeader([
        "https://a.com/path",
        "*",
        "https://b.com",
        "https://c.com/",
        "https://d.com; script-src *",
      ]),
    ).toBe("frame-ancestors 'self' https://b.com");
  });

  it("lower-cases what it emits", () => {
    expect(frameAncestorsHeader(["https://Shop.Example.com"])).toBe(
      "frame-ancestors 'self' https://shop.example.com",
    );
  });
});

describe("fetchFrameOrigins", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  function okResponse(origins: unknown): Response {
    return new Response(JSON.stringify({ allowed_origins: origins }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  it("asks the API's frame-policy route for the key", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse(["https://a.com"]));
    vi.stubGlobal("fetch", fetchMock);

    const origins = await fetchFrameOrigins("http://api:8000/", "pk_url", () => 0);

    expect(origins).toEqual(["https://a.com"]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe("http://api:8000/api/v1/widget/pk_url/frame-policy");
  });

  it("serves a repeat within 30 s from the cache and refetches after", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okResponse(["https://a.com"]))
      .mockResolvedValueOnce(okResponse(["https://b.com"]));
    vi.stubGlobal("fetch", fetchMock);

    expect(await fetchFrameOrigins("http://api", "pk_cache", () => 1_000)).toEqual([
      "https://a.com",
    ]);
    expect(await fetchFrameOrigins("http://api", "pk_cache", () => 30_999)).toEqual([
      "https://a.com",
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    expect(await fetchFrameOrigins("http://api", "pk_cache", () => 31_001)).toEqual([
      "https://b.com",
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("answers [] on a network failure and does not cache it", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("fetch failed"))
      .mockResolvedValueOnce(okResponse(["https://a.com"]));
    vi.stubGlobal("fetch", fetchMock);

    expect(await fetchFrameOrigins("http://api", "pk_fail", () => 0)).toEqual([]);
    expect(await fetchFrameOrigins("http://api", "pk_fail", () => 1)).toEqual(["https://a.com"]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("answers [] on a non-200 and does not cache it", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("busy", { status: 429 }))
      .mockResolvedValueOnce(okResponse(["https://a.com"]));
    vi.stubGlobal("fetch", fetchMock);

    expect(await fetchFrameOrigins("http://api", "pk_429", () => 0)).toEqual([]);
    expect(await fetchFrameOrigins("http://api", "pk_429", () => 1)).toEqual(["https://a.com"]);
  });

  it("serves the last known origins when a refetch is answered 429, even past the TTL", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okResponse(["https://a.com"]))
      .mockResolvedValueOnce(new Response("slow down", { status: 429 }))
      .mockResolvedValueOnce(okResponse(["https://b.com"]));
    vi.stubGlobal("fetch", fetchMock);

    expect(await fetchFrameOrigins("http://api", "pk_stale_429", () => 0)).toEqual([
      "https://a.com",
    ]);
    // Expired, the refetch fails: a live widget must stay framed.
    expect(await fetchFrameOrigins("http://api", "pk_stale_429", () => 60_000)).toEqual([
      "https://a.com",
    ]);
    // The stale answer is not treated as fresh: the next call asks again.
    expect(await fetchFrameOrigins("http://api", "pk_stale_429", () => 60_001)).toEqual([
      "https://b.com",
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("serves the last known origins on a network error after the TTL", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okResponse(["https://a.com"]))
      .mockRejectedValueOnce(new TypeError("fetch failed"));
    vi.stubGlobal("fetch", fetchMock);

    await fetchFrameOrigins("http://api", "pk_stale_net", () => 0);
    expect(await fetchFrameOrigins("http://api", "pk_stale_net", () => 45_000)).toEqual([
      "https://a.com",
    ]);
  });

  it("answers [] on a failure when that key never had a successful answer", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status: 429 })));
    expect(await fetchFrameOrigins("http://api", "pk_never_ok", () => 0)).toEqual([]);
    expect(await fetchFrameOrigins("http://api", "pk_never_ok", () => 60_000)).toEqual([]);
  });

  it("answers [] for a malformed body", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(okResponse("https://a.com")));
    expect(await fetchFrameOrigins("http://api", "pk_bad", () => 0)).toEqual([]);
  });

  it("keeps only string entries", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(okResponse(["https://a.com", 7, null])));
    expect(await fetchFrameOrigins("http://api", "pk_mixed", () => 0)).toEqual(["https://a.com"]);
  });

  it("evicts the oldest entry past 500 keys", async () => {
    const fetchMock = vi.fn().mockImplementation(async () => okResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    await fetchFrameOrigins("http://api", "pk_evict_first", () => 0);
    for (let i = 0; i < 500; i += 1) {
      await fetchFrameOrigins("http://api", `pk_evict_${i}`, () => 0);
    }
    fetchMock.mockClear();

    await fetchFrameOrigins("http://api", "pk_evict_499", () => 0);
    expect(fetchMock).not.toHaveBeenCalled();
    await fetchFrameOrigins("http://api", "pk_evict_first", () => 0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("sends the frame-policy secret header when WIDGET_FRAME_POLICY_SECRET is set", async () => {
    vi.stubEnv("WIDGET_FRAME_POLICY_SECRET", "s3cret");
    const fetchMock = vi.fn().mockResolvedValue(okResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    await fetchFrameOrigins("http://api", "pk_secret_set", () => 0);

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(init.headers).get("X-Widget-Frame-Secret")).toBe("s3cret");
  });

  it("sends no secret header when WIDGET_FRAME_POLICY_SECRET is unset", async () => {
    vi.stubEnv("WIDGET_FRAME_POLICY_SECRET", "");
    const fetchMock = vi.fn().mockResolvedValue(okResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    await fetchFrameOrigins("http://api", "pk_secret_unset", () => 0);

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(init.headers).has("X-Widget-Frame-Secret")).toBe(false);
  });
});
