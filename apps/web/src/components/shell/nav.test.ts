import { describe, expect, it } from "vitest";
import { currentSectionLabel, isActive, NAV_GROUPS } from "./nav";

describe("isActive", () => {
  it("keeps Agents highlighted on an agent's detail page", () => {
    expect(isActive("/dashboard/agents/0191e4c0-1234-7000-8000-000000000000", "/dashboard/agents")).toBe(
      true,
    );
  });

  it("matches a section's own page", () => {
    expect(isActive("/dashboard/agents", "/dashboard/agents")).toBe(true);
  });

  it("does not match a sibling route that merely starts with the same characters", () => {
    expect(isActive("/dashboard/agentsomething", "/dashboard/agents")).toBe(false);
  });

  it("matches Overview only exactly, since every route is under /dashboard", () => {
    expect(isActive("/dashboard", "/dashboard")).toBe(true);
    expect(isActive("/dashboard/agents", "/dashboard")).toBe(false);
  });
});

describe("currentSectionLabel", () => {
  it("names the section a nested route belongs to", () => {
    expect(currentSectionLabel("/dashboard/agents/abc")).toBe("Agents");
  });

  it("names Overview for the dashboard root", () => {
    expect(currentSectionLabel("/dashboard")).toBe("Overview");
  });

  it("falls back to a generic label for an unknown route", () => {
    expect(currentSectionLabel("/dashboard/nowhere")).toBe("Dashboard");
  });
});

describe("NAV_GROUPS", () => {
  it("gives every not-yet-built section the phase it arrives in", () => {
    const soon = NAV_GROUPS.flatMap((group) => group.items).filter((item) => item.state === "soon");
    expect(soon.length).toBeGreaterThan(0);
    expect(soon.every((item) => Boolean(item.phase))).toBe(true);
  });

  it("has no duplicate hrefs", () => {
    const hrefs = NAV_GROUPS.flatMap((group) => group.items).map((item) => item.href);
    expect(new Set(hrefs).size).toBe(hrefs.length);
  });
});
