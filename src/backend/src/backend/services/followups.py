"""One bounded retrieval rewrite from user questions; no assistant claims become evidence."""

import calendar
import re


def needs_context(question: str) -> bool:
    return bool(
        re.match(
            r"\s*(?:what about|how about|and\b|what does (?:that|it)|how does (?:that|it)|compare (?:that|it)|why (?:is|was) that)",
            question,
            re.IGNORECASE,
        )
    )


def rewrite(question: str, history: list[tuple[str, str]]) -> str | None:
    if not needs_context(question):
        return question
    previous = next(
        (
            content
            for role, content in reversed(history[-20:])
            if role == "user" and not needs_context(content)
        ),
        None,
    )
    if previous is None:
        return None
    if re.search(r"\blast month\b", question, re.IGNORECASE):
        pattern = r"\b(" + "|".join(calendar.month_name[1:]) + r")\s+(\d{4})\b"
        match = re.search(pattern, previous, re.IGNORECASE)
        if match:
            month = next(
                index
                for index, name in enumerate(calendar.month_name)
                if name.casefold() == match.group(1).casefold()
            )
            year = int(match.group(2))
            month, year = (12, year - 1) if month == 1 else (month - 1, year)
            previous = (
                previous[: match.start()]
                + f"{calendar.month_name[month]} {year}"
                + previous[match.end() :]
            )
    return (previous[:2000] + "\nFollow-up: " + question)[:4000]
