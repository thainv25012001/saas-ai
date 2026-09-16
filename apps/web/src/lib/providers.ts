/**
 * Mirrors `app/llm/registry.KNOWN_PROVIDERS`, which is what the API validates
 * against. `fake` belongs here: `DEFAULT_LLM_PROVIDER` is `fake` out of the
 * box, so every agent created by a fresh clone has `provider="fake"` --
 * without it in this list the select rendered blank for exactly those agents,
 * and a user who touched the dropdown could never put it back.
 */
export const PROVIDERS = ["fake", "openai", "anthropic", "openrouter"] as const;

const LABELS: Record<string, string> = {
  fake: "Fake (offline)",
  openai: "OpenAI",
  anthropic: "Anthropic",
  openrouter: "OpenRouter",
};

/**
 * The display name for a provider id. The stored value is always the raw id --
 * this only decides what the dropdown shows, so that Provider reads the way
 * Status does rather than leaking wire ids into the form.
 */
export function providerLabel(provider: string): string {
  return LABELS[provider] ?? provider;
}
