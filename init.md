You are a senior AI engineer and full-stack architect.

I want you to build a production-oriented MVP of an **AI Sales Agent SaaS**.

The product allows a business to create an AI sales assistant for its website. The business provides product information, documents, FAQs, policies, and other knowledge. Customers can chat with the AI to ask questions and get product recommendations.

The long-term goal is to support multiple business types such as car dealerships, e-commerce stores, and local businesses.

## Primary Goals

This project has two goals:

1. Build a strong portfolio project demonstrating:

   * LLM integration
   * RAG
   * Embeddings
   * Vector search
   * AI Agents
   * Tool calling
   * MCP
   * AI evaluation
   * Prompt/version management
   * Production-oriented backend architecture

2. Build the foundation of a real SaaS product that could eventually be sold to businesses.

Do NOT build a generic ChatGPT clone.

The AI should behave like a **sales assistant** that understands the company's knowledge, recommends products, answers customer questions, and eventually performs business actions through tools.

---

# Tech Stack

## Frontend

* Next.js
* TypeScript
* Tailwind CSS
* GraphQL client
* Streaming AI chat UI

## Backend

* Python
* FastAPI
* GraphQL
* PostgreSQL
* SQLAlchemy
* Redis

## AI

Initially support:

* OpenAI
* Anthropic

Design the AI provider layer so that providers can be switched without rewriting the agent.

Example abstraction:

```python
class LLMProvider:
    async def generate(...)
    async def stream(...)
    async def generate_structured(...)
```

Do not tightly couple business logic to OpenAI-specific APIs.

## Vector Database

Use a vector database with good Python support.

Prefer a simple architecture for MVP.

PostgreSQL + pgvector is acceptable and preferred if it keeps the system simpler.

---

# Core Product Concept

A business should be able to:

1. Create an organization
2. Create an AI agent
3. Upload business knowledge
4. Add products
5. Configure the AI's personality
6. Configure prompts
7. Test the AI in a playground
8. Eventually embed the chatbot into their website

A customer should be able to:

1. Open the chatbot
2. Ask questions
3. Get answers based on business knowledge
4. Get product recommendations
5. Ask follow-up questions
6. Eventually perform actions such as:

   * create lead
   * check product availability
   * schedule appointment
   * request quote

---

# Important Architecture Principle

Do NOT implement the AI as:

```text
User
  ↓
LLM
  ↓
Response
```

The architecture should be:

```text
User
 ↓
Chat API
 ↓
Agent Orchestrator
 ↓
Intent / Context understanding
 ↓
Retrieve relevant knowledge
 ↓
Decide whether tools are required
 ↓
Tool calls if necessary
 ↓
LLM reasoning / response generation
 ↓
Streaming response
```

The agent must be able to decide:

* when to retrieve knowledge
* when to call a tool
* when it has enough information
* when it should ask the customer a follow-up question
* when it should admit that the information is unavailable

---

# Multi-Tenant Architecture

Design the database from the beginning to support multiple businesses.

Core entities should include approximately:

```text
Organization
User
Agent
AgentConfig
Conversation
Message
Document
DocumentChunk
Product
Tool
Lead
Prompt
PromptVersion
Evaluation
```

Every business-owned resource must be scoped to an organization.

Example:

```text
organization_id
```

must be used to isolate tenant data.

Do not implement multi-tenancy as an afterthought.

---

# RAG Pipeline

Build a proper ingestion pipeline.

Example:

```text
Upload Document
      ↓
Extract text
      ↓
Clean / normalize
      ↓
Chunk
      ↓
Generate embeddings
      ↓
Store chunks
      ↓
Store embeddings
      ↓
Ready for retrieval
```

The retrieval pipeline should support:

```text
User Question
      ↓
Query processing
      ↓
Embedding
      ↓
Vector search
      ↓
Relevant chunks
      ↓
Context construction
      ↓
LLM
```

The system must preserve metadata such as:

```text
organization_id
document_id
chunk_id
source
title
page
product_id
```

so the AI can provide citations/sources later.

---

# Product Knowledge

Do not rely only on documents.

Create a structured Product model.

Example:

```text
Product
- id
- organization_id
- name
- description
- category
- price
- currency
- attributes
- availability
- metadata
```

The AI should be able to combine:

```text
Structured product data
+
RAG knowledge
+
Tools
```

---

# Agent

Create an Agent Orchestrator.

The agent should support:

### 1. Knowledge retrieval

Example:

Customer:

"Does the Camry have a hybrid version?"

Agent:

