"""Deterministic verification script for the Phase 5 chunker.

Run from backend/:

    .\\.venv\\Scripts\\python.exe scripts\\test_chunker.py

Each test uses synthetic, hardcoded page text (no real PDFs, no database).
Chunking parameters are passed explicitly so results never depend on the
current ``.env`` file.
"""

import subprocess
import sys
import uuid
from pathlib import Path
from typing import get_type_hints

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.config import settings  # noqa: E402
from app.rag.chunker import ChunkRecord, chunk_document  # noqa: E402

TEST_CHUNK_SIZE = 800
TEST_CHUNK_OVERLAP = 150
TEST_MIN_CHUNK_SIZE = 100

WORD_POOL = [
    "the", "document", "contains", "a", "detailed", "description", "of",
    "the", "insurance", "coverage", "and", "its", "terms", "conditions",
    "for", "all", "eligible", "members", "including", "deductibles",
    "premiums", "benefits", "limits", "exclusions", "as", "well", "as",
    "the", "procedures", "required", "to", "file", "a", "claim", "under",
    "this", "policy", "with", "the", "company", "office", "or", "online",
]


def make_prose(min_chars: int) -> str:
    """Deterministically build realistic prose of at least ``min_chars``."""
    words: list[str] = []
    total = 0
    i = 0
    size = len(WORD_POOL)
    while total < min_chars:
        for _ in range(11 + (i % 3)):
            word = WORD_POOL[i % size]
            words.append(word)
            total += len(word) + 1
            i += 1
        words[-1] = words[-1] + "."
        total += 1
    return " ".join(words)


def pages_from_texts(texts: list[str]) -> list[tuple[int, str]]:
    return [(i + 1, text) for i, text in enumerate(texts)]


def chunk(texts: list[str]) -> list[ChunkRecord]:
    return chunk_document(
        uuid.uuid4(),
        pages_from_texts(texts),
        chunk_size=TEST_CHUNK_SIZE,
        chunk_overlap=TEST_CHUNK_OVERLAP,
        min_chunk_size=TEST_MIN_CHUNK_SIZE,
    )


results: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))


def realized_overlap(chunk_a: str, chunk_b: str) -> str:
    a_words = chunk_a.split()
    b_words = chunk_b.split()
    longest = 0
    limit = min(len(a_words), len(b_words))
    for k in range(1, limit + 1):
        if a_words[len(a_words) - k:] == b_words[:k]:
            longest = k
    return " ".join(a_words[len(a_words) - longest:])


def test_01_normal_multi_page() -> None:
    chunks = chunk([make_prose(1500), make_prose(1500), make_prose(1500)])

    assert len(chunks) > 3, "expected multiple chunks across 3 pages"
    assert [c.chunk_index for c in chunks] == list(
        range(len(chunks))
    ), "chunk_index must be globally sequential"
    mismatch = [c for c in chunks if c.page_number not in (1, 2, 3)]
    record(
        "TEST 1 - Normal multi-page document",
        not mismatch,
        f"{len(chunks)} chunks, chunk_index 0..{len(chunks) - 1}",
    )


def test_02_short_document() -> None:
    text = make_prose(300)
    chunks = chunk([text])

    expected = " ".join(text.split())
    assert len(chunks) == 1, "short page must produce exactly one chunk"
    assert chunks[0].content == expected, (
        "single chunk content must equal full normalized page text"
    )
    record(
        "TEST 2 - Short document",
        True,
        f"1 chunk, {len(chunks[0].content)} chars",
    )


def test_03_long_single_page() -> None:
    chunks = chunk([make_prose(5000)])

    assert len(chunks) >= 2, "5,000-char page must produce multiple chunks"
    over = [c for c in chunks if len(c.content) > TEST_CHUNK_SIZE]
    tiny = [c for c in chunks if len(c.content) < TEST_MIN_CHUNK_SIZE]
    record(
        "TEST 3 - Long single page",
        not over and not tiny,
        f"{len(chunks)} chunks, max "
        f"{max(len(c.content) for c in chunks)} chars",
    )


def test_04_page_boundaries() -> None:
    lower = make_prose(1500).lower()
    upper = make_prose(1500).upper()
    chunks = chunk([lower, upper])

    mixed = [
        c
        for c in chunks
        if (c.page_number == 1 and not c.content.islower())
        or (c.page_number == 2 and not c.content.isupper())
    ]
    record(
        "TEST 4 - Page boundaries",
        not mixed,
        f"{len(chunks)} chunks, no cross-page mixing",
    )


