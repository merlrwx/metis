from backend.services.followups import rewrite


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
