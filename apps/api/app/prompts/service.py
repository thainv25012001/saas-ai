import uuid

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.core.ids import uuid7
from app.core.tenancy import TenantContext
from app.db.models import Prompt, PromptVersion
from app.prompts.schemas import CreatePromptInput, CreateVersionInput


class PromptService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    async def list_prompts(self) -> list[Prompt]:
        result = await self.session.execute(
            select(Prompt)
            .where(Prompt.organization_id == self.tenant.organization_id)
            .order_by(Prompt.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_prompt(self, prompt_id: uuid.UUID) -> Prompt:
        result = await self.session.execute(
            select(Prompt).where(
                Prompt.id == prompt_id,
                Prompt.organization_id == self.tenant.organization_id,
            )
        )
        prompt = result.scalar_one_or_none()
        if prompt is None:
            raise NotFoundError("prompt not found")
        return prompt

    async def create_prompt(self, data: CreatePromptInput) -> Prompt:
        """A prompt is never useful without text, so version 1 is created with
        it and activated immediately.

        `created_by` is left unset here: nothing in this task's scope calls
        this service with a `tenant.user_id` verified to reference a `users`
        row (real requests derive it from a validated access token's `sub`
        claim -- see app/auth/dependencies.py -- but this task adds no route
        that does so yet). Wiring a real value through is for the caller that
        eventually has one to supply explicitly.
        """
        prompt = Prompt(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            name=data.name,
            key=data.key,
            description=data.description,
        )
        version = PromptVersion(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            prompt_id=prompt.id,
            version=1,
            system_prompt=data.system_prompt,
            is_active=True,
            created_by=None,
        )
        self.session.add_all([prompt, version])
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ConflictError(f"a prompt with key '{data.key}' already exists") from exc
        return prompt

    async def create_version(self, prompt_id: uuid.UUID, data: CreateVersionInput) -> PromptVersion:
        await self.get_prompt(prompt_id)
        highest = await self.session.execute(
            select(func.max(PromptVersion.version)).where(PromptVersion.prompt_id == prompt_id)
        )
        version = PromptVersion(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            prompt_id=prompt_id,
            version=(highest.scalar_one() or 0) + 1,
            system_prompt=data.system_prompt,
            variables=data.variables,
            is_active=False,  # explicit activation is a separate, auditable act
            notes=data.notes,
            created_by=None,  # see create_prompt's docstring
        )
        self.session.add(version)
        await self.session.flush()
        return version

    async def activate_version(self, version_id: uuid.UUID) -> PromptVersion:
        result = await self.session.execute(
            select(PromptVersion).where(
                PromptVersion.id == version_id,
                PromptVersion.organization_id == self.tenant.organization_id,
            )
        )
        version = result.scalar_one_or_none()
        if version is None:
            raise NotFoundError("prompt version not found")

        # Deactivate first: the partial unique index rejects two active rows,
        # so the order of these two statements is load-bearing.
        await self.session.execute(
            update(PromptVersion)
            .where(
                PromptVersion.prompt_id == version.prompt_id,
                PromptVersion.is_active.is_(True),
            )
            .values(is_active=False)
        )
        await self.session.flush()
        version.is_active = True
        await self.session.flush()
        return version

    async def active_version(self, prompt_id: uuid.UUID) -> PromptVersion:
        result = await self.session.execute(
            select(PromptVersion).where(
                PromptVersion.prompt_id == prompt_id,
                PromptVersion.organization_id == self.tenant.organization_id,
                PromptVersion.is_active.is_(True),
            )
        )
        version = result.scalar_one_or_none()
        if version is None:
            raise NotFoundError("no active version for this prompt")
        return version