```text
retrieve_knowledge(...)
```

### 2. Product search

Example:

Customer:

"I need a family car under $35,000."

Agent:

```text
search_products(
    budget=35000,
    use_case="family"
)
```

### 3. Lead creation

Example:

Customer:

"I'm interested. My name is John and my phone is..."

Agent:

```text
create_lead(...)
```

### 4. Appointment booking

Later:

```text
schedule_appointment(...)
```

---

# Tool Calling

Create a clean tool abstraction.

Example:

```python
class AgentTool:
    name: str
    description: str

    async def execute(self, arguments):
        ...
```

Initial tools:

```text
search_products
get_product
create_lead
```

Keep the architecture extensible so future tools can include:

```text
check_inventory
schedule_appointment
create_quote
check_order
send_email
```

---

# MCP

Do not over-engineer MCP in the first implementation.

However, design the tool layer so that the same business capabilities can eventually be exposed through MCP.

The architecture should allow:

```text
Agent
  ↓
Tool Interface
  ↓
Local Tool
OR
MCP Tool
```

Add MCP after the basic agent/tool system works.

---

# Prompt Management

Create prompt versioning.

Example:

```text
Prompt
 ├── Version 1
 ├── Version 2
 └── Version 3
```

Each version should have:

```text
system_prompt
created_at
created_by
version
is_active
```

The agent should always use the active prompt version.

Do not hard-code the main system prompt throughout the codebase.

---

# Agent System Prompt

Create a configurable system prompt similar to:

```text
You are the AI sales assistant for {{company_name}}.

Your job is to help customers understand our products and find the product that best matches their needs.

Rules:

1. Only provide factual information supported by the company's knowledge base or available tools.
2. Never invent prices, specifications, availability, policies, or product information.
3. If information is unavailable, clearly say that you do not have that information.
4. Ask useful follow-up questions when necessary.
5. When appropriate, recommend products based on the customer's requirements.
6. Do not aggressively pressure customers to buy.
7. When a tool is required, use the appropriate tool.
8. When creating a lead or performing an external action, confirm the required information before executing the action.
9. Keep responses concise and useful.
10. Maintain context throughout the conversation.
```

Make this configurable per Agent.

---

# GraphQL

Use GraphQL as the main application API.

Create schemas/resolvers for:

```text
Organization
Agent
Conversation
Message
Document
Product
Lead
Prompt
```

Example queries:

```graphql
query GetAgent($id: ID!) {
  agent(id: $id) {
    id
    name
    status
  }
}
```

Example mutation:

```graphql
mutation CreateProduct(...) {
  createProduct(...) {
    id
    name
  }
}
```

For AI streaming, use an appropriate streaming mechanism rather than forcing everything through standard GraphQL request/response semantics.

---

# Chat API

The chat system should support streaming.

Example flow:

```text
POST /chat
      ↓
create/retrieve conversation
      ↓
Agent
      ↓
retrieve context
      ↓
tool calls
      ↓
LLM streaming
      ↓
stream tokens to frontend
```

The frontend should show the response progressively.

---

# Frontend

Build an admin dashboard.

Pages:

```text
/dashboard
/dashboard/agents
/dashboard/agents/[id]
/dashboard/knowledge
/dashboard/products
/dashboard/leads
/dashboard/prompts
/dashboard/playground
```

The playground should allow the business owner to test the AI before deploying it.

Example:

```text
┌─────────────────────────────────────────────┐
│ AI Sales Agent Playground                  │
├─────────────────────────────────────────────┤
│                                             │
│ Customer: I need a family car under $35k   │
│                                             │
│ AI: Based on your budget and family size... │
│                                             │
├─────────────────────────────────────────────┤
│ Ask something...                         ➤  │
└─────────────────────────────────────────────┘
```

---

# Evaluation

Create the foundation for AI evaluation.

For each conversation, eventually support metrics such as:

```text
answer relevance
faithfulness
retrieval relevance
tool correctness
hallucination rate
```

For MVP, implement a simple evaluation framework where predefined questions have expected/reference answers.

Example:

```text
Question:
"What is the warranty for the Camry?"

Expected:
"3 years"

AI answer:
"3 years"

Score:
PASS
```

The architecture should later support LLM-as-a-judge evaluation.

---

# Observability

Log:

```text
request_id
organization_id
conversation_id
agent_id
model
prompt_version
retrieved_chunks
tool_calls
latency
token_usage
estimated_cost
errors
```

This is important because this should be a production-oriented AI system, not just a demo.

