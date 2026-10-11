"""One bounded retrieval rewrite from user questions; no assistant claims become evidence."""

import calendar
import json
import re

from pydantic import BaseModel, ConfigDict, Field


class StandaloneQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    standalone_question: str = Field(max_length=2000)


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


def needs_model_rewrite(question: str) -> bool:
    """References that deterministic concatenation cannot resolve safely."""
    return bool(
        re.search(
            r"\b(?:that|those|these|it|they|them|there|this one|the former|the latter)\b",
            question,
            re.IGNORECASE,
        )
    )


def resolve(question: str, history: list[tuple[str, str]], provider=None) -> str | None:
    """Use deterministic rewriting first and a bounded model call only for ambiguity."""
    deterministic = rewrite(question, history)
    ambiguous = needs_model_rewrite(question)
    if ambiguous and deterministic == question:
        previous = next(
            (content for role, content in reversed(history[-20:]) if role == "user"),
            None,
        )
        deterministic = (
            (previous[:2000] + "\nFollow-up: " + question)[:4000] if previous else None
        )
    if deterministic is None or not ambiguous or provider is None:
        return deterministic

    # Only user questions are inputs to rewriting. Assistant text is never a source
    # for inferred entities, dates, or claims.
    user_history = [
        content[:1500] for role, content in history[-20:] if role == "user"
    ][-6:]
    if not user_history:
        return None
    messages = [
        (
            "system",
            (
                "Rewrite the latest user follow-up as one standalone question. Use only "
                "the earlier user questions to resolve references. Do not answer it, "
                "add facts, or broaden any document scope. If the reference cannot be "
                "resolved, return an empty standalone_question. Return JSON only with "
                "the standalone_question string."
            ),
        ),
        (
            "human",
            json.dumps(
                {"earlier_user_questions": user_history, "follow_up": question[:500]},
                ensure_ascii=False,
            ),
        ),
    ]
    try:
        completion = provider.generate(messages)
        payload = StandaloneQuestion.model_validate(json.loads(completion.content))
        resolved = payload.standalone_question.strip()
        if not resolved:
            return None
        return resolved
    except Exception:  # noqa: BLE001 - optional rewrite failures keep safe fallback
        return deterministic
