from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Any

DEFAULT_CHUNK_SIZE = 1000
DEFAULT_CHUNK_OVERLAP = 150
MAX_SECTION_LENGTH = 512


@lru_cache(maxsize=1)
def minilm_tokenizer():
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_str(
        files("backend").joinpath("resources/minilm-tokenizer.json").read_text()
    )
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


@dataclass(frozen=True)
class TextChunk:
    content: str
    start_offset: int
    end_offset: int
    page: int | None
    section: str | None
    metadata: dict[str, Any]


def split_document(
    text: str,
    extraction_metadata: dict[str, Any],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    max_tokens: int | None = None,
) -> list[TextChunk]:
    if chunk_size <= 0 or chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValueError(
            "Chunk overlap must be nonnegative and smaller than chunk size"
        )

    if max_tokens is not None and max_tokens < 1:
        raise ValueError("Token budget must be positive")

    records = extraction_metadata.get("pages") or extraction_metadata.get("sections")
    if records:
        segments = records
    else:
        segments = [{"start": 0, "end": len(text)}]

    chunks = []
    for record in segments:
        segment_start = max(0, record.get("start", 0))
        segment_end = min(len(text), record.get("end", len(text)))
        if segment_end <= segment_start:
            continue

        position = segment_start
        while position < segment_end:
            target_end = min(position + chunk_size, segment_end)
            if target_end < segment_end:
                boundary = max(
                    text.rfind("\n", position + chunk_size // 2, target_end),
                    text.rfind(" ", position + chunk_size // 2, target_end),
                )
                if boundary > position:
                    target_end = boundary

            if max_tokens is not None:
                encoded = minilm_tokenizer().encode(
                    text[position:target_end], add_special_tokens=False
                )
                if len(encoded.ids) > max_tokens:
                    target_end = position + encoded.offsets[max_tokens - 1][1]

            raw = text[position:target_end]
            leading_trim = len(raw) - len(raw.lstrip())
            trailing_trim = len(raw.rstrip())
            content = raw.strip()
            if content:
                start_offset = position + leading_trim
                end_offset = position + trailing_trim
                metadata = {}
                if "page" in record:
                    metadata["page"] = record["page"]
                if record.get("level") is not None:
                    metadata["heading_level"] = record["level"]
                chunks.append(
                    TextChunk(
                        content=content,
                        start_offset=start_offset,
                        end_offset=end_offset,
                        page=record.get("page"),
                        section=(
                            record["title"][:MAX_SECTION_LENGTH]
                            if isinstance(record.get("title"), str)
                            else None
                        ),
                        metadata=metadata,
                    )
                )

            if target_end >= segment_end:
                break
            position = max(position + 1, target_end - chunk_overlap)
            while position < target_end and text[position].isspace():
                position += 1

    return chunks
