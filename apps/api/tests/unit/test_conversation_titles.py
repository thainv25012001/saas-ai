"""The text rules behind a conversation's title.

Pure functions, unit-tested: the arq job that calls them (see
`tests/integration/test_title_conversation_task.py`) is about the database and
the provider, and every question about *what a title may contain* is settled
here where it costs nothing to ask.
"""

from app.conversations.titles import (
    FALLBACK_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    TITLE_MAX_TOKENS,
    UNTITLED,
    build_title_messages,
    clean_title,
    fallback_title,
)


class TestCleanTitle:
    def test_keeps_an_ordinary_title(self):
        assert clean_title("Pricing for the starter plan") == "Pricing for the starter plan"

    def test_rejects_whitespace_only(self):
        # The caller distinguishes None from a string: None means "the model
        # gave us nothing usable, fall back", and a blank title stored as-is
        # would be a row with no label that nothing ever retries.
        assert clean_title("   \n\t ") is None

    def test_rejects_an_empty_string(self):
        assert clean_title("") is None

    def test_strips_surrounding_quotes(self):
        # Models habitually answer "give me a title" with a quoted string.
        assert clean_title('"Pricing questions"') == "Pricing questions"
        assert clean_title("'Pricing questions'") == "Pricing questions"

    def test_leaves_quotes_that_are_part_of_the_title(self):
        assert clean_title('The "starter" plan') == 'The "starter" plan'

    def test_collapses_a_multi_line_answer_to_one_line(self):
        # A title is rendered in a single-line list row. A model that answers
        # with a preamble and a newline would otherwise store both.
        assert clean_title("Pricing questions\nabout the starter plan") == (
            "Pricing questions about the starter plan"
        )

    def test_truncates_to_the_column_bound(self):
        # `conversations.title` is String(255); anything longer is a database
        # error at write time, not a cosmetic problem.
        cleaned = clean_title("x" * 400)
        assert cleaned is not None
        assert len(cleaned) == TITLE_MAX_LENGTH


class TestFallbackTitle:
    def test_keeps_a_short_message_whole(self):
        assert fallback_title("How much is the starter plan?") == "How much is the starter plan?"

    def test_truncates_a_long_message_on_a_word_boundary(self):
        title = fallback_title("word " * 60)
        assert len(title) <= FALLBACK_MAX_LENGTH
        assert title.endswith("word")

    def test_collapses_whitespace(self):
        assert fallback_title("How much\n  is  it?") == "How much is it?"

    def test_a_message_with_no_words_still_produces_a_label(self):
        # `message` is validated as min_length=1, which "   " satisfies. A
        # fallback that returned "" here would leave the row blank forever --
        # the fallback is also what stops the job recurring.
        assert fallback_title("   ") == UNTITLED

    def test_a_single_word_longer_than_the_bound_is_still_cut(self):
        # No word boundary to cut on. Truncating anyway beats storing 400
        # characters of one token.
        assert len(fallback_title("x" * 400)) <= FALLBACK_MAX_LENGTH


class TestBuildTitleMessages:
    def test_sends_the_exchange_as_a_single_user_turn(self):
        messages = build_title_messages("how much?", "Forty dollars.")

        # One user turn, not a user/assistant pair: providers differ on how
        # strictly they police role alternation, and a titling request is not
        # a continuation of the conversation being titled.
        assert [m.role for m in messages] == ["user"]
        assert "how much?" in messages[0].text_content
        assert "Forty dollars." in messages[0].text_content

    def test_bounds_what_it_sends(self):
        # The job runs after the first turn, but nothing stops a first turn
        # from being 8000 characters. A title's cost must not scale with it.
        messages = build_title_messages("q" * 9000, "a" * 9000)

        assert len(messages[0].text_content) < 5_000

    def test_the_token_budget_is_small(self):
        # A title is a handful of words. A large budget here would let a
        # rambling model turn every conversation into a paid essay.
        assert TITLE_MAX_TOKENS <= 64
