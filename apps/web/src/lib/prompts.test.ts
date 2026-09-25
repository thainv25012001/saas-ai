import { describe, expect, it } from "vitest";
import {
  PROMPT_KEY_PATTERN,
  activationImpact,
  activationLabel,
  activeVersionOf,
  agentCountLabel,
  promptKeyFromName,
} from "./prompts";

describe("promptKeyFromName", () => {
  it("lowercases and joins words with underscores", () => {
    expect(promptKeyFromName("Sales System Prompt")).toBe("sales_system_prompt");
  });
  it("collapses runs of other characters and trims the ends", () => {
    expect(promptKeyFromName("  Onboarding — v2!  ")).toBe("onboarding_v2");
  });
  it("is empty, not '_', for a name with nothing slug-able", () => {
    expect(promptKeyFromName("!!!")).toBe("");
  });
  it("caps at 100 characters and always matches the API pattern when non-empty", () => {
    const key = promptKeyFromName("a".repeat(150));
    expect(key).toHaveLength(100);
    expect(PROMPT_KEY_PATTERN.test(key)).toBe(true);
  });
});

describe("activeVersionOf", () => {
  it("finds the active version, or null", () => {
    const versions = [
      { id: "b", version: 2, isActive: false },
      { id: "a", version: 1, isActive: true },
    ];
    expect(activeVersionOf(versions)?.id).toBe("a");
    expect(activeVersionOf([])).toBeNull();
  });
});

describe("activationLabel", () => {
  it("calls activating an older version a rollback", () => {
    expect(activationLabel(1, 3)).toBe("Roll back to v1");
  });
  it("calls activating a newer version an activation", () => {
    expect(activationLabel(4, 3)).toBe("Activate v4");
    expect(activationLabel(1, null)).toBe("Activate v1");
  });
});

describe("activationImpact", () => {
  it("names the agents it goes live for", () => {
    expect(activationImpact(2, ["Abe"])).toBe("v2 becomes live for Abe on their next message.");
    expect(activationImpact(2, ["Abe", "Zed"])).toBe(
      "v2 becomes live for Abe and Zed on their next message.",
    );
    expect(activationImpact(2, ["Abe", "Kim", "Zed"])).toBe(
      "v2 becomes live for Abe, Kim and Zed on their next message.",
    );
  });
  it("says when no agent uses the prompt", () => {
    expect(activationImpact(2, [])).toBe("No agents use this prompt yet, so nothing changes for customers.");
  });
});

describe("agentCountLabel", () => {
  it("pluralises and names the unused case", () => {
    expect(agentCountLabel(0)).toBe("Not used");
    expect(agentCountLabel(1)).toBe("1 agent");
    expect(agentCountLabel(3)).toBe("3 agents");
  });
});
