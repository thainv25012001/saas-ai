"""`create_lead`'s input shape -- both the tool's `args_model` (Task 6,
`app/tools/leads.py`) and `LeadService.create`'s parameter, so a lead is
validated and normalised in exactly one place regardless of which caller
reaches it.
"""

import re
from typing import Self

from pydantic import BaseModel, EmailStr, field_validator, model_validator

# Deliberately narrow. Full E.164 validation/parsing needs a region to
# interpret a national-format number correctly (is "020 7946 0018" a UK
# landmark number or a wrong-country typo?), and an anonymous chat-widget
# visitor's locale isn't something this tool can reliably infer -- guessing
# wrong would silently corrupt or reject a real number, which is worse than
# storing a lightly-cleaned string as given. So normalisation here strips
# only cosmetic formatting a human might type -- spaces, hyphens, dots,
# parentheses -- and keeps an optional leading `+`, without attempting to
# validate that what remains is a real, dialable number in any country.
_PHONE_COSMETIC_CHARS = re.compile(r"[\s\-.()]")
# 3-15 digits: 15 is E.164's own maximum digit count, used here only as a
# sanity ceiling (not as evidence of E.164 conformance); 3 rejects a
# handful of stray digits as clearly not a phone number without guessing
# at any country's real minimum length.
_PHONE_SHAPE = re.compile(r"\+?\d{3,15}")


class CreateLeadInput(BaseModel):
    """§5.2: requiring `name` plus at least one of `email`/`phone` is the
    mechanism that makes the agent ask a follow-up -- the model cannot call
    `create_lead` with only a name, so "what's your email?" falls out of
    this schema rather than out of a prompt instruction the model could
    ignore. Enforced by `_require_contact_method` below (a Pydantic model
    validator), not by a check inside the tool's `execute`, so the same
    guarantee holds for every caller of this model, tool or otherwise.
    """

    name: str
    email: EmailStr | None = None
    phone: str | None = None
    interest: str

    @field_validator("email")
    @classmethod
    def _lowercase_email(cls, value: EmailStr | None) -> str | None:
        # The one normalisation an email genuinely needs: case is not
        # semantically meaningful in the domain part, and effectively never
        # in the local part for the mail providers a real visitor uses, so
        # two submissions differing only by case should land as the same
        # lead, not two.
        return value.lower() if value is not None else None

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _PHONE_COSMETIC_CHARS.sub("", value.strip())
        if not normalized:
            # A phone field that was present but blank once cosmetic
            # characters are removed (e.g. "   " or "()") carries no
            # contact information -- treated as "not provided" so
            # `_require_contact_method` below can still catch a lead with
            # no real way to reach the visitor.
            return None
        if not _PHONE_SHAPE.fullmatch(normalized):
            raise ValueError(
                "phone must contain only digits, with an optional leading '+' "
                "(spaces, hyphens, dots, and parentheses are stripped automatically)"
            )
        return normalized

    @model_validator(mode="after")
    def _require_contact_method(self) -> Self:
        if self.email is None and self.phone is None:
            raise ValueError(
                "at least one of 'email' or 'phone' is required to create a lead "
                "(both are currently missing)"
            )
        return self
