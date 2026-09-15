import { CombinedError } from "urql";
import { describe, expect, it } from "vitest";
import { firstGraphQLError } from "./graphql-errors";

describe("firstGraphQLError", () => {
  it("returns null when there is no error", () => {
    expect(firstGraphQLError(undefined)).toBeNull();
  });

  it("returns the first GraphQL error message", () => {
    const error = new CombinedError({ graphQLErrors: ["Agent name already taken", "second"] });
    expect(firstGraphQLError(error)).toBe("Agent name already taken");
  });

  it("explains a network failure in words a user can act on", () => {
    const error = new CombinedError({ networkError: new Error("Failed to fetch") });
    expect(firstGraphQLError(error)).toBe(
      "Could not reach the server. Check your connection and try again.",
    );
  });
});
