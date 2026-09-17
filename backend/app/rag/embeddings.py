"""Local text embedding service. No database or HTTP awareness.

Phase 6 deliverable: turns chunk text into 384-dimensional embedding
vectors using a local ``sentence-transformers`` model. The model is loaded
lazily, once per process, via a thread-safe module-level singleton, and
reused for every subsequent call — never re-instantiated per call, per
text, or per request.

The single entry point ``embed_texts()`` accepts either one string or a
list of strings and returns the corresponding shape (one vector, or a list
of vectors), preserving input order so Phase 7 can zip results back onto
``ChunkRecord`` objects purely by position. Output is plain Python
``list``/``float`` — directly usable by ``DocumentChunk.embedding``
(``vector(384)``) without any numpy transformation at the call site.

Nothing here is wired into any route, service, or the chunker. Nothing is
written to the database.
"""

from __future__ import annotations

import threading
from typing import Union

from app.config import settings

# Fixed expected embedding dimension — a code constant, never a config value.
# Must match document_chunks.embedding's vector(384) column exactly. The model
# name is configurable (settings.EMBEDDING_MODEL); the dimension is derived
# from the loaded model itself and compared against this constant at load time.
EXPECTED_EMBEDDING_DIMENSION = 384


class EmbeddingModelLoadError(Exception):
    """Raised when the embedding model cannot be loaded at all.

    Covers an invalid model name, a genuinely first-ever run with no internet
    and an empty local cache, a corrupted local cache, or insufficient memory.
    The message is actionable — never a raw, unhandled library traceback.
    """


class EmbeddingDimensionMismatchError(Exception):
    """Raised when the loaded model's native output dimension != 384.

    Guards against someone changing EMBEDDING_MODEL to an incompatible model
    and only discovering the mismatch as a confusing database error later
    (Phase 7's inserts expect vector(384)).
    """


class EmbeddingGenerationError(Exception):
    """Raised when encoding succeeds in loading but fails during inference.

    Covers a corrupted/mid-load model object or any unexpected low-level
    encoding failure. Re-raised as this clear, catchable error rather than
    leaking an opaque library exception.
    """


_model: object | None = None
_model_lock = threading.Lock()


def _validate_dimension(dimension: int) -> None:
    """Raise ``EmbeddingDimensionMismatchError`` if ``dimension`` is not 384.

    Exposed as a plain function so the test suite can exercise the guard
    against a fabricated wrong dimension in isolation, without loading a
    second real model.
    """
    if dimension != EXPECTED_EMBEDDING_DIMENSION:
        raise EmbeddingDimensionMismatchError(
            f"Embedding model reports dimension {dimension}, but the "
            f"document_chunks.embedding column is vector("
            f"{EXPECTED_EMBEDDING_DIMENSION}). "
            f"Configured model: {settings.EMBEDDING_MODEL!r}. "
            f"Change EMBEDDING_MODEL to a model with a native output "
            f"dimension of {EXPECTED_EMBEDDING_DIMENSION} (e.g. "
            f"all-MiniLM-L6-v2)."
        )


def _load_model() -> None:
    """Load the model once, populate the module-level singleton, and validate
    its output dimension. Any failure surfaces as ``EmbeddingModelLoadError``."""
    global _model
    from sentence_transformers import SentenceTransformer

    try:
        model = SentenceTransformer(
            settings.EMBEDDING_MODEL,
            device=settings.EMBEDDING_DEVICE,
        )
    except Exception as exc:
        raise EmbeddingModelLoadError(
            f"Failed to load embedding model {settings.EMBEDDING_MODEL!r} "
            f"on device {settings.EMBEDDING_DEVICE!r}. If this is the first "
            f"run on this machine, a one-time model download (~80 MB) from "
            f"Hugging Face Hub is required and needs internet access; on "
            f"later runs the model loads from the local cache "
            f"(~/.cache/huggingface by default) and needs no network. "
            f"Details: {exc}"
        ) from exc

    try:
        dimension = _get_model_dimension(model)
    except Exception as exc:
        raise EmbeddingModelLoadError(
            f"Loaded embedding model {settings.EMBEDDING_MODEL!r} but could "
            f"not read its output dimension. Details: {exc}"
        ) from exc

    _validate_dimension(dimension)
    _model = model


def _get_model_dimension(model: object) -> int:
    """Return the model's native output embedding dimension.

    Tries the modern ``get_embedding_dimension`` API first and falls back to
    the legacy ``get_sentence_embedding_dimension`` (renamed in
    sentence-transformers v6) so the service works across library versions.
    """
    if hasattr(model, "get_embedding_dimension"):
        return int(model.get_embedding_dimension())
    if hasattr(model, "get_sentence_embedding_dimension"):
        return int(model.get_sentence_embedding_dimension())
    raise AttributeError(
        "loaded model exposes neither get_embedding_dimension() nor "
        "get_sentence_embedding_dimension()"
    )


def _get_model() -> object:
    """Return the process-wide model singleton, loading it if necessary.

    Thread-safe: a lock guards the load-if-not-loaded path so concurrent
    first-callers (possible once Phase 7 serves the live app) never both
    trigger a model load.
    """
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                _load_model()
    return _model


def _validate_text(text: str) -> None:
    """Raise ``ValueError`` for empty or whitespace-only strings.

    Phase 5's ``MIN_CHUNK_SIZE`` filtering normally prevents near-empty
    content from reaching this point; this is a defensive guard so a
    degenerate input can never silently produce a meaningless vector.
    """
    if not text.strip():
        raise ValueError(
            "Cannot embed empty or whitespace-only text. "
            "Chunk content must contain at least one visible character."
        )


def embed_texts(
    texts: Union[str, list[str]],
) -> Union[list[float], list[list[float]]]:
    """Embed one string (returns one 384-dim vector) or a list of strings
    (returns one 384-dim vector per string, in input order).

    Empty/whitespace-only strings raise ``ValueError``. An empty list returns
    ``[]`` — a valid no-op. Invalid input types raise ``TypeError``.

    Output is plain Python ``list`` of ``float`` values (never numpy), directly
    compatible with ``DocumentChunk.embedding`` (``vector(384)``).
    """
    if isinstance(texts, str):
        _validate_text(texts)
        texts = [texts]
        single = True
    elif isinstance(texts, list):
        if not texts:
            return []
        single = False
    else:
        raise TypeError(
            "embed_texts() expects a single string or a list of strings, "
            f"got {type(texts).__name__}."
        )

    for item in texts:
        if not isinstance(item, str):
            raise TypeError(
                "embed_texts() batch input must contain only strings, "
                f"got {type(item).__name__}."
            )
        _validate_text(item)

    model = _get_model()

    try:
        encoded = model.encode(texts, batch_size=settings.EMBEDDING_BATCH_SIZE)
    except Exception as exc:
        raise EmbeddingGenerationError(
            f"Embedding generation failed for {len(texts)} text(s) using "
            f"model {settings.EMBEDDING_MODEL!r} on device "
            f"{settings.EMBEDDING_DEVICE!r}. Details: {exc}"
        ) from exc

    vectors = [[float(value) for value in row] for row in encoded]

    expected = EXPECTED_EMBEDDING_DIMENSION
    for index, vector in enumerate(vectors):
        if len(vector) != expected:
            raise EmbeddingDimensionMismatchError(
                f"Model {settings.EMBEDDING_MODEL!r} produced a vector of "
                f"length {len(vector)} at batch position {index}; expected "
                f"{expected}. This should be impossible for a 384-dim model "
                f"and indicates a corrupted or unexpected model object."
            )

    return vectors[0] if single else vectors