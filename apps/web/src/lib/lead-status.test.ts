import { describe, expect, it } from "vitest";
import { leadStatusLabel, leadStatusTone } from "./lead-status";

describe("leadStatusLabel / leadStatusTone", () => {
  it("labels every status distinctly from its own wire value", () => {
    expect(leadStatusLabel("NEW")).toBe("New");
    expect(leadStatusLabel("CONTACTED")).toBe("Contacted");
    expect(leadStatusLabel("QUALIFIED")).toBe("Qualified");
    expect(leadStatusLabel("WON")).toBe("Won");
    expect(leadStatusLabel("LOST")).toBe("Lost");
  });

  it("gives every status its own tone, so the badges are not all the same colour", () => {
    const statuses = ["NEW", "CONTACTED", "QUALIFIED", "WON", "LOST"] as const;
    const tones = statuses.map(leadStatusTone);
    expect(new Set(tones).size).toBe(statuses.length);
  });

  it("marks won as success and lost as danger, the two terminal outcomes", () => {
    expect(leadStatusTone("WON")).toBe("success");
    expect(leadStatusTone("LOST")).toBe("danger");
  });
});
