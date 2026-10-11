import json
import os
import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend import observability
from backend.chat import AnswerFormatError, ChatCompletion, ChatProvider
from backend.services import answer_prompts
from backend.services.calculations import Calculation, calculate
from backend.services.knowledge import SearchHit, retrieval_settings

NO_EVIDENCE_ANSWER = (
    "I couldn't find enough supporting information in the indexed documents to answer."
)
MIN_RETRIEVAL_SCORE = retrieval_settings()[2]
MAX_HISTORY_MESSAGES = 20


@dataclass(frozen=True)
class GroundedAnswer:
    content: str
    citations: list[SearchHit]
    completion: ChatCompletion | None
    outcome: str = "answered"


class AnswerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal[
        "answered",
        "partially_answered",
        "clarification_needed",
        "insufficient_evidence",
    ]
    answer: str = Field(min_length=1, max_length=16000)
    calculation: Calculation | None = None


def needs_complete_scope(question: str) -> bool:
    return bool(
        re.search(
            r"\b(?:all|every)\s+(?:records?|payslips?|invoices?|documents?|payments?|entries|rows?)\b|\b(?:total|count|sum|average)\b",
            question,
            re.IGNORECASE,
        )
    )


def supporting_evidence(
    hits: list[SearchHit],
    document_id: UUID | None = None,
    document_ids: list[UUID] | None = None,
) -> list[SearchHit]:
    minimum_relevance = retrieval_settings()[2]

    selected = (
        document_ids
        if document_ids is not None
        else ([document_id] if document_id is not None else None)
    )

    # Explicitly selected documents supply context independently of query similarity.
    def is_primary(hit: SearchHit) -> bool:
        if selected is not None:
            return hit.document.id in selected
        return (
            hit.score >= minimum_relevance
            or hit.exact_match
            or ("lexical" in hit.provenance and hit.rank_score >= 1 / 61)
        )

    primary = [
        hit for hit in hits if "neighbour" not in hit.provenance and is_primary(hit)
    ]
    supported_versions = {
        hit.chunk.document_version_id
        for hit in primary
        if getattr(hit.chunk, "document_version_id", None) is not None
    }
    return primary + [
        hit
        for hit in hits
        if "neighbour" in hit.provenance
        and getattr(hit.chunk, "document_version_id", None) in supported_versions
    ]


def _fit_evidence_budget(
    evidence: list[SearchHit], history: list[tuple[str, str]]
) -> list[SearchHit]:
    from backend.services.chunking import minilm_tokenizer

    tokenizer = minilm_tokenizer()
    context_budget = min(
        32768, max(512, int(os.environ.get("RAG_CONTEXT_TOKEN_BUDGET", "8192")))
    )
    instructions = max(
        256,
        min(32768, int(os.environ.get("RAG_INSTRUCTION_TOKEN_RESERVE", "1600"))),
    )
    generation = max(
        256,
        min(32768, int(os.environ.get("RAG_GENERATION_TOKEN_RESERVE", "1200"))),
    )
    history_tokens = sum(
        len(tokenizer.encode(f"{role}: {content}").ids)
        for role, content in history[-MAX_HISTORY_MESSAGES:]
    )
    history_reserve = max(
        history_tokens,
        min(32768, int(os.environ.get("RAG_HISTORY_TOKEN_RESERVE", "512"))),
    )
    budget = max(128, context_budget - instructions - generation - history_reserve)
    selected = []
    consumed = 0
    for hit in evidence:
        size = len(tokenizer.encode(hit.chunk.content).ids)
        if consumed + size <= budget:
            selected.append(hit)
            consumed += size
    return selected


