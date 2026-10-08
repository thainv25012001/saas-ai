from app.db.models.agent import Agent, AgentConfig, AgentStatus
from app.db.models.api_key import ApiKey
from app.db.models.citation import MessageCitation
from app.db.models.conversation import (
    Conversation,
    ConversationChannel,
    ConversationStatus,
    Message,
    MessageRole,
    UsageEvent,
    UsageKind,
)
from app.db.models.document import (
    Document,
    DocumentChunk,
    DocumentSourceType,
    DocumentStatus,
)
from app.db.models.evaluation import (
    EvalCase,
    EvalDataset,
    EvalResult,
    EvalRun,
    EvalRunStatus,
)
from app.db.models.gmail_connection import GmailConnection
from app.db.models.lead import Lead, LeadStatus
from app.db.models.membership import Membership, MembershipRole
from app.db.models.organization import Organization
from app.db.models.product import Product, ProductAvailability
from app.db.models.product_import import ProductImport, ProductImportStatus
from app.db.models.prompt import Prompt, PromptVersion
from app.db.models.supplier import Supplier, SupplierMapping
from app.db.models.supplier_quote import SupplierMessage, SupplierQuote, SupplierReport
from app.db.models.tool import AgentToolLink, MessageToolCall, Tool, ToolType
from app.db.models.user import User
from app.db.models.widget import WidgetPosition, WidgetSettings

# `Message` is the ORM row for the `messages` table. `app.llm.types.Message`
# is the unrelated LLM wire type (provider-agnostic chat message). A module
# that needs both in scope should import this one as `ConversationMessage`
# rather than shadowing one of them.
ConversationMessage = Message

__all__ = [
    "Agent",
    "AgentConfig",
    "AgentStatus",
    "AgentToolLink",
    "ApiKey",
    "Conversation",
    "ConversationChannel",
    "ConversationMessage",
    "ConversationStatus",
    "Document",
    "DocumentChunk",
    "DocumentSourceType",
    "DocumentStatus",
    "EvalCase",
    "EvalDataset",
    "EvalResult",
    "EvalRun",
    "EvalRunStatus",
    "GmailConnection",
    "Lead",
    "LeadStatus",
    "Membership",
    "MembershipRole",
    "Message",
    "MessageCitation",
    "MessageRole",
    "MessageToolCall",
    "Organization",
    "Product",
    "ProductAvailability",
    "ProductImport",
    "ProductImportStatus",
    "Prompt",
    "PromptVersion",
    "Supplier",
    "SupplierMapping",
    "SupplierMessage",
    "SupplierQuote",
    "SupplierReport",
    "Tool",
    "ToolType",
    "UsageEvent",
    "UsageKind",
    "User",
    "WidgetPosition",
    "WidgetSettings",
]
