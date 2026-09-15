import { describe, expect, it } from "vitest";
import { agentStatusLabel, agentStatusTone } from "./agent-status";

describe("agent status presentation", () => {
  it("maps each status to a badge tone", () => {
    expect(agentStatusTone("ACTIVE")).toBe("success");
    expect(agentStatusTone("DRAFT")).toBe("warn");
    expect(agentStatusTone("DISABLED")).toBe("neutral");
  });

  it("renders statuses in sentence case rather than as enum shouting", () => {
    expect(agentStatusLabel("ACTIVE")).toBe("Active");
    expect(agentStatusLabel("DRAFT")).toBe("Draft");
    expect(agentStatusLabel("DISABLED")).toBe("Disabled");
  });
});
