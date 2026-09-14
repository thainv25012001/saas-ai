import type { CodegenConfig } from "@graphql-codegen/cli";

const config: CodegenConfig = {
  schema: "../../packages/shared/schema.graphql",
  documents: ["src/**/*.graphql"],
  generates: {
    "src/graphql/generated.ts": {
      // No "typescript" (base schema types) plugin: with everything emitted
      // into one file, it and "typescript-operations" both declare any
      // enum/input type an operation touches (e.g. AgentStatus,
      // UpdateAgentInput), which collides as a duplicate identifier under
      // strict tsc. "typescript-operations" alone is self-contained - it
      // emits the Exact/Scalars preamble and every enum/input an operation
      // actually needs - and nothing here imports the raw schema types
      // (Agent, Query, Mutation, ...) directly, so dropping the base plugin
      // loses nothing this app uses.
      plugins: ["typescript-operations", "typed-document-node"],
    },
  },
};

export default config;
