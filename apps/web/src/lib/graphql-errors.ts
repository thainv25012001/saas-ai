import type { CombinedError } from "urql";

/**
 * The message to show a user for a failed urql operation. Replaces four
 * copies of `result.error?.graphQLErrors[0]?.message ?? null`, which rendered
 * nothing at all when the request never reached the server.
 */
export function firstGraphQLError(error: CombinedError | undefined): string | null {
  if (!error) return null;

  const graphQLMessage = error.graphQLErrors[0]?.message;
  if (graphQLMessage) return graphQLMessage;

  if (error.networkError) {
    return "Could not reach the server. Check your connection and try again.";
  }

  return error.message || "Something went wrong. Please try again.";
}