def test_05_overlap_behavior() -> None:
    chunks = chunk([" ".join(f"alpha{i}" for i in range(400))])

    assert len(chunks) >= 2, "page must produce at least two chunks"
    overlap = realized_overlap(chunks[0].content, chunks[1].content)
    max_word = max(len(w) for w in chunks[0].content.split())
    close = (
        TEST_CHUNK_OVERLAP - max_word < len(overlap) <= TEST_CHUNK_OVERLAP
    )
    record(
        "TEST 5 - Overlap behavior",
        close,
        f"overlap {len(overlap)} chars vs configured {TEST_CHUNK_OVERLAP}",
    )


def test_06_whitespace_normalization() -> None:
    messy = (
        "\tthis\tpage   has\n\n\nirregular\n  formatting  \r\n"
        "with   blank\nlines\n and   odd\r\nspacing\n"
        "that   must   be normalized   before   chunking "
        "\ninto\ta single   spaced   stream   of   words "
        "without   any tabs   or   line\nbreaks "
        "remaining   anywhere   in\n the   final   chunk\n"
    )
    chunks = chunk([messy])

    expected = " ".join(messy.split())
    assert len(chunks) == 1, "normalized messy page must produce one chunk"
    content = chunks[0].content
    normal = (
        "\t" not in content
        and "\n" not in content
        and "\r" not in content
        and "  " not in content
        and content == expected
    )
    record("TEST 6 - Whitespace normalization", normal, repr(content))


def test_07_tiny_empty_pages() -> None:
    chunks = chunk(["", "   \n\t  ", "just a few words"])

    assert len(chunks) == 0, "all three pages must contribute zero chunks"
    record("TEST 7 - Tiny/empty pages", True, "0 chunks, no exceptions")


def test_08_zero_chunk_document() -> None:
    chunks = chunk(["tiny", "also tiny", "still tiny"])

    assert chunks == [], "every-page-skipped document must return empty list"
    record("TEST 8 - Zero-chunk document", True, "returned []")


def test_09_field_compatibility() -> None:
    from app.models.document_chunk import DocumentChunk

    expected_types = {
        "document_id": uuid.UUID,
        "chunk_index": int,
        "page_number": int,
        "content": str,
    }

    actual_types = get_type_hints(ChunkRecord)

    mismatched: list[str] = []
    for name, type_ in expected_types.items():
        if actual_types.get(name) is not type_:
            mismatched.append(
                f"{name}: got {actual_types.get(name)!r}, expected {type_.__name__}"
            )
        if not hasattr(DocumentChunk, name):
            mismatched.append(f"DocumentChunk has no column {name}")

    chunk_record_columns = set(ChunkRecord.__dataclass_fields__)
    chunk_columns = {
        c.key for c in DocumentChunk.__table__.columns
    } - {"id", "embedding"}
    assert chunk_record_columns == chunk_columns, (
        f"column sets differ: {chunk_record_columns ^ chunk_columns}"
    )

    record(
        "TEST 9 - Field compatibility with DocumentChunk",
        not mismatched,
        "chunk fields map 1:1 onto DocumentChunk non-embedding columns",
    )


def test_10_cascade_fk_reference() -> None:
    record(
        "TEST 10 - Cascade/FK compatibility (reference)",
        True,
        "FK + cascade delete for document_chunks already verified in Phase 3 "
        "via a live DB write with a placeholder embedding; Phase 5 touches no "
        "database, so no re-test is required here.",
    )


def test_11_config_wiring() -> None:
    import inspect

    params = inspect.signature(chunk_document).parameters
    wired = (
        params["chunk_size"].default == settings.CHUNK_SIZE
        and params["chunk_overlap"].default == settings.CHUNK_OVERLAP
        and params["min_chunk_size"].default == settings.MIN_CHUNK_SIZE
    )
    record(
        "Config wiring - chunker defaults read from settings",
        wired,
        f"size={params['chunk_size'].default}, "
        f"overlap={params['chunk_overlap'].default}, "
        f"min={params['min_chunk_size'].default}",
    )


