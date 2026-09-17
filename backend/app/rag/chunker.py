"""Page-aware text chunking. Pure functions only — no database or HTTP awareness.

Phase 5 deliverable: turns an ordered list of ``(page_number, text)`` pairs
(such as ``parser.py``'s per-page extraction output) into an ordered list of
in-memory chunk records. Nothing is persisted here; Phase 7 will attach
embeddings to each record's ``content`` and write rows to ``document_chunks``.

Every record's field names and types mirror ``DocumentChunk``'s non-embedding
columns exactly so that Phase 7 can construct ORM instances directly with no
field mapping or renaming step.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class ChunkRecord:
    """In-memory chunk record matching ``DocumentChunk``'s non-embedding columns.

    - ``document_id``: parent document, provided by the caller.
    - ``chunk_index``: global, 0-based, sequential across the whole document.
    - ``page_number``: 1-indexed source page; a chunk never spans more than one page.
    - ``content``: normalized chunk text, trimmed, no leading/trailing whitespace.
    """

    document_id: uuid.UUID
    chunk_index: int
    page_number: int
    content: str


def normalize_text(text: str) -> str:
    """Collapse all runs of whitespace (tabs, blank lines, irregular spacing)
    into single spaces and strip leading/trailing whitespace."""
    return " ".join(text.split())


def _overlap_words(words: list[str], chunk_overlap: int) -> list[str]:
    """Return the longest suffix of ``words`` whose joined length is at most
    ``chunk_overlap`` characters. Word-bounded, so the realized length varies
    slightly around the configured value. Returns an empty list if even a
    single trailing word exceeds the overlap budget."""
    seed: list[str] = []
    chars = 0
    for word in reversed(words):
        cost = len(word) if not seed else len(word) + 1
        if chars + cost > chunk_overlap:
            break
        seed.insert(0, word)
        chars += cost
    return seed


def _chunk_page(
    normalized: str,
    chunk_size: int,
    chunk_overlap: int,
    min_chunk_size: int,
) -> list[str]:
    """Chunk one normalized page's text into content strings.

    A page's text is split on whitespace and words are accumulated into
    chunks without ever exceeding ``chunk_size`` characters. Each chunk after
    the first re-includes the trailing words of the previous chunk (word-based
    overlap, up to ``chunk_overlap`` characters). Pages with less than
    ``min_chunk_size`` normalized characters produce zero chunks.
    """
    if len(normalized) < min_chunk_size:
        return []

    words = normalized.split(" ")
    chunks: list[list[str]] = []
    consumed = 0
    while consumed < len(words):
        seed = _overlap_words(chunks[-1], chunk_overlap) if chunks else []
        current = list(seed)
        chars = sum(len(w) for w in current) + max(0, len(current) - 1)
        taken = 0
        while consumed < len(words):
            word = words[consumed]
            cost = len(word) if not current else 1 + len(word)
            if current and chars + cost > chunk_size:
                break
            current.append(word)
            chars += cost
            consumed += 1
            taken += 1
        if taken == 0:
            current = [words[consumed]]
            consumed += 1
        chunks.append(current)

    return [" ".join(chunk_words) for chunk_words in chunks]


def _merge_trailing_chunk(
    chunks: list[str],
    chunk_size: int,
    min_chunk_size: int,
) -> list[str]:
    """Merge an undersized final chunk into its predecessor.

    If the last chunk is smaller than ``min_chunk_size`` it is merged into the
    previous chunk. When a plain concatenation would push the combined chunk
    past ``chunk_size``, words are redistributed from the end of the previous
    chunk to the front of the trailing chunk until the trailing chunk reaches
    ``min_chunk_size``. This keeps the result deterministic, never violates the
    maximum chunk size, never leaves a minuscule trailing fragment behind, and
    never drops any document content (words only move between the two chunks).

    If the page has only one chunk, or the final chunk already meets
    ``min_chunk_size``, the list is returned unchanged. As a last resort, when
    redistribution cannot satisfy all constraints at once (a pathological
    oversized-word case), the chunks are left unchanged rather than violating
    the maximum size constraint.
    """
    if len(chunks) < 2:
        return chunks

    tail = chunks[-1]
    if len(tail) >= min_chunk_size:
        return chunks

    prev = chunks[-2]
    if len(prev) + 1 + len(tail) <= chunk_size:
        return chunks[:-2] + [prev + " " + tail]

    prev_words = prev.split(" ")
    tail_words = tail.split(" ")
    while len(" ".join(tail_words)) < min_chunk_size and prev_words:
        word = prev_words.pop()
        new_tail_words = [word] + tail_words
        new_prev_words = prev_words
        over_tail = len(" ".join(new_tail_words)) > chunk_size
        over_drain = bool(new_prev_words) and len(" ".join(new_prev_words)) < min_chunk_size
        if over_tail or over_drain:
            prev_words.append(word)
            break
        tail_words = new_tail_words

    new_prev = " ".join(prev_words)
    new_tail = " ".join(tail_words)
    if prev_words:
        return chunks[:-2] + [new_prev, new_tail]
    if len(new_tail) <= chunk_size and len(new_tail) >= min_chunk_size:
        return chunks[:-2] + [new_tail]
    return chunks


def chunk_document(
    document_id: uuid.UUID,
    pages: list[tuple[int, str]],
    *,
    chunk_size: int = settings.CHUNK_SIZE,
    chunk_overlap: int = settings.CHUNK_OVERLAP,
    min_chunk_size: int = settings.MIN_CHUNK_SIZE,
) -> list[ChunkRecord]:
    """Turn an ordered list of ``(page_number, text)`` pairs into chunk records.

    ``chunk_index`` is a single, globally increasing 0-based counter across the
    entire document (not reset per page). Skipped pages contribute no records.

    Every page's text is normalized before chunking; a page below
    ``min_chunk_size`` characters contributes zero chunks without raising.

    Returns an empty list when every page is skipped. That empty list is a
    valid, distinguishable return value — not an exception — but it is a real
    signal: Phase 7 must treat it as a reason to mark the document
    ``status = "failed"`` rather than leaving it ``ready`` with no retrievable
    content.

    Raises:
        ValueError: if ``page_number`` is not a positive integer or ``text``
            is not a string (a caller bug), or if the chunking parameters are
            invalid (``chunk_overlap`` not strictly smaller than
            ``chunk_size``, or ``min_chunk_size`` outside
            ``1 <= min_chunk_size < chunk_size``).
    """
    if chunk_size < 1:
        raise ValueError(f"chunk_size must be at least 1 (got {chunk_size})")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValueError(
            f"chunk_overlap must satisfy 0 <= chunk_overlap < chunk_size "
            f"(got overlap={chunk_overlap}, size={chunk_size})"
        )
    if min_chunk_size < 1 or min_chunk_size >= chunk_size:
        raise ValueError(
            f"min_chunk_size must satisfy 1 <= min_chunk_size < chunk_size "
            f"(got min={min_chunk_size}, size={chunk_size})"
        )

    records: list[ChunkRecord] = []
    chunk_index = 0
    for page_number, text in pages:
        if not isinstance(page_number, int) or page_number < 1:
            raise ValueError(
                f"page_number must be a positive integer (got {page_number!r})"
            )
        if not isinstance(text, str):
            raise ValueError(
                f"text must be a string for page {page_number} "
                f"(got {type(text).__name__})"
            )

        normalized = normalize_text(text)
        page_chunks = _chunk_page(
            normalized, chunk_size, chunk_overlap, min_chunk_size
        )
        page_chunks = _merge_trailing_chunk(page_chunks, chunk_size, min_chunk_size)

        for content in page_chunks:
            records.append(
                ChunkRecord(
                    document_id=document_id,
                    chunk_index=chunk_index,
                    page_number=page_number,
                    content=content,
                )
            )
            chunk_index += 1

    return records