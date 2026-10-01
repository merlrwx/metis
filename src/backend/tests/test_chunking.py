import pytest
from backend.services.chunking import split_document


def test_chunks_preserve_offsets_overlap_and_page_metadata():
    text = "Medication incident response steps are listed here for staff."
    chunks = split_document(
        text,
        {"pages": [{"page": 4, "start": 0, "end": len(text)}]},
        chunk_size=32,
        chunk_overlap=8,
    )

    assert len(chunks) > 1
    assert all(
        text[chunk.start_offset : chunk.end_offset] == chunk.content for chunk in chunks
    )
    assert all(chunk.page == 4 for chunk in chunks)
    assert chunks[0].start_offset == 0
    assert chunks[1].start_offset < chunks[0].end_offset


def test_chunks_do_not_cross_page_or_section_boundaries():
    text = "First page text.\n\nSecond page text."
    boundary = text.index("Second")
    pages = split_document(
        text,
        {
            "pages": [
                {"page": 1, "start": 0, "end": boundary - 2},
                {"page": 2, "start": boundary, "end": len(text)},
            ]
        },
        chunk_size=100,
        chunk_overlap=10,
    )

    assert [chunk.page for chunk in pages] == [1, 2]
    assert pages[0].content == "First page text."
    assert pages[1].content == "Second page text."

    sections = split_document(
        "Incident response and recovery steps.",
        {
            "sections": [
                {"title": "Incident response", "level": 1, "start": 0, "end": 18},
                {
                    "title": "Recovery",
                    "level": 2,
                    "start": 22,
                    "end": 38,
                },
            ]
        },
        chunk_size=100,
        chunk_overlap=10,
    )
    assert [chunk.section for chunk in sections] == ["Incident response", "Recovery"]


def test_long_section_titles_are_bounded():
    title = "x" * 1000
    chunks = split_document(
        "A section with a long title.",
        {"sections": [{"title": title, "start": 0, "end": 28}]},
    )

    assert len(chunks[0].section) == 512


@pytest.mark.parametrize(
    ("chunk_size", "chunk_overlap"),
    [(0, 0), (10, -1), (10, 10)],
)
def test_invalid_chunk_strategy_is_rejected(chunk_size, chunk_overlap):
    with pytest.raises(ValueError, match="Chunk overlap"):
        split_document("text", {}, chunk_size, chunk_overlap)
