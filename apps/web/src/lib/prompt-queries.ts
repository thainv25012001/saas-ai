import { useQuery } from "urql";
import { PromptDocument, PromptsDocument } from "@/graphql/generated";

/*
 * Both queries are asked of the network on every mount (`cache-and-network`),
 * not served from urql's document cache alone. That cache invalidates a
 * result only by the typenames it contains, and an empty list contains none:
 * a cached prompt with `agents: []` survives the `setAgentPrompt` that links
 * it (which returns an `Agent`), and a cached `prompts: []` survives the
 * `createPrompt` that ends it. On the prompt page that stale "no agents" is
 * what the activation confirmation reads -- it would tell the operator that
 * nothing changes for customers when a version is about to go live.
 */

export function usePromptDetailQuery(id: string, pause: boolean) {
  return useQuery({ query: PromptDocument, variables: { id }, pause, requestPolicy: "cache-and-network" });
}

export function usePromptOptionsQuery(pause: boolean) {
  return useQuery({ query: PromptsDocument, pause, requestPolicy: "cache-and-network" });
}
