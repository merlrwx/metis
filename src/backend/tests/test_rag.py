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
