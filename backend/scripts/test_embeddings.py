"""Deterministic verification script for the Phase 6 embedding service.

Run from backend/:

    .\\.venv\\Scripts\\python.exe scripts\\test_embeddings.py

No live database, no HTTP server, no mocks of the model: the real local
``all-MiniLM-L6-v2`` sentence-transformers model is exercised. On a genuine
first run on a given machine the model (~80 MB) is downloaded once from
Hugging Face Hub; on every later run it loads from the local cache offline.
Tests 1-10 follow the Phase 6 spec (section 10).
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

results: list[tuple[str, bool, str]] = []

_SENTENCES = [
    "The annual fee for this credit card is one hundred dollars.",
    "Coverage begins on the first day of the month after enrollment.",
    "Claims must be filed within thirty days of the incident.",
    "The deductible applies to every claim made under this policy.",
    "Renewal notices are sent to the address on file each year.",
]


def record(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))


def model_cache_dir() -> Path:
    try:
        from huggingface_hub import constants

        hub_cache = Path(constants.HF_HUB_CACHE)
    except Exception:
        hub_cache = Path.home() / ".cache" / "huggingface" / "hub"
    return hub_cache / "models--sentence-transformers--all-MiniLM-L6-v2"


def test_01_model_loads() -> None:
    import app.rag.embeddings as embeddings

    try:
        from app.rag.embeddings import embed_texts

        embed_texts("A short sentence to trigger the one-time model load.")
    except Exception as exc:  # pragma: no cover - failure path
        record(
            "TEST 1 - Model loads successfully",
            False,
            f"raised {type(exc).__name__}: {exc}",
        )
        return

    singleton = embeddings._model is not None
    record("TEST 1 - Model loads successfully", singleton, "singleton populated")


def test_02_single_text_embedding() -> None:
    from app.rag.embeddings import embed_texts

    vector = embed_texts(_SENTENCES[0])
    ok = isinstance(vector, list) and len(vector) == 384 and all(
        isinstance(x, float) for x in vector
    )
    record(
        "TEST 2 - Single text embedding",
        ok,
        f"list of {len(vector) if isinstance(vector, list) else '?'} floats",
    )

    again = embed_texts(_SENTENCES[0])
    identical = again == vector
    record(
        "Determinism - same sentence twice is bit-identical",
        identical,
        "exact match" if identical else "VECTORS DIFFER",
    )


def test_03_batch_embedding() -> None:
    from app.rag.embeddings import embed_texts

    vectors = embed_texts(_SENTENCES)
    ok = (
        isinstance(vectors, list)
        and len(vectors) == 5
        and all(isinstance(v, list) and len(v) == 384 for v in vectors)
    )
    record(
        "TEST 3 - Batch embedding",
        ok,
        f"{len(vectors) if isinstance(vectors, list) else '?'} vectors, "
        f"all length 384",
    )

    batch_again = embed_texts(_SENTENCES)
    record(
        "Determinism - same batch twice is bit-identical",
        batch_again == vectors,
        "exact match" if batch_again == vectors else "VECTORS DIFFER",
    )


def test_04_correct_dimension() -> None:
    from app.rag.embeddings import embed_texts

    single = embed_texts(_SENTENCES[0])
    batch = embed_texts(_SENTENCES)
    bad = [len(v) for v in batch if len(v) != 384]
    ok = len(single) == 384 and not bad
    record(
        "TEST 4 - Correct dimension (single + batch)",
        ok,
        f"single={len(single)}; batch lengths all "
        f"{'384' if not bad else bad}",
    )


def test_05_output_format() -> None:
    import numpy as np

    from app.rag.embeddings import embed_texts

    vector = embed_texts(_SENTENCES[0])
    batch = embed_texts(_SENTENCES)
    plain = (
        isinstance(vector, list)
        and not isinstance(vector, np.ndarray)
        and all(isinstance(x, float) and not isinstance(x, np.float32) for x in vector)
        and isinstance(batch, list)
        and not isinstance(batch, np.ndarray)
        and all(
            isinstance(v, list) and not isinstance(v, np.ndarray)
            and all(isinstance(x, float) and not isinstance(x, np.float32) for x in v)
            for v in batch
        )
    )
    record(
        "TEST 5 - Output format (plain list/float)",
        plain,
        "no numpy.ndarray/numpy.float32 in output",
    )


def test_06_empty_input_handling() -> None:
    from app.rag.embeddings import embed_texts

    empty_raises = False
    whitespace_raises = False
    try:
        embed_texts("")
    except ValueError:
        empty_raises = True
    try:
        embed_texts("   \n\t ")
    except ValueError:
        whitespace_raises = True

    empty_batch = embed_texts([])

    bad_types = 0
    for bad in [None, 42, 3.14, ["ok", 7], ["ok", ["nested"]], ("ok",), True]:
        try:
            embed_texts(bad)  # type: ignore[arg-type]
        except TypeError:
            bad_types += 1

    ok = (
        empty_raises
        and whitespace_raises
        and empty_batch == []
        and empty_batch is not None
        and bad_types == 7
    )
    record(
        "TEST 6 - Empty/whitespace strings raise ValueError, "
        "[] returns [], bad types raise TypeError",
        ok,
        f"empty str ok={empty_raises}, whitespace ok={whitespace_raises}, "
        f"empty list -> {empty_batch!r}, {bad_types}/7 bad types rejected",
    )


def test_07_ordering_preserved() -> None:
    from app.rag.embeddings import embed_texts

    batch = embed_texts(_SENTENCES)
    max_deviation = 0.0
    for index, sentence in enumerate(_SENTENCES):
        individual = embed_texts(sentence)
        for a, b in zip(individual, batch[index]):
            max_deviation = max(max_deviation, abs(a - b))
    ok = max_deviation < 1e-4
    record(
        "TEST 7 - Ordering preserved (batch index == individual index)",
        ok,
        f"max abs deviation {max_deviation:.2e} vs batch positions",
    )


def test_08_model_reuse() -> None:
    import app.rag.embeddings as embeddings

    first = embeddings._get_model()
    second = embeddings._get_model()
    same_object = first is second
    record(
        "TEST 8 - Model reuse / no repeated initialization",
        same_object and construction_count["n"] == 1,
        f"same singleton object: {same_object}; "
        f"SentenceTransformer constructed {construction_count['n']} time(s)",
    )


def test_09_dimension_validation() -> None:
    from app.rag.embeddings import (
        EmbeddingDimensionMismatchError,
        _validate_dimension,
    )

    raised = False
    try:
        _validate_dimension(385)
    except EmbeddingDimensionMismatchError:
        raised = True

    no_raise = True
    try:
        _validate_dimension(384)
    except EmbeddingDimensionMismatchError:
        no_raise = False

    record(
        "TEST 9 - Dimension validation triggers correctly",
        raised and no_raise,
        f"384 accepted, 385 -> {raised}",
    )


def test_10_configuration_behavior() -> None:
    import inspect

    from app.config import settings
    from app.rag.embeddings import _load_model, embed_texts

    embed_src = inspect.getsource(embed_texts)
    load_src = inspect.getsource(_load_model)

    wired = (
        isinstance(settings.EMBEDDING_BATCH_SIZE, int)
        and settings.EMBEDDING_BATCH_SIZE == 32
        and isinstance(settings.EMBEDDING_DEVICE, str)
        and settings.EMBEDDING_DEVICE == "cpu"
        and "batch_size=settings.EMBEDDING_BATCH_SIZE" in embed_src
        and "device=settings.EMBEDDING_DEVICE" in load_src
    )

    original_batch_size = settings.EMBEDDING_BATCH_SIZE
    settings.EMBEDDING_BATCH_SIZE = 1
    try:
        vectors = embed_texts(_SENTENCES[:3])
        nondefault_ok = len(vectors) == 3 and all(len(v) == 384 for v in vectors)
    finally:
        settings.EMBEDDING_BATCH_SIZE = original_batch_size

    record(
        "TEST 10 - Configuration behavior "
        "(batch size + device read from settings)",
        wired and nondefault_ok,
        f"defaults batch={settings.EMBEDDING_BATCH_SIZE}, "
        f"device={settings.EMBEDDING_DEVICE!r}; "
        f"batch_size=1 encode of 3 texts -> "
        f"{'3x384' if nondefault_ok else 'MISSHAPEN'}",
    )


construction_count = {"n": 0}


def _install_construction_counter() -> None:
    """Count real SentenceTransformer constructions (no mocks of behavior)."""
    import sentence_transformers

    real_init = sentence_transformers.SentenceTransformer.__init__

    def counting_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        construction_count["n"] += 1
        real_init(self, *args, **kwargs)

    sentence_transformers.SentenceTransformer.__init__ = counting_init


def main() -> None:
    print("Phase 6 embedding verification")
    print("Model: sentence-transformers/all-MiniLM-L6-v2 (dim 384, device: cpu)")
    if model_cache_dir().exists():
        print("Local model cache present - no download expected.")
    else:
        print(
            "First run on this machine: the model (~80 MB) will be downloaded "
            "once from Hugging Face Hub. This is expected and not a hang."
        )
        print("Downloading model - first run only...")

    _install_construction_counter()

    test_01_model_loads()
    test_02_single_text_embedding()
    test_03_batch_embedding()
    test_04_correct_dimension()
    test_05_output_format()
    test_06_empty_input_handling()
    test_07_ordering_preserved()
    test_08_model_reuse()
    test_09_dimension_validation()
    test_10_configuration_behavior()

    print()
    print(f"{'CHECK':<78}{'RESULT':<8}")
    print("-" * 86)
    failed = 0
    for name, passed, detail in results:
        status = "PASS" if passed else "FAIL"
        if not passed:
            failed += 1
        print(f"{name:<78}{status:<8}")
        print(f"  -> {detail}")
    print("-" * 86)
    print(
        f"Total: {len(results)} checks, {len(results) - failed} passed, "
        f"{failed} failed"
    )
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()