Never log sensitive customer information unnecessarily.

---

# Error Handling

Handle:

* LLM timeout
* LLM rate limit
* vector DB failure
* malformed tool arguments
* tool execution failure
* embedding failure
* document processing failure

The AI should gracefully recover.

Example:

If a tool fails:

```text
The system was unable to check live inventory.
```

Do NOT hallucinate a result.

---

# Security

Implement:

* authentication
* authorization
* organization-level isolation
* input validation
* rate limiting
* secure API keys
* environment variables
* protection against prompt injection where practical

Never expose LLM provider API keys to the frontend.

---

# Development Strategy

Do NOT try to build everything at once.

Build in phases.

## Phase 1 — Foundation

Implement:

* repository structure
* FastAPI
* GraphQL
* PostgreSQL
* migrations
* authentication
* Organization
* User
* Agent
* basic Next.js dashboard

Everything must run locally.

---

## Phase 2 — Basic LLM Chat

Implement:

```text
Next.js
 ↓
Chat API
 ↓
LLM Provider
 ↓
streaming response
```

Support OpenAI first.

Then abstract the provider so Anthropic can be added.

---

## Phase 3 — RAG

Implement:

```text
Document upload
 ↓
text extraction
 ↓
chunking
 ↓
embedding
 ↓
pgvector
 ↓
retrieval
 ↓
LLM
```

The playground must be able to answer questions based on uploaded documents.

---

## Phase 4 — Agent + Tools

Implement:

```text
Agent
 ├── retrieve_knowledge
 ├── search_products
 ├── get_product
 └── create_lead
```

The agent decides when to use each tool.

---

## Phase 5 — Evaluation

Implement:

* test datasets
* evaluation runs
* basic scoring
* retrieval evaluation
* answer evaluation

---

## Phase 6 — MCP

Expose selected business capabilities through MCP.

Do this only after the normal tool system is stable.

---

## Phase 7 — SaaS Features

Later implement:

* multi-tenant billing
* Stripe
* usage limits
* subscription plans
* embeddable chatbot
* analytics
* lead dashboard

Do not build these in the first MVP.

---

# Code Quality Requirements

Use:

* clear module boundaries
* type hints
* Pydantic
* async Python where appropriate
* dependency injection
* repository/service patterns where useful
* unit tests
* integration tests
* clean error handling

Avoid unnecessary abstractions.

Do not create microservices unless there is a clear reason.

Start as a modular monolith.

---

# Repository Structure

Use something approximately like:

```text
ai-sales-agent/
│
├── apps/
│   ├── web/
│   └── api/
│
├── packages/
│   └── shared/
│
├── infrastructure/
│
├── docker-compose.yml
├── README.md
└── .env.example
```

Backend:

```text
api/
├── app/
│   ├── graphql/
│   ├── agents/
│   ├── llm/
│   ├── rag/
│   ├── embeddings/
│   ├── tools/
│   ├── documents/
│   ├── products/
│   ├── conversations/
│   ├── prompts/
│   ├── evaluations/
│   ├── auth/
│   ├── db/
│   └── core/
```

Keep AI-specific logic separate from generic business logic.

---

# Important Development Rules

1. Do not generate huge amounts of code blindly.
2. Before implementing a major component, explain the architecture briefly.
3. Implement one phase at a time.
4. After each phase, run tests and verify that the application works.
5. Fix errors before moving to the next phase.
6. Prefer simple solutions over premature complexity.
7. Do not add technologies just because they are trendy.
8. Every AI feature should have a clear reason to exist.
9. Do not hard-code business-specific data.
10. Keep the system extensible for multiple organizations.
11. Write meaningful tests for the important AI/business logic.
12. Keep README documentation updated.

---

# First Task

Do NOT start by implementing the entire application.

First:

1. Analyze the requirements.
2. Propose the final architecture.
3. Propose the database schema.
4. Propose the repository structure.
5. Explain the agent architecture.
6. Explain the RAG architecture.
7. Explain how tool calling will work.
8. Explain how MCP can later integrate with the architecture.
9. Define Phase 1 in detail.
10. Identify potential architectural risks and trade-offs.

Then wait for approval before implementing Phase 1.

When implementation begins, create the project skeleton and make sure it can run locally with:

```bash
docker compose up
```

The first milestone should be a working:

```text
Next.js
   ↓
GraphQL
   ↓
FastAPI
   ↓
PostgreSQL
```

application with authentication and organization/agent management.

Do not implement RAG, MCP, evaluation, billing, or complex agents until the foundation is working.
