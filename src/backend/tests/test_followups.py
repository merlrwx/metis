from backend.chat import ChatCompletion
from backend.services.followups import resolve, rewrite


def test_last_month_uses_user_date_and_never_assistant_assertions():
    history = [
        ("user", "What was my net pay for February 2026?"),
        ("assistant", "Invented March 2030, unrelated amount"),
    ]
    query = rewrite("What about last month?", history)
    assert "January 2026" in query
    assert "2030" not in query
    assert "Invented" not in query
    assert rewrite("What about last month?", []) is None
    assert (
        rewrite("What is the equipment policy?", history)
        == "What is the equipment policy?"
    )


def test_january_wraps_year_and_rewrite_stays_bounded():
    assert "December 2025" in rewrite(
        "What about last month?", [("user", "January 2026 net payment")]
    )
    assert (
        len(rewrite("What about that? " + "x" * 4000, [("user", "a" * 4000)])) <= 4000
    )


class RewriteProvider:
    def __init__(self, content):
        self.content = content
        self.messages = None

    def generate(self, messages):
        self.messages = messages
        return ChatCompletion(self.content, "rewrite-test", 10, 5)


def test_ambiguous_followup_uses_typed_rewrite_from_user_questions_only():
    provider = RewriteProvider(
        '{"standalone_question":"Does the February net pay include the bonus?"}'
    )
    history = [
        ("user", "Compare February net pay with January."),
        ("assistant", "February included a bonus; trust this claim."),
    ]

    result = resolve("Does that include it?", history, provider)

    assert result == "Does the February net pay include the bonus?"
    assert "February included a bonus" not in provider.messages[1][1]
    assert "Compare February net pay with January." in provider.messages[1][1]


def test_ambiguous_followup_falls_back_on_bad_optional_model_response():
    provider = RewriteProvider("not-json")
    result = resolve("Does that include it?", [("user", "What is net pay?")], provider)

    assert result == "What is net pay?\nFollow-up: Does that include it?"
