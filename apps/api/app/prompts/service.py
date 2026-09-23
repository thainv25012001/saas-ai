import uuid
from collections.abc import Sequence

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
        it and activated immediately."""
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
            created_by=self.tenant.user_id,
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
            select(func.max(PromptVersion.version)).where(
                PromptVersion.prompt_id == prompt_id,
                PromptVersion.organization_id == self.tenant.organization_id,
            )
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
            created_by=self.tenant.user_id,
        )
        self.session.add(version)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            # Two concurrent calls can compute the same max(version) + 1.
            raise ConflictError("a version with that number already exists") from exc
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
                PromptVersion.organization_id == self.tenant.organization_id,
                PromptVersion.is_active.is_(True),
            )
            .values(is_active=False)
        )
        await self.session.flush()
        version.is_active = True
        await self.session.flush()
        return version

    async def get_version(self, version_id: uuid.UUID) -> PromptVersion:
        """Load one version by id, two-layer scoped like every other lookup
        here: the explicit `organization_id` predicate plus this session's
        own RLS. A version id from another organization is indistinguishable
        from one that does not exist -- `NotFoundError`, not a more specific
        error, for the same reason `get_prompt`/`activate_version` never
        confirm whether a foreign id belongs to someone else.

        Used by `ChatService._resolve_system_prompt` (Phase 6) to resolve a
        pinned version *before* checking it belongs to the calling agent's
        prompt -- that check needs the row's own `prompt_id`, which only
        this lookup can supply.
        """
        result = await self.session.execute(
            select(PromptVersion).where(
                PromptVersion.id == version_id,
                PromptVersion.organization_id == self.tenant.organization_id,
            )
        )
        version = result.scalar_one_or_none()
        if version is None:
            raise NotFoundError("prompt version not found")
        return version

    async def list_versions(self, prompt_id: uuid.UUID) -> list[PromptVersion]:
        """Every version of one prompt, newest first. A prompt id from another
        organization is `NotFoundError` via `get_prompt`, as everywhere else
        here. Used by the Evaluations dashboard to pick the version a run pins
        (docs/PHASE-6.md §5)."""
        await self.get_prompt(prompt_id)
        return (await self.versions_by_prompt([prompt_id]))[prompt_id]

    async def versions_by_prompt(
        self, prompt_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[PromptVersion]]:
        """Versions for several prompts in one query, newest first per prompt,
        keyed by prompt id (every requested id present, possibly empty). The
        batched form behind `Prompt.versions`' dataloader. It does not raise
        for an unknown or foreign id -- the explicit `organization_id`
        predicate (plus RLS) simply returns no rows for it."""
        by_prompt: dict[uuid.UUID, list[PromptVersion]] = {pid: [] for pid in prompt_ids}
        if not prompt_ids:
            return by_prompt
        result = await self.session.execute(
            select(PromptVersion)
            .where(
                PromptVersion.prompt_id.in_(list(prompt_ids)),
                PromptVersion.organization_id == self.tenant.organization_id,
            )
            .order_by(PromptVersion.prompt_id, PromptVersion.version.desc())
        )
        for version in result.scalars().all():
            by_prompt[version.prompt_id].append(version)
        return by_prompt

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
