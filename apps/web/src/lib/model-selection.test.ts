import { describe, expect, it } from "vitest";
import { isOverridden, overrideFields } from "./model-selection";

const AGENT = { provider: "openai", model: "gpt-4o-mini" };

describe("overrideFields", () => {
  it("sends nothing when the choice is the agent's own pair", () => {
    // The playground sends this on every turn, so an untouched one must put
    // exactly the request on the wire it put there before the picker existed.
    expect(overrideFields(AGENT, AGENT)).toEqual({});
  });

  it("sends the pair when the model differs", () => {
    expect(overrideFields({ provider: "openai", model: "gpt-4o" }, AGENT)).toEqual({
      provider: "openai",
      model: "gpt-4o",
    });
  });

  it("sends the pair when the provider differs", () => {
    // Both fields, never the model alone: a model id only means anything
    // against the provider that serves it.
    expect(overrideFields({ provider: "anthropic", model: "claude-sonnet-5" }, AGENT)).toEqual({
      provider: "anthropic",
      model: "claude-sonnet-5",
    });
  });

  it("sends nothing while the choice is still empty", () => {
    // The picker is seeded from the agent, which arrives from a query -- the
    // gap before it resolves must not become a `model: ""` the API rejects.
    expect(overrideFields({ provider: "", model: "" }, AGENT)).toEqual({});
  });
});

describe("isOverridden", () => {
  it("is false for the agent's own pair", () => {
    expect(isOverridden(AGENT, AGENT)).toBe(false);
  });

  it("is true once either half differs", () => {
    expect(isOverridden({ provider: "openai", model: "gpt-4o" }, AGENT)).toBe(true);
    expect(isOverridden({ provider: "anthropic", model: "gpt-4o-mini" }, AGENT)).toBe(true);
  });
});
