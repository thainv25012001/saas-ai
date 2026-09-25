/** The API's own rule for a prompt key (`app/prompts/schemas.py`). */
export const PROMPT_KEY_PATTERN = /^[a-z0-9_]+$/;

export type VersionSummary = { id: string; version: number; isActive: boolean };

/** A key suggested from the name while the user has not typed one. Empty --
 * never "_" -- when nothing in the name survives, so the form's required
 * check stops the submit instead of the API rejecting it. */
export function promptKeyFromName(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 100)
    .replace(/_+$/, "");
}

export function activeVersionOf<T extends VersionSummary>(versions: readonly T[]): T | null {
  return versions.find((version) => version.isActive) ?? null;
}

/** Activating an older version is how a prompt is rolled back, so the button
 * says so. */
export function activationLabel(selected: number, active: number | null): string {
  return active !== null && selected < active ? `Roll back to v${selected}` : `Activate v${selected}`;
}

function joinNames(names: readonly string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

export function activationImpact(version: number, agentNames: readonly string[]): string {
  if (agentNames.length === 0) return "No agents use this prompt yet, so nothing changes for customers.";
  return `v${version} becomes live for ${joinNames(agentNames)} on their next message.`;
}

export function agentCountLabel(count: number): string {
  if (count === 0) return "Not used";
  return count === 1 ? "1 agent" : `${count} agents`;
}
