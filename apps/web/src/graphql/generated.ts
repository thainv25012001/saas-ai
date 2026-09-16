/** Internal type. DO NOT USE DIRECTLY. */
type Exact<T extends { [key: string]: unknown }> = { [K in keyof T]: T[K] };
/** Internal type. DO NOT USE DIRECTLY. */
export type Incremental<T> = T | { [P in keyof T]?: P extends ' $fragmentName' | '__typename' ? T[P] : never };
import { TypedDocumentNode as DocumentNode } from '@graphql-typed-document-node/core';
export type AgentStatus =
  | 'ACTIVE'
  | 'DISABLED'
  | 'DRAFT';

export type UpdateAgentConfigInput = {
  enabledToolNames?: Array<string> | null | undefined;
  fallbackMessage?: string | null | undefined;
  greeting?: string | null | undefined;
  language?: string | null | undefined;
  maxAgentSteps?: number | null | undefined;
  persona?: string | null | undefined;
  retrievalMinScore?: number | null | undefined;
  retrievalTopK?: number | null | undefined;
  tone?: string | null | undefined;
};

export type UpdateAgentInput = {
  maxTokens?: number | null | undefined;
  model?: string | null | undefined;
  name?: string | null | undefined;
  provider?: string | null | undefined;
  status?: AgentStatus | null | undefined;
  temperature?: number | null | undefined;
};

export type AgentsQueryVariables = Exact<{ [key: string]: never; }>;


export type AgentsQuery = { agents: Array<{ id: unknown, name: string, slug: string, status: AgentStatus, model: string, provider: string, createdAt: unknown }> };

export type AgentQueryVariables = Exact<{
  id: unknown;
}>;


export type AgentQuery = { agent: { id: unknown, name: string, slug: string, status: AgentStatus, provider: string, model: string, temperature: number, maxTokens: number, config: { id: unknown, tone: string, language: string, persona: string | null, greeting: string | null, fallbackMessage: string, retrievalTopK: number, maxAgentSteps: number } | null } };

export type CreateAgentMutationVariables = Exact<{
  name: string;
}>;


export type CreateAgentMutation = { createAgent: { id: unknown, name: string, slug: string, status: AgentStatus } };

export type UpdateAgentMutationVariables = Exact<{
  id: unknown;
  input: UpdateAgentInput;
}>;


export type UpdateAgentMutation = { updateAgent: { id: unknown, name: string, slug: string, status: AgentStatus, provider: string, model: string, temperature: number, maxTokens: number } };

export type UpdateAgentConfigMutationVariables = Exact<{
  agentId: unknown;
  input: UpdateAgentConfigInput;
}>;


export type UpdateAgentConfigMutation = { updateAgentConfig: { id: unknown, tone: string, retrievalTopK: number, maxAgentSteps: number } };

export type DeleteAgentMutationVariables = Exact<{
  id: unknown;
}>;


export type DeleteAgentMutation = { deleteAgent: boolean };

export type ProviderModelsQueryVariables = Exact<{
  provider: string;
}>;


export type ProviderModelsQuery = { providerModels: Array<{ id: string, label: string, contextLength: number | null }> };


