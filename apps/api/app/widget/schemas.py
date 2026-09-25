"""`WidgetSettingsService.update`'s input (spec §3, §7's dashboard form)."""

from pydantic import BaseModel, Field, field_validator

from app.db.models.widget import WidgetPosition


class UpdateWidgetSettingsInput(BaseModel):
    enabled: bool
    allowed_origins: list[str]
    # Storage is always lower-case (`_lowercase_brand_color`) so a later
    # comparison or render never has to case-fold it again. The pattern
    # itself accepts either case, since that is what a color picker's hex
    # output or a pasted value from a design tool commonly looks like.
    brand_color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    position: WidgetPosition
    title: str | None = Field(default=None, max_length=60)
    daily_message_cap: int = Field(ge=1, le=100_000)

    @field_validator("title", mode="before")
    @classmethod
    def _strip_title(cls, value: str | None) -> str | None:
        """Stripped before length validation; a blank or whitespace-only
        title means "no title" (falls back to the agent's own name), not a
        stored empty string."""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("brand_color", mode="after")
    @classmethod
    def _lowercase_brand_color(cls, value: str) -> str:
        return value.lower()
