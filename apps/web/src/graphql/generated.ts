/** Internal type. DO NOT USE DIRECTLY. */
type Exact<T extends { [key: string]: unknown }> = { [K in keyof T]: T[K] };
/** Internal type. DO NOT USE DIRECTLY. */
export type Incremental<T> = T | { [P in keyof T]?: P extends ' $fragmentName' | '__typename' ? T[P] : never };
import { TypedDocumentNode as DocumentNode } from '@graphql-typed-document-node/core';
export type Maybe<T> = T | null;
export type InputMaybe<T> = Maybe<T>;
/** All built-in and custom scalars, mapped to their actual values */
export type Scalars = {
  ID: { input: string; output: string; }
  String: { input: string; output: string; }
  Boolean: { input: boolean; output: boolean; }
  Int: { input: number; output: number; }
  Float: { input: number; output: number; }
  /** Date with time (isoformat) */
  DateTime: { input: unknown; output: unknown; }
  UUID: { input: unknown; output: unknown; }
};

export type Agent = {
  __typename?: 'Agent';
  config?: Maybe<AgentConfig>;
  createdAt: Scalars['DateTime']['output'];
  id: Scalars['UUID']['output'];
  maxTokens: Scalars['Int']['output'];
  model: Scalars['String']['output'];
  name: Scalars['String']['output'];
  promptId?: Maybe<Scalars['UUID']['output']>;
  provider: Scalars['String']['output'];
  slug: Scalars['String']['output'];
  status: AgentStatus;
  temperature: Scalars['Float']['output'];
  updatedAt: Scalars['DateTime']['output'];
};

export type AgentConfig = {
  __typename?: 'AgentConfig';
  enabledToolNames: Array<Scalars['String']['output']>;
  fallbackMessage: Scalars['String']['output'];
  greeting?: Maybe<Scalars['String']['output']>;
  id: Scalars['UUID']['output'];
  language: Scalars['String']['output'];
  maxAgentSteps: Scalars['Int']['output'];
  persona?: Maybe<Scalars['String']['output']>;
  retrievalMinScore: Scalars['Float']['output'];
  retrievalTopK: Scalars['Int']['output'];
  tone: Scalars['String']['output'];
};

export enum AgentStatus {
  Active = 'ACTIVE',
  Disabled = 'DISABLED',
  Draft = 'DRAFT'
}

export type CreateAgentInput = {
  maxTokens?: Scalars['Int']['input'];
  model?: Scalars['String']['input'];
  name: Scalars['String']['input'];
  provider?: Scalars['String']['input'];
  temperature?: Scalars['Float']['input'];
};

export type CreatePromptInput = {
  description?: InputMaybe<Scalars['String']['input']>;
  key: Scalars['String']['input'];
  name: Scalars['String']['input'];
  systemPrompt: Scalars['String']['input'];
};

export type CreatePromptVersionInput = {
  notes?: InputMaybe<Scalars['String']['input']>;
  systemPrompt: Scalars['String']['input'];
};

export type Me = {
  __typename?: 'Me';
  email: Scalars['String']['output'];
  fullName: Scalars['String']['output'];
  organizationId: Scalars['UUID']['output'];
  organizationName: Scalars['String']['output'];
  role: Scalars['String']['output'];
  userId: Scalars['UUID']['output'];
};

export type Mutation = {
  __typename?: 'Mutation';
  activatePromptVersion: PromptVersion;
  createAgent: Agent;
  createPrompt: Prompt;
  createPromptVersion: PromptVersion;
  deleteAgent: Scalars['Boolean']['output'];
  updateAgent: Agent;
  updateAgentConfig: AgentConfig;
};


export type MutationActivatePromptVersionArgs = {
  versionId: Scalars['UUID']['input'];
};


export type MutationCreateAgentArgs = {
  input: CreateAgentInput;
};


export type MutationCreatePromptArgs = {
  input: CreatePromptInput;
};


export type MutationCreatePromptVersionArgs = {
  input: CreatePromptVersionInput;
  promptId: Scalars['UUID']['input'];
};


export type MutationDeleteAgentArgs = {
  id: Scalars['UUID']['input'];
};


export type MutationUpdateAgentArgs = {
  id: Scalars['UUID']['input'];
  input: UpdateAgentInput;
};


export type MutationUpdateAgentConfigArgs = {
  agentId: Scalars['UUID']['input'];
  input: UpdateAgentConfigInput;
};

export type Prompt = {
  __typename?: 'Prompt';
  createdAt: Scalars['DateTime']['output'];
  description?: Maybe<Scalars['String']['output']>;
  id: Scalars['UUID']['output'];
  key: Scalars['String']['output'];
  name: Scalars['String']['output'];
};

export type PromptVersion = {
  __typename?: 'PromptVersion';
  createdAt: Scalars['DateTime']['output'];
  id: Scalars['UUID']['output'];
  isActive: Scalars['Boolean']['output'];
  notes?: Maybe<Scalars['String']['output']>;
  systemPrompt: Scalars['String']['output'];
  version: Scalars['Int']['output'];
};

export type Query = {
  __typename?: 'Query';
  agent: Agent;
  agents: Array<Agent>;
  me: Me;
  prompt: Prompt;
  prompts: Array<Prompt>;
};


export type QueryAgentArgs = {
  id: Scalars['UUID']['input'];
};


export type QueryPromptArgs = {
  id: Scalars['UUID']['input'];
};

export type UpdateAgentConfigInput = {
  enabledToolNames?: InputMaybe<Array<Scalars['String']['input']>>;
  fallbackMessage?: InputMaybe<Scalars['String']['input']>;
  greeting?: InputMaybe<Scalars['String']['input']>;
  language?: InputMaybe<Scalars['String']['input']>;
  maxAgentSteps?: InputMaybe<Scalars['Int']['input']>;
  persona?: InputMaybe<Scalars['String']['input']>;
  retrievalMinScore?: InputMaybe<Scalars['Float']['input']>;
  retrievalTopK?: InputMaybe<Scalars['Int']['input']>;
  tone?: InputMaybe<Scalars['String']['input']>;
};

export type UpdateAgentInput = {
  maxTokens?: InputMaybe<Scalars['Int']['input']>;
  model?: InputMaybe<Scalars['String']['input']>;
  name?: InputMaybe<Scalars['String']['input']>;
  provider?: InputMaybe<Scalars['String']['input']>;
  status?: InputMaybe<AgentStatus>;
  temperature?: InputMaybe<Scalars['Float']['input']>;
};

export type PingQueryVariables = Exact<{ [key: string]: never; }>;


export type PingQuery = { me: { email: string } };


export const PingDocument = {"kind":"Document","definitions":[{"kind":"OperationDefinition","operation":"query","name":{"kind":"Name","value":"Ping"},"selectionSet":{"kind":"SelectionSet","selections":[{"kind":"Field","name":{"kind":"Name","value":"me"},"selectionSet":{"kind":"SelectionSet","selections":[{"kind":"Field","name":{"kind":"Name","value":"email"}}]}}]}}]} as unknown as DocumentNode<PingQuery, PingQueryVariables>;