import { describe, expect, it } from "vitest";
import {
  type ProviderInfo,
  creatableProviders,
  editableProviders,
  modelFieldHelp,
  providerLabel,
} from "./providers";

const ALL: ProviderInfo[] = [
  { id: "fake", configured: true },
  { id: "openai", configured: false },
  { id: "anthropic", configured: false },
  { id: "openrouter", configured: true },
];

const ids = (providers: readonly ProviderInfo[]) => providers.map((p) => p.id);

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

  it("labels every provider the API currently reports", () => {
    for (const provider of ALL) {
      expect(providerLabel(provider.id)).not.toBe("");
    }
  });
});

describe("creatableProviders", () => {
  it("offers only providers whose API key is set", () => {
    // Picking one without a key produces an agent that cannot answer, and the
    // failure surfaces in the playground rather than at the dropdown.
    expect(ids(creatableProviders(ALL))).toEqual(["openrouter"]);
  });

  it("never offers `fake` on a new agent", () => {
    // It is `configured`, because it needs no key -- but an agent that replies
    // with a canned string is not what anyone is trying to create. Every agent
    // used to land on it by default, which is the whole reason this exists.
    expect(ids(creatableProviders(ALL))).not.toContain("fake");
  });

  it("is empty when the server holds no keys at all", () => {
    // The form has to say so rather than render an empty dropdown.
    expect(creatableProviders([{ id: "fake", configured: true }])).toEqual([]);
  });
});

describe("editableProviders", () => {
  it("lists every provider, so an unconfigured one can still be seen and chosen", () => {
    // Unlike the create form, the edit form shows them all: it disables the
    // unconfigured ones rather than hiding them, so the reason is visible.
    expect(ids(editableProviders(ALL, "openrouter"))).toEqual([
      "openai",
      "anthropic",
      "openrouter",
    ]);
  });

  it("keeps `fake` when it is the agent's current provider", () => {
    // Every agent created before this change is on `fake`. Dropping it from
    // the list makes the select render blank for exactly those agents, and a
    // user who touches the dropdown can never put it back.
    expect(ids(editableProviders(ALL, "fake"))).toContain("fake");
  });

  it("drops `fake` for an agent already on a real provider", () => {
    expect(ids(editableProviders(ALL, "anthropic"))).not.toContain("fake");
  });

  it("always includes the agent's own provider, even before the list has loaded", () => {
    // The page renders while the query is still in flight. A `<select>` with
    // no option matching its value paints its FIRST option instead, so an
    // empty list would show the agent on a provider it is not on -- and saving
    // the form would then move it there.
    expect(ids(editableProviders([], "anthropic"))).toEqual(["anthropic"]);
  });

  it("does not duplicate the current provider when the list already has it", () => {
    expect(ids(editableProviders(ALL, "openai")).filter((id) => id === "openai")).toHaveLength(1);
  });

  it("marks a provider only known from the agent as unconfigured", () => {
    // Nothing is known about it yet. Claiming it is configured would enable an
    // option that cannot answer.
    expect(editableProviders([], "anthropic")[0].configured).toBe(false);
  });
});

describe("modelFieldHelp", () => {
  it("says where OpenRouter's list comes from, since it is the one that moves", () => {
    expect(modelFieldHelp("openrouter")).toMatch(/fetched from OpenRouter/);
  });

  it("describes every other provider without the form branching on its name", () => {
    // The point of the lookup: adding a provider must not mean adding an arm
    // to a ternary in the page.
    for (const provider of ALL) {
      expect(modelFieldHelp(provider.id)).not.toBe("");
    }
    expect(modelFieldHelp("mistral")).toBe(modelFieldHelp("openai"));
  });
});