@observability.RAG_DURATION.time()
def answer_question(
    question: str,
    history: list[tuple[str, str]],
    hits: list[SearchHit],
    provider: ChatProvider | None,
    *,
    document_id: UUID | None = None,
    document_ids: list[UUID] | None = None,
    complete_scope: bool = False,
) -> GroundedAnswer:
    evidence = _fit_evidence_budget(
        supporting_evidence(hits, document_id, document_ids), history
    )
    if needs_complete_scope(question) and not complete_scope:
        return GroundedAnswer(
            "I cannot establish a complete total or list from a sample of passages. Please narrow the source or document scope and period so I can check every indexed record within it.",
            [],
            None,
            "clarification_needed",
        )
    if not evidence:
        return GroundedAnswer(NO_EVIDENCE_ANSWER, [], None, "insufficient_evidence")
    if provider is None:
        raise ValueError("A chat provider is required when supporting evidence exists")

    system_prompt = _grounded_prompt(evidence)
    if complete_scope:
        system_prompt += "\nCoverage: these excerpts include every indexed chunk in the requested scope. Counts/totals must still identify units, periods and records from that evidence; do not count documents as records."
    messages = [("system", system_prompt)]
    messages.extend(history[-MAX_HISTORY_MESSAGES:])
    messages.append(("human", question))
    raw = ""
    outcome = "answered"
    used: list[int] = []
    content = ""
    for attempt in range(2):
        with observability.LLM_DURATION.time():
            structured_call = getattr(provider, "generate_structured", None)
            completion = (
                structured_call(messages)
                if structured_call is not None
                else provider.generate(messages)
            )
        raw = re.sub(
            r"<think>.*?</think>\s*", "", completion.content, flags=re.DOTALL
        ).strip()
        if raw.startswith("```json") and raw.endswith("```"):
            raw = raw[7:-3].strip()
        structured = raw.startswith("{")
        try:
            if not structured and getattr(
                provider, "requires_structured_answers", False
            ):
                raise ValueError("response is not JSON")
            outcome = "answered"
            if structured:
                payload = AnswerPayload.model_validate(json.loads(raw))
                outcome, raw = payload.outcome, payload.answer
                if payload.calculation is not None:
                    try:
                        raw = calculate(payload.calculation, evidence)
                    except ValueError:
                        return GroundedAnswer(
                            "Please clarify the amounts, currency, net/gross meaning and pay periods to compare. I could not verify the requested operands in the cited evidence.",
                            [],
                            completion,
                            "clarification_needed",
                        )

            used = []

            def keep_valid_citation(
                match: re.Match, citation_indices=used, is_structured=structured
            ) -> str:
                index = int(match.group(1)) - 1
                if not 0 <= index < len(evidence):
                    if not is_structured:
                        return ""
                    raise ValueError("citation marker is outside supplied evidence")
                if index not in citation_indices:
                    citation_indices.append(index)
                return match.group(0)

            content = re.sub(r"\[C(\d+)\]", keep_valid_citation, raw).strip()
            if outcome not in {"clarification_needed", "insufficient_evidence"} and (
                not content or not used
            ):
                if structured:
                    raise ValueError("answer is missing a valid citation")
                content = NO_EVIDENCE_ANSWER
                used = []
                outcome = "insufficient_evidence"
            break
        except (ValueError, ValidationError) as error:
            if attempt:
                raise AnswerFormatError(
                    "Chat provider answer format remained invalid after one repair"
                ) from error
            messages = [
                *messages,
                ("assistant", completion.content),
                (
                    "human",
                    answer_prompts.REPAIR_INSTRUCTIONS
                    + " Return one JSON object matching the required schema.",
                ),
            ]

    if content != NO_EVIDENCE_ANSWER:
        markers = {index + 1: position + 1 for position, index in enumerate(used)}
        content = re.sub(
            r"\[C(\d+)\]", lambda match: f"[C{markers[int(match.group(1))]}]", content
        )
    return GroundedAnswer(
        content, [evidence[index] for index in used], completion, outcome
    )


def _grounded_prompt(hits: list[SearchHit]) -> str:
    excerpts = []
    groups: list[list[tuple[int, SearchHit]]] = []
    for index, hit in enumerate(hits, start=1):
        if (
            groups
            and getattr(groups[-1][-1][1].chunk, "document_version_id", None)
            is not None
            and getattr(groups[-1][-1][1].chunk, "document_version_id", None)
            == getattr(hit.chunk, "document_version_id", None)
            and getattr(groups[-1][-1][1].chunk, "end_offset", 0)
            > getattr(hit.chunk, "start_offset", 0)
        ):
            groups[-1].append((index, hit))
        else:
            groups.append([(index, hit)])

    for group in groups:
        index, hit = group[0]
        location = []
        if hit.source is not None:
            location.append(f"source: {hit.source.name}")
        location.append(f"document: {hit.document.title}")
        if getattr(hit.chunk, "document_version_id", None):
            location.append(f"version: {hit.chunk.document_version_id}")
        if getattr(hit.document, "external_modified_at", None):
            location.append(
                f"source modified (not an effective date): {hit.document.external_modified_at}"
            )
        if hit.chunk.page is not None:
            location.append(f"page: {hit.chunk.page}")
        if hit.chunk.section:
            location.append(f"section: {hit.chunk.section}")
        content = hit.chunk.content
        for _, following in group[1:]:
            overlap = max(
                0,
                getattr(hit.chunk, "end_offset", 0)
                - getattr(following.chunk, "start_offset", 0),
            )
            content += following.chunk.content[
                min(overlap, len(following.chunk.content)) :
            ]
        markers = ", ".join(f"C{marker}" for marker, _ in group)
        excerpts.append(f"[{markers}] ({'; '.join(location)})\n{content}")

    return (
        f"Prompt version: {answer_prompts.PROMPT_VERSION}.\n"
        + answer_prompts.BASE_INSTRUCTIONS
        + "\n"
        + answer_prompts.CONFLICT_AND_SCOPE
        + "\n"
        + answer_prompts.OUTPUT_CONTRACT
        + "\n\nEvidence:\n"
        + "\n\n".join(excerpts)
    )
