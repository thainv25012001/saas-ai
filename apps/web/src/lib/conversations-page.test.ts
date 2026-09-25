import { describe, expect, it } from "vitest";
import {
  channelFilterLabel,
  channelQueryValue,
  parseChannelFilter,
  pickDefaultAgentId,
} from "./conversations-page";

describe("parseChannelFilter", () => {
  it("defaults to Widget with no query value", () => {
    expect(parseChannelFilter(null)).toBe("WIDGET");
  });

  it("accepts each known filter", () => {
    expect(parseChannelFilter("WIDGET")).toBe("WIDGET");
    expect(parseChannelFilter("PLAYGROUND")).toBe("PLAYGROUND");
    expect(parseChannelFilter("API")).toBe("API");
    expect(parseChannelFilter("ALL")).toBe("ALL");
  });

  it("falls back to Widget for an unknown value", () => {
    expect(parseChannelFilter("bogus")).toBe("WIDGET");
    expect(parseChannelFilter("")).toBe("WIDGET");
  });
});

describe("channelQueryValue", () => {
  it("passes a real channel straight through", () => {
    expect(channelQueryValue("WIDGET")).toBe("WIDGET");
    expect(channelQueryValue("PLAYGROUND")).toBe("PLAYGROUND");
    expect(channelQueryValue("API")).toBe("API");
  });

  it("omits the argument entirely for All", () => {
    expect(channelQueryValue("ALL")).toBeUndefined();
  });
});

describe("channelFilterLabel", () => {
  it("labels every filter", () => {
    expect(channelFilterLabel("WIDGET")).toBe("Widget");
    expect(channelFilterLabel("PLAYGROUND")).toBe("Playground");
    expect(channelFilterLabel("API")).toBe("API");
    expect(channelFilterLabel("ALL")).toBe("All channels");
  });
});

describe("pickDefaultAgentId", () => {
  const agents = [{ id: "a1" }, { id: "a2" }];

  it("uses the query value when it names a real agent", () => {
    expect(pickDefaultAgentId(agents, "a2")).toBe("a2");
  });

  it("falls back to the first agent when the query value names no agent", () => {
    expect(pickDefaultAgentId(agents, "nope")).toBe("a1");
  });

  it("falls back to the first agent with no query value", () => {
    expect(pickDefaultAgentId(agents, null)).toBe("a1");
  });

  it("returns null with no agents at all", () => {
    expect(pickDefaultAgentId([], "a1")).toBeNull();
  });
});
