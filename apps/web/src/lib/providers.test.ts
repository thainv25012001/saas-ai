import { describe, expect, it } from "vitest";
import { PROVIDERS, providerLabel } from "./providers";

describe("providerLabel", () => {
  it("gives every provider a display name, the way statuses have one", () => {
    // The Status dropdown reads "Draft", the Provider dropdown read "openai".
    // Two dropdowns on one card, two conventions.
    expect(providerLabel("openai")).toBe("OpenAI");
    expect(providerLabel("anthropic")).toBe("Anthropic");
    expect(providerLabel("openrouter")).toBe("OpenRouter");
  });

  it("says what `fake` actually is, since its name explains nothing", () => {
    expect(providerLabel("fake")).toBe("Fake (offline)");
  });

  it("falls back to the raw id for a provider the API added after this list", () => {
    // The API validates against its own KNOWN_PROVIDERS. An unknown id must
    // still render as something, not as blank.
    expect(providerLabel("mistral")).toBe("mistral");
  });

  it("labels every provider the form offers", () => {
    for (const id of PROVIDERS) {
      expect(providerLabel(id)).not.toBe("");
    }
  });
});