def test_12_config_validation() -> None:
    bad_overlap = _config_load_fails({"CHUNK_OVERLAP": "900"})
    bad_min = _config_load_fails({"MIN_CHUNK_SIZE": "900"})
    record(
        "Config validation - overlap >= size rejected",
        bad_overlap,
        "load-time error raised" if bad_overlap else "NO ERROR RAISED",
    )
    record(
        "Config validation - min >= size rejected",
        bad_min,
        "load-time error raised" if bad_min else "NO ERROR RAISED",
    )


def test_13_trailing_chunk_at_or_above_min_untouched() -> None:
    chunks = chunk_document(
        uuid.uuid4(),
        [(1, " ".join(["word"] * 105))],
        chunk_size=300,
        chunk_overlap=50,
        min_chunk_size=100,
    )
    lengths = [len(c.content) for c in chunks]
    over = [n for n in lengths if n > 300]
    tiny = [n for n in lengths if n < 100]
    record(
        "Edge - trailing chunk at/above MIN stays standalone",
        not over and not tiny and len(chunks) == 2,
        f"chunk lengths: {lengths}",
    )


def test_14_trailing_merge_edge_case() -> None:
    words = [f"w{i:03d}" for i in range(120)]
    chunks = chunk_document(
        uuid.uuid4(),
        [(1, " ".join(words))],
        chunk_size=300,
        chunk_overlap=50,
        min_chunk_size=100,
    )
    lengths = [len(c.content) for c in chunks]
    covered: set[str] = set()
    for c in chunks:
        covered.update(c.content.split())
    missing = [w for w in words if w not in covered]
    over = [n for n in lengths if n > 300]
    tiny = [n for n in lengths if n < 100]
    record(
        "Edge - undersized trailing chunk, merge would exceed CHUNK_SIZE",
        not over and not tiny and not missing and len(chunks) == 3,
        f"chunk lengths: {lengths}; missing words: {len(missing)}",
    )


def test_15_single_chunk_page_rule_not_applied() -> None:
    chunks = chunk([make_prose(120)])
    assert len(chunks) == 1, "single-chunk page must not be merged/removed"
    record("Edge - single-chunk page (merge rule not applicable)", True, "1 chunk")


def test_16_malformed_input() -> None:
    for bad_pages in [
        [(0, "some text")],
        [(-3, "some text")],
        [("one", "some text")],
        [(1, None)],
        [(None, "text")],
    ]:
        try:
            chunk_document(
                uuid.uuid4(),
                bad_pages,
                chunk_size=TEST_CHUNK_SIZE,
                chunk_overlap=TEST_CHUNK_OVERLAP,
                min_chunk_size=TEST_MIN_CHUNK_SIZE,
            )
        except ValueError:
            continue
        assert False, f"expected ValueError for {bad_pages!r}"
    record(
        "Malformed input raises ValueError",
        True,
        "non-positive/non-int page numbers and non-str text rejected",
    )


def main() -> None:
    test_01_normal_multi_page()
    test_02_short_document()
    test_03_long_single_page()
    test_04_page_boundaries()
    test_05_overlap_behavior()
    test_06_whitespace_normalization()
    test_07_tiny_empty_pages()
    test_08_zero_chunk_document()
    test_09_field_compatibility()
    test_10_cascade_fk_reference()
    test_11_config_wiring()
    test_12_config_validation()
    test_13_trailing_chunk_at_or_above_min_untouched()
    test_14_trailing_merge_edge_case()
    test_15_single_chunk_page_rule_not_applied()
    test_16_malformed_input()

    print()
    print(f"{'CHECK':<76}{'RESULT':<8}")
    print("-" * 84)
    failed = 0
    for name, passed, detail in results:
        status = "PASS" if passed else "FAIL"
        if not passed:
            failed += 1
        print(f"{name:<76}{status:<8}")
        print(f"  -> {detail}")
    print("-" * 84)
    print(
        f"Total: {len(results)} checks, {len(results) - failed} passed, "
        f"{failed} failed"
    )
    sys.exit(1 if failed else 0)


def _config_load_fails(env_override: dict[str, str]) -> bool:
    code_lines = [
        "import os",
        *[f"os.environ[{k!r}] = {v!r}" for k, v in env_override.items()],
        "from app.config import settings",
        "print('NO_ERROR')",
    ]
    code = "; ".join(code_lines)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(BACKEND_DIR),
    )
    return "NO_ERROR" not in result.stdout and result.returncode != 0


if __name__ == "__main__":
    main()