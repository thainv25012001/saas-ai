/**
 * One provider as the API reports it, from the `configuredProviders` query.
 *
 * The list is no longer hardcoded here. It used to be, mirroring the API's
 * `KNOWN_PROVIDERS`, which meant two places to edit and no way for the
 * dashboard to know which providers actually had an API key behind them.
 */
export type ProviderInfo = {
  id: string;
  /** Whether the server holds this provider's API key. `fake` is always true. */
  configured: boolean;
};

/** The offline provider: answers with a canned reply, needs no API key. */
const OFFLINE = "fake";

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

/**
 * What the *create* form may offer: providers that can actually answer.
 *
 * `fake` is excluded even though it is always `configured`, because an agent
 * that replies with a canned string is not what anyone sets out to create.
 * Creating an agent used to send a name alone and inherit the API's
 * `DEFAULT_LLM_PROVIDER`, so every agent landed on `fake` and had to be fixed
 * afterwards on its detail page.
 *
 * An empty result is meaningful: the server holds no keys, and the form has to
 * say so rather than render an empty dropdown.
 */
export function creatableProviders(providers: readonly ProviderInfo[]): ProviderInfo[] {
  return providers.filter((provider) => provider.id !== OFFLINE && provider.configured);
}

/**
 * What the *edit* form may offer, given the provider the agent is on now.
 *
 * Wider than `creatableProviders` in both directions. Unconfigured providers
 * stay in the list so the form can disable them with a visible reason, rather
 * than hiding the option and leaving the user to wonder where it went. And
 * `fake` stays when it is the agent's own provider: every agent created before
 * this change is on it, and a `<select>` with no option matching its value
 * renders blank -- a user who then touched the dropdown could never put it
 * back.
 */
export function editableProviders(
  providers: readonly ProviderInfo[],
  currentProvider: string,
): ProviderInfo[] {
  const listed = providers.filter(
    (provider) => provider.id !== OFFLINE || currentProvider === OFFLINE,
  );
  if (listed.some((provider) => provider.id === currentProvider)) return listed;
  // The agent's own provider is always offered, even when the query has not
  // resolved yet or the API has since dropped the provider from
  // KNOWN_PROVIDERS. Without it the `<select>` has no option matching its
  // value and paints its FIRST one instead, so the form shows the agent on a
  // provider it is not on -- and the next save moves it there. `configured`
  // is false because nothing here knows otherwise, which leaves the option
  // disabled rather than falsely inviting.
  return [{ id: currentProvider, configured: false }, ...listed];
}

/**
 * What the Model field says about a provider, keyed by provider id rather than
 * branched on in the form: the form asks the same question of every provider,
 * and a new one is a row here rather than another arm of a ternary in the page.
 */
const MODEL_HELP: Record<string, string> = {
  openrouter:
    "Every model OpenRouter currently serves for free. The list is fetched from OpenRouter, so it follows their roster.",
};

const DEFAULT_MODEL_HELP = "The models this app can both run and cost for the selected provider.";

export function modelFieldHelp(provider: string): string {
  return MODEL_HELP[provider] ?? DEFAULT_MODEL_HELP;
}