export const AgentsDocument = {"kind":"Document","definitions":[{"kind":"OperationDefinition","operation":"query","name":{"kind":"Name","value":"Agents"},"selectionSet":{"kind":"SelectionSet","selections":[{"kind":"Field","name":{"kind":"Name","value":"agents"},"selectionSet":{"kind":"SelectionSet","selections":[{"kind":"Field","name":{"kind":"Name","value":"id"}},{"kind":"Field","name":{"kind":"Name","value":"name"}},{"kind":"Field","name":{"kind":"Name","value":"slug"}},{"kind":"Field","name":{"kind":"Name","value":"status"}},{"kind":"Field","name":{"kind":"Name","value":"model"}},{"kind":"Field","name":{"kind":"Name","value":"provider"}},{"kind":"Field","name":{"kind":"Name","value":"createdAt"}}]}}]}}]} as unknown as DocumentNode<AgentsQuery, AgentsQueryVariables>;
export const AgentDocument = {"kind":"Document","definitions":[{"kind":"OperationDefinition","operation":"query","name":{"kind":"Name","value":"Agent"},"variableDefinitions":[{"kind":"VariableDefinition","variable":{"kind":"Variable","name":{"kind":"Name","value":"id"}},"type":{"kind":"NonNullType","type":{"kind":"NamedType","name":{"kind":"Name","value":"UUID"}}}}],"selectionSet":{"kind":"SelectionSet","selections":[{"kind":"Field","name":{"kind":"Name","value":"agent"},"arguments":[{"kind":"Argument","name":{"kind":"Name","value":"id"},"value":{"kind":"Variable","name":{"kind":"Name","value":"id"}}}],"selectionSet":{"kind":"SelectionSet","selections":[{"kind":"Field","name":{"kind":"Name","value":"id"}},{"kind":"Field","name":{"kind":"Name","value":"name"}},{"kind":"Field","name":{"kind":"Name","value":"slug"}},{"kind":"Field","name":{"kind":"Name","value":"status"}},{"kind":"Field","name":{"kind":"Name","value":"provider"}},{"kind":"Field","name":{"kind":"Name","value":"model"}},{"kind":"Field","name":{"kind":"Name","value":"temperature"}},{"kind":"Field","name":{"kind":"Name","value":"maxTokens"}},{"kind":"Field","name":{"kind":"Name","value":"config"},"selectionSet":{"kind":"SelectionSet","selections":[{"kind":"Field","name":{"kind":"Name","value":"id"}},{"kind":"Field","name":{"kind":"Name","value":"tone"}},{"kind":"Field","name":{"kind":"Name","value":"language"}},{"kind":"Field","name":{"kind":"Name","value":"persona"}},{"kind":"Field","name":{"kind":"Name","value":"greeting"}},{"kind":"Field","name":{"kind":"Name","value":"fallbackMessage"}},{"kind":"Field","name":{"kind":"Name","value":"retrievalTopK"}},{"kind":"Field","name":{"kind":"Name","value":"maxAgentSteps"}}]}}]}}]}}]} as unknown as DocumentNode<AgentQuery, AgentQueryVariables>;
export const CreateAgentDocument = {"kind":"Document","definitions":[{"kind":"OperationDefinition","operation":"mutation","name":{"kind":"Name","value":"CreateAgent"},"variableDefinitions":[{"kind":"VariableDefinition","variable":{"kind":"Variable","name":{"kind":"Name","value":"name"}},"type":{"kind":"NonNullType","type":{"kind":"NamedType","name":{"kind":"Name","value":"String"}}}}],"selectionSet":{"kind":"SelectionSet","selections":[{"kind":"Field","name":{"kind":"Name","value":"createAgent"},"arguments":[{"kind":"Argument","name":{"kind":"Name","value":"input"},"value":{"kind":"ObjectValue","fields":[{"kind":"ObjectField","name":{"kind":"Name","value":"name"},"value":{"kind":"Variable","name":{"kind":"Name","value":"name"}}}]}}],"selectionSet":{"kind":"SelectionSet","selections":[{"kind":"Field","name":{"kind":"Name","value":"id"}},{"kind":"Field","name":{"kind":"Name","value":"name"}},{"kind":"Field","name":{"kind":"Name","value":"slug"}},{"kind":"Field","name":{"kind":"Name","value":"status"}}]}}]}}]} as unknown as DocumentNode<CreateAgentMutation, CreateAgentMutationVariables>;
export const UpdateAgentDocument = {"kind":"Document","definitions":[{"kind":"OperationDefinition","operation":"mutation","name":{"kind":"Name","value":"UpdateAgent"},"variableDefinitions":[{"kind":"VariableDefinition","variable":{"kind":"Variable","name":{"kind":"Name","value":"id"}},"type":{"kind":"NonNullType","type":{"kind":"NamedType","name":{"kind":"Name","value":"UUID"}}}},{"kind":"VariableDefinition","variable":{"kind":"Variable","name":{"kind":"Name","value":"input"}},"type":{"kind":"NonNullType","type":{"kind":"NamedType","name":{"kind":"Name","value":"UpdateAgentInput"}}}}],"selectionSet":{"kind":"SelectionSet","selections":[{"kind":"Field","name":{"kind":"Name","value":"updateAgent"},"arguments":[{"kind":"Argument","name":{"kind":"Name","value":"id"},"value":{"kind":"Variable","name":{"kind":"Name","value":"id"}}},{"kind":"Argument","name":{"kind":"Name","value":"input"},"value":{"kind":"Variable","name":{"kind":"Name","value":"input"}}}],"selectionSet":{"kind":"SelectionSet","selections":[{"kind":"Field","name":{"kind":"Name","value":"id"}},{"kind":"Field","name":{"kind":"Name","value":"name"}},{"kind":"Field","name":{"kind":"Name","value":"slug"}},{"kind":"Field","name":{"kind":"Name","value":"status"}},{"kind":"Field","name":{"kind":"Name","value":"provider"}},{"kind":"Field","name":{"kind":"Name","value":"model"}},{"kind":"Field","name":{"kind":"Name","value":"temperature"}},{"kind":"Field","name":{"kind":"Name","value":"maxTokens"}}]}}]}}]} as unknown as DocumentNode<UpdateAgentMutation, UpdateAgentMutationVariables>;
export const UpdateAgentConfigDocument = {"kind":"Document","definitions":[{"kind":"OperationDefinition","operation":"mutation","name":{"kind":"Name","value":"UpdateAgentConfig"},"variableDefinitions":[{"kind":"VariableDefinition","variable":{"kind":"Variable","name":{"kind":"Name","value":"agentId"}},"type":{"kind":"NonNullType","type":{"kind":"NamedType","name":{"kind":"Name","value":"UUID"}}}},{"kind":"VariableDefinition","variable":{"kind":"Variable","name":{"kind":"Name","value":"input"}},"type":{"kind":"NonNullType","type":{"kind":"NamedType","name":{"kind":"Name","value":"UpdateAgentConfigInput"}}}}],"selectionSet":{"kind":"SelectionSet","selections":[{"kind":"Field","name":{"kind":"Name","value":"updateAgentConfig"},"arguments":[{"kind":"Argument","name":{"kind":"Name","value":"agentId"},"value":{"kind":"Variable","name":{"kind":"Name","value":"agentId"}}},{"kind":"Argument","name":{"kind":"Name","value":"input"},"value":{"kind":"Variable","name":{"kind":"Name","value":"input"}}}],"selectionSet":{"kind":"SelectionSet","selections":[{"kind":"Field","name":{"kind":"Name","value":"id"}},{"kind":"Field","name":{"kind":"Name","value":"tone"}},{"kind":"Field","name":{"kind":"Name","value":"retrievalTopK"}},{"kind":"Field","name":{"kind":"Name","value":"maxAgentSteps"}}]}}]}}]} as unknown as DocumentNode<UpdateAgentConfigMutation, UpdateAgentConfigMutationVariables>;
export const DeleteAgentDocument = {"kind":"Document","definitions":[{"kind":"OperationDefinition","operation":"mutation","name":{"kind":"Name","value":"DeleteAgent"},"variableDefinitions":[{"kind":"VariableDefinition","variable":{"kind":"Variable","name":{"kind":"Name","value":"id"}},"type":{"kind":"NonNullType","type":{"kind":"NamedType","name":{"kind":"Name","value":"UUID"}}}}],"selectionSet":{"kind":"SelectionSet","selections":[{"kind":"Field","name":{"kind":"Name","value":"deleteAgent"},"arguments":[{"kind":"Argument","name":{"kind":"Name","value":"id"},"value":{"kind":"Variable","name":{"kind":"Name","value":"id"}}}]}]}}]} as unknown as DocumentNode<DeleteAgentMutation, DeleteAgentMutationVariables>;
export const ProviderModelsDocument = {"kind":"Document","definitions":[{"kind":"OperationDefinition","operation":"query","name":{"kind":"Name","value":"ProviderModels"},"variableDefinitions":[{"kind":"VariableDefinition","variable":{"kind":"Variable","name":{"kind":"Name","value":"provider"}},"type":{"kind":"NonNullType","type":{"kind":"NamedType","name":{"kind":"Name","value":"String"}}}}],"selectionSet":{"kind":"SelectionSet","selections":[{"kind":"Field","name":{"kind":"Name","value":"providerModels"},"arguments":[{"kind":"Argument","name":{"kind":"Name","value":"provider"},"value":{"kind":"Variable","name":{"kind":"Name","value":"provider"}}}],"selectionSet":{"kind":"SelectionSet","selections":[{"kind":"Field","name":{"kind":"Name","value":"id"}},{"kind":"Field","name":{"kind":"Name","value":"label"}},{"kind":"Field","name":{"kind":"Name","value":"contextLength"}}]}}]}}]} as unknown as DocumentNode<ProviderModelsQuery, ProviderModelsQueryVariables>;