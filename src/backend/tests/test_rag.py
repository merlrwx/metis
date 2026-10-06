import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from backend.chat import ChatCompletion
from backend.embeddings import HashingEmbeddingProvider
from backend.services.knowledge import SearchHit
from backend.services.rag import (
    MIN_RETRIEVAL_SCORE,
    NO_EVIDENCE_ANSWER,
    answer_question,
)

EVALUATION_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "rag_evaluation.json").read_text()
)


@pytest.mark.parametrize("case", EVALUATION_CASES, ids=lambda case: case["id"])
def test_small_retrieval_evaluation_ranks_expected_evidence(case):
    provider = HashingEmbeddingProvider()
    query = provider.embed_query(case["question"])
    relevant = provider.embed_query(case["relevant"])
    distractor = provider.embed_query(case["distractor"])
    relevant_score = sum(left * right for left, right in zip(query, relevant))
    distractor_score = sum(left * right for left, right in zip(query, distractor))

    assert relevant_score >= MIN_RETRIEVAL_SCORE
    assert relevant_score > distractor_score


class FakeChatProvider:
    model_id = "test-chat-v1"

    def __init__(self, content):
        self.content = content
        self.messages = None

    def generate(self, messages):
        self.messages = messages
        return ChatCompletion(self.content, self.model_id, 12, 7)


def hit(score=0.9):
    return SearchHit(
        chunk=SimpleNamespace(
            id="chunk-id",
            content="Notify the clinical supervisor after an incident.",
            page=2,
            section="Incident response",
        ),
        document=SimpleNamespace(
            id="document-id", title="Incident policy", source_id="source-id"
        ),
        source=SimpleNamespace(id="source-id", name="Policies"),
        score=score,
    )


def test_rag_refuses_without_relevant_evidence_or_valid_citations():
    provider = FakeChatProvider("An uncited answer.")
    unsupported = answer_question("Question", [], [hit(0.1)], provider)
    assert provider.messages is None
    uncited = answer_question("Question", [], [hit()], provider)

    assert unsupported.content == NO_EVIDENCE_ANSWER
    assert unsupported.completion is None
    assert uncited.content == NO_EVIDENCE_ANSWER
    assert uncited.citations == []
    assert uncited.completion.input_tokens == 12


def test_rag_keeps_only_retrieved_citations_and_includes_history():
    provider = FakeChatProvider("Notify the supervisor [C1]. [C4]")
    grounded = answer_question(
        "What do I do?", [("user", "What is the policy?")], [hit()], provider
    )

    assert grounded.content == "Notify the supervisor [C1]."
    assert len(grounded.citations) == 1
    assert grounded.citations[0].document.title == "Incident policy"
    assert provider.messages[1] == ("user", "What is the policy?")
    assert "[C1]" in provider.messages[0][1]
    assert "[C4]" not in grounded.content


def test_explicit_document_answers_payslip_question_despite_lexical_mismatch():
    embedding = HashingEmbeddingProvider()
    question = "How much did I get paid in 2 weeks?"
    text = "PAYSLIP\nPeriod: fortnight\nGross earnings 2500.00\nTax withheld 500.00\nNet payment 2000.00"
    score = sum(
        a * b
        for a, b in zip(embedding.embed_query(question), embedding.embed_query(text))
    )
    payslip = hit(score)
    payslip.chunk.content = text
    assert score < MIN_RETRIEVAL_SCORE
    provider = FakeChatProvider("Your net payment for the fortnight was 2000.00 [C1].")
    grounded = answer_question(
        question, [], [payslip], provider, document_id=payslip.document.id
    )
    assert grounded.citations == [payslip]
    assert provider.messages is not None
    assert text in provider.messages[0][1]
    other = answer_question(
        question, [], [payslip], None, document_id="another-document"
    )
    assert other.content == NO_EVIDENCE_ANSWER and not other.citations


def test_structured_outcomes_and_invalid_citations():
    from backend.chat import ChatProviderError

    provider = FakeChatProvider(
        json.dumps(
            {
                "outcome": "partially_answered",
                "answer": "The policy says notify the supervisor [C1]; escalation timing is missing.",
            }
        )
    )
    result = answer_question("What happens next?", [], [hit()], provider)
    assert result.outcome == "partially_answered"
    provider.content = json.dumps(
        {
            "outcome": "clarification_needed",
            "answer": "Which pay period and net or gross amount do you mean?",
        }
    )
    assert (
        answer_question("How much?", [], [hit()], provider).outcome
        == "clarification_needed"
    )
    provider.content = json.dumps(
        {"outcome": "answered", "answer": "Wrong source [C9]"}
    )
    with pytest.raises(ChatProviderError):
        answer_question("Question", [], [hit()], provider)
    provider.content = json.dumps({"answer": "Missing required outcome [C1]"})
    with pytest.raises(ChatProviderError):
        answer_question("Question", [], [hit()], provider)


def test_exhaustive_question_requires_proven_coverage_before_model_call():
    provider = FakeChatProvider("There are 2 records [C1].")
    result = answer_question("Count all records", [], [hit()], provider)
    assert result.outcome == "clarification_needed"
    assert provider.messages is None
    assert "sample" in result.content


def test_citations_are_renumbered_to_match_rendered_sources():
    first, second = hit(), hit()
    second.document.id = "second"
    second.document.title = "Second"
    provider = FakeChatProvider("Second [C2], first [C1].")
    result = answer_question("Compare", [], [first, second], provider)
    assert result.content == "Second [C1], first [C2]."
    assert result.citations == [second, first]


def test_structured_arithmetic_uses_checked_result_and_both_citations():
    from test_calculations import calculation, evidence

    provider = FakeChatProvider(
        json.dumps(
            {
                "outcome": "answered",
                "answer": "An unverified reason and wrong amount [C1]",
                "calculation": calculation().model_dump(),
            }
        )
    )
    hits = evidence()
    for result in hits:
        result.document.title = "Synthetic payslip"
        result.chunk.page = None
        result.chunk.section = None
    result = answer_question(
        "Compare February net pay with January", [], hits, provider
    )
    assert result.outcome == "answered"
    assert "100.15 AUD" in result.content
    assert "unverified reason" not in result.content
    assert len(result.citations) == 2
    request = calculation()
    request.operands[0].value = "9999"
    provider.content = json.dumps(
        {
            "outcome": "answered",
            "answer": "Bad arithmetic",
            "calculation": request.model_dump(),
        }
    )
    assert (
        answer_question("Compare pay", [], hits, provider).outcome
        == "clarification_needed"
    )


def test_local_thinking_prefix_is_removed_before_structured_validation():
    provider = FakeChatProvider(
        '<think>Internal provider preamble</think>{"outcome":"clarification_needed","answer":"Which period and net or gross pay?"}'
    )
    provider.requires_structured_answers = True
    result = answer_question("How much was I paid?", [], [hit()], provider)
    assert result.outcome == "clarification_needed"
    assert result.content == "Which period and net or gross pay?"
