import { describe, expect, it } from "vitest";
import { type SSEEvent, parseSSEStream } from "./sse";

const encoder = new TextEncoder();

/** Turns a plain string into a single-chunk async iterable -- the "nice"
 * case every real network read is not guaranteed to give you. */
async function* chunksOf(...parts: string[]): AsyncGenerator<Uint8Array> {
  for (const part of parts) {
    yield encoder.encode(part);
  }
}

async function collect(chunks: AsyncIterable<Uint8Array>): Promise<SSEEvent[]> {
  const events: SSEEvent[] = [];
  for await (const event of parseSSEStream(chunks)) {
    events.push(event);
  }
  return events;
}

function frame(payload: unknown): string {
  return `data: ${JSON.stringify(payload)}\n\n`;
}

describe("parseSSEStream", () => {
  it("parses a single frame delivered in one chunk", async () => {
    const events = await collect(chunksOf(frame({ type: "text_delta", text: "hi" })));
    expect(events).toEqual([{ type: "text_delta", text: "hi" }]);
  });

  it("parses multiple frames delivered in one chunk", async () => {
    const events = await collect(
      chunksOf(
        frame({ type: "message_start", conversation_id: "c1", message_id: "m1" }) +
          frame({ type: "text_delta", text: "a" }) +
          frame({ type: "text_delta", text: "b" }),
      ),
    );
    expect(events).toEqual([
      { type: "message_start", conversation_id: "c1", message_id: "m1" },
      { type: "text_delta", text: "a" },
      { type: "text_delta", text: "b" },
    ]);
  });

  it("ignores heartbeat comment lines", async () => {
    const events = await collect(
      chunksOf(": ping\n\n" + frame({ type: "text_delta", text: "hi" }) + ": ping\n\n"),
    );
    expect(events).toEqual([{ type: "text_delta", text: "hi" }]);
  });

  it("reassembles a frame split mid-JSON across chunks", async () => {
    const whole = frame({ type: "text_delta", text: "hello world" });
    const splitPoint = whole.indexOf('"hello');
    const events = await collect(chunksOf(whole.slice(0, splitPoint), whole.slice(splitPoint)));
    expect(events).toEqual([{ type: "text_delta", text: "hello world" }]);
  });

  it("reassembles a frame split mid- the 'data:' prefix itself", async () => {
    const whole = frame({ type: "text_delta", text: "x" });
    // Split inside the literal characters "data:" (after "da").
    const events = await collect(chunksOf(whole.slice(0, 2), whole.slice(2)));
    expect(events).toEqual([{ type: "text_delta", text: "x" }]);
  });

  it("reassembles a frame split across the blank-line separator", async () => {
    const first = frame({ type: "text_delta", text: "one" });
    const second = frame({ type: "text_delta", text: "two" });
    // Split so the first "\n" of the "\n\n" separator ends chunk 1 and the
    // second "\n" starts chunk 2 -- the separator itself straddles the
    // boundary, not just the data around it.
    const combined = first + second;
    const separatorStart = first.length - 2; // index of the first \n of "\n\n"
    const events = await collect(
      chunksOf(combined.slice(0, separatorStart + 1), combined.slice(separatorStart + 1)),
    );
    expect(events).toEqual([
      { type: "text_delta", text: "one" },
      { type: "text_delta", text: "two" },
    ]);
  });

  it("splits a single frame across many single-byte chunks", async () => {
    const whole = frame({ type: "message_end", usage: { input_tokens: 1, output_tokens: 2 }, cost_usd: "0.01", latency_ms: 5, model: "fake-1", prompt_version_id: null });
    const bytes = encoder.encode(whole);
    async function* byteAtATime(): AsyncGenerator<Uint8Array> {
      for (const b of bytes) yield new Uint8Array([b]);
    }
    const events = await collect(byteAtATime());
    expect(events).toEqual([
      {
        type: "message_end",
        usage: { input_tokens: 1, output_tokens: 2 },
        cost_usd: "0.01",
        latency_ms: 5,
        model: "fake-1",
        prompt_version_id: null,
      },
    ]);
  });

  it("handles a multi-byte UTF-8 character split across chunk boundaries", async () => {
    const whole = frame({ type: "text_delta", text: "café — emoji 😀" });
    const bytes = encoder.encode(whole);
    // Cut in the middle of the buffer so at least one multi-byte sequence
    // is very likely split; TextDecoder's `{stream: true}` must carry the
    // partial sequence over rather than emit U+FFFD.
    const mid = Math.floor(bytes.length / 2);
    async function* twoChunks(): AsyncGenerator<Uint8Array> {
      yield bytes.slice(0, mid);
      yield bytes.slice(mid);
    }
    const events = await collect(twoChunks());
    expect(events).toEqual([{ type: "text_delta", text: "café — emoji 😀" }]);
  });

  it("treats cost_usd: null as null, not the string 'null'", async () => {
    const events = await collect(
      chunksOf(
        frame({
          type: "message_end",
          usage: { input_tokens: 10, output_tokens: 20 },
          cost_usd: null,
          latency_ms: 100,
          model: "gpt-4o-mini",
          prompt_version_id: null,
        }),
      ),
    );
    expect(events).toEqual([
      {
        type: "message_end",
        usage: { input_tokens: 10, output_tokens: 20 },
        cost_usd: null,
        latency_ms: 100,
        model: "gpt-4o-mini",
        prompt_version_id: null,
      },
    ]);
  });

  it("parses an error event and preserves its code and message", async () => {
    const events = await collect(
      chunksOf(frame({ type: "error", code: "llm_rate_limited", message: "Too many requests" })),
    );
    expect(events).toEqual([{ type: "error", code: "llm_rate_limited", message: "Too many requests" }]);
  });

  it("skips a malformed frame instead of throwing", async () => {
    const events = await collect(chunksOf("data: {not json\n\n" + frame({ type: "text_delta", text: "ok" })));
    expect(events).toEqual([{ type: "text_delta", text: "ok" }]);
  });

  it("skips an unrecognised event type instead of throwing", async () => {
    const events = await collect(
      chunksOf(frame({ type: "future_event", whatever: 1 }) + frame({ type: "text_delta", text: "ok" })),
    );
    expect(events).toEqual([{ type: "text_delta", text: "ok" }]);
  });

  it("still recovers a final frame whose JSON is complete but whose trailing blank line never arrived", async () => {
    // A well-behaved server always terminates a frame with "\n\n", but a
    // connection can close the instant after the last byte of valid JSON
    // and before the separator. Since the payload itself is well-formed,
    // this is treated as a real event rather than discarded -- the data
    // arrived, only the terminator did not.
    const events = await collect(chunksOf('data: {"type":"text_delta","text":"partial"}'));
    expect(events).toEqual([{ type: "text_delta", text: "partial" }]);
  });

  it("discards a genuinely truncated final frame (incomplete JSON)", async () => {
    const events = await collect(chunksOf('data: {"type":"text_delta","text":"cut off'));
    expect(events).toEqual([]);
  });
});
