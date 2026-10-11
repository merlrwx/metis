"""Shared helpers for synthetic evaluation reports."""


def failure_stage(*, expected_retrieved: bool, expected_supplied: bool) -> str | None:
    if not expected_retrieved:
        return "evidence_not_retrieved"
    if not expected_supplied:
        return "evidence_retrieved_but_discarded"
    return "evidence_supplied_answer_or_citation_incorrect"


def provider_details(embedding, chat_provider=None) -> dict[str, str | None]:
    return {
        "embedding_provider": embedding.model_id.split(":", 1)[0],
        "embedding_model": embedding.model_id,
        "chat_provider": chat_provider.model_id.split(":", 1)[0]
        if chat_provider
        else None,
        "chat_model": chat_provider.model_id if chat_provider else None,
    }


def answer_matches_case(case: dict, result) -> bool:
    """Check deterministic expectations against a fixed-evidence answer result."""
    outcomes = (
        case["outcome"] if isinstance(case["outcome"], list) else [case["outcome"]]
    )
    answer = result.content.casefold()
    return (
        result.outcome in outcomes
        and len(result.citations) >= case["citations"]
        and all(claim.casefold() in answer for claim in case["claims"])
        and not any(
            claim.casefold() in answer for claim in case.get("forbidden_claims", [])
        )
    )
