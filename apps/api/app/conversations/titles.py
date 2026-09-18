"""What a conversation's title may contain, and what is asked of the model to
get one.

Kept apart from the arq job that uses it (`app.workers.tasks`) because these
are the parts with no database and no provider in them: every question about
quoting, length and whitespace is settled here, in unit tests that cost
nothing to run, leaving the job itself to be about ordering and failure.
"""

from app.llm.types import Message as LLMMessage

#: `conversations.title` is `String(255)`. Exceeding it is a database error at
#: write time, not a cosmetic problem, so every path truncates to it.
TITLE_MAX_LENGTH = 255

#: The fallback is a quotation of what the user typed, not a summary of it, so
#: it is held to a tighter bound than a generated title -- a list row is one
#: line, and 255 characters of someone's question is not a label.
FALLBACK_MAX_LENGTH = 120

#: A title is a handful of words. A generous budget here would let a rambling
#: model turn every conversation into a paid essay.
TITLE_MAX_TOKENS = 32

#: What a conversation with nothing quotable in it is called. `message` is
#: validated as `min_length=1`, which a string of spaces satisfies, and the
#: fallback is also what stops the job recurring -- so it must always produce
#: something.
UNTITLED = "Untitled conversation"

#: Each half of the exchange is cut to this before being sent. The job runs
#: after the first turn, but a first turn may be 8000 characters, and the cost
#: of a title must not scale with the length of the question that earned it.
_EXCERPT_MAX_LENGTH = 2_000

TITLE_SYSTEM_PROMPT = (
    "You write short titles for saved conversations. Reply with the title "
    "only: a noun phrase of at most eight words, no quotation marks, no "
    "trailing punctuation, in the language of the conversation."
)

_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"))


def _collapse(text: str) -> str:
    """One line, single-spaced. A title is rendered in a single-line row, so a
    model that answers with a preamble and a newline must not store both."""
    return " ".join(text.split())


def _truncate(text: str, limit: int) -> str:
    """Cut to `limit`, preferring a word boundary.

    Falls back to a hard cut when there is no boundary to use -- a single
    token longer than the bound has none, and storing it whole is worse than
    cutting it.
    """
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = cut.rfind(" ")
    # A boundary in the first few characters means the rest of the text is one
    # long token; cutting there would leave a title of one short word.
    if boundary > limit // 2:
        return cut[:boundary].rstrip()
    return cut.rstrip()


def clean_title(raw: str) -> str | None:
    """The model's answer, made fit to store -- or `None` if there is nothing
    usable in it.

    `None` is distinct from a blank string on purpose: it is the caller's
    signal to fall back. A blank title written as-is would be a row with no
    label that nothing ever retries, because a written title is exactly what
    stops the job running again.
    """
    title = _collapse(raw)
    for opening, closing in _QUOTE_PAIRS:
        # Only a matched pair wrapping the whole string. Quotes inside the
        # title are the user's own words and stay.
        if len(title) >= 2 and title.startswith(opening) and title.endswith(closing):
            title = title[1:-1].strip()
            break
    if not title:
        return None
    return _truncate(title, TITLE_MAX_LENGTH)


def fallback_title(first_user_message: str) -> str:
    """What the conversation is called when the model could not say.

    Always returns something: see `UNTITLED`.
    """
    quoted = _truncate(_collapse(first_user_message), FALLBACK_MAX_LENGTH)
    return quoted or UNTITLED


def build_title_messages(user_text: str, assistant_text: str) -> list[LLMMessage]:
    """The one turn sent to the model.

    A single `user` message holding both halves, not a user/assistant pair:
    providers differ on how strictly they police role alternation, and this is
    a request *about* a conversation rather than a continuation of it.
    """
    exchange = (
        f"Question: {_truncate(_collapse(user_text), _EXCERPT_MAX_LENGTH)}\n"
        f"Answer: {_truncate(_collapse(assistant_text), _EXCERPT_MAX_LENGTH)}"
    )
    return [LLMMessage.text("user", exchange)]
