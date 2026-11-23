"""Text -> vector embeddings.

Two implementations behind one ``Embedder`` interface (a ``typing.Protocol``: any class with
these attributes and methods fits, no inheritance needed):

* ``FastEmbedEmbedder`` (default): BAAI/bge-small-en-v1.5, a real embedding model run locally
  with ONNX Runtime on the CPU. It matches meaning, not just words ("time off" finds "annual
  leave"). No API key, no per-call cost, and document text never leaves the server.
* ``HashingEmbedder``: dependency-free word matching. Used by fast tests, and kept in the
  retrieval eval as a baseline that shows what a real model adds.

Each embedder has a ``name`` that is stored with every chunk, so vectors from different models
are never compared with each other.
"""

import hashlib
import re
from collections.abc import Sequence
from typing import Protocol

import numpy as np

# Below this cosine similarity, a passage is treated as "not about the question" and the
# assistant says it found nothing. Every model has its own score scale (bge-small rarely goes
# below 0.3 even for unrelated text), so never reuse one model's cut-off for another.
# bge-small: 0.60, calibrated on this project's demo set with evals/retrieval (see README,
# "Retrieval quality"): 91% accuracy against 84% for the 0.55 used with the same model in the
# HRMS project, because here unrelated questions score up to ~0.65. 0.600 and 0.625 tied; the
# lower one hides fewer real answers. In-sample, with only 8 unanswerable questions: re-check
# whenever the corpus or the dataset grows.
DEFAULT_MIN_SCORES: dict[str, float] = {
    "hashing-v1": 0.06,
    "BAAI/bge-small-en-v1.5": 0.60,
}


class Embedder(Protocol):
    name: str
    dim: int

    def embed_documents(self, texts: Sequence[str]) -> list[np.ndarray]: ...

    def embed_query(self, text: str) -> np.ndarray: ...


class FastEmbedEmbedder:
    """A sentence-embedding model run locally (model files are downloaded once, then cached)."""

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5", cache_dir: str | None = None):
        from fastembed import TextEmbedding  # imported lazily: unit tests don't need it

        supported = {m["model"]: m for m in TextEmbedding.list_supported_models()}
        if model_name not in supported:
            raise ValueError(f"Unsupported embedding model: {model_name}")
        self.name = model_name
        self.dim = int(supported[model_name]["dim"])
        self._model = TextEmbedding(model_name, cache_dir=cache_dir)

    def embed_documents(self, texts: Sequence[str]) -> list[np.ndarray]:
        return [_normalize(v) for v in self._model.passage_embed(list(texts), batch_size=32)]

    def embed_query(self, text: str) -> np.ndarray:
        # BGE models embed queries with an instruction prefix; query_embed adds it.
        return _normalize(next(iter(self._model.query_embed(text))))


STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from",
    "how", "i", "if", "in", "is", "it", "my", "of", "on", "or", "our", "the", "their",
    "this", "to", "was", "what", "when", "where", "which", "who", "will", "with", "you", "your",
})  # fmt: skip


class HashingEmbedder:
    """Feature-hashed word and word-pair counts. Captures shared words, not meaning."""

    name = "hashing-v1"

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def embed_documents(self, texts: Sequence[str]) -> list[np.ndarray]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed(text)

    def _embed(self, text: str) -> np.ndarray:
        tokens = [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOPWORDS]
        features = tokens + [f"{a} {b}" for a, b in zip(tokens, tokens[1:], strict=False)]

        vector = np.zeros(self.dim, dtype=np.float32)
        for feature in features:
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            h = int.from_bytes(digest, "little")
            sign = 1.0 if h >> 63 else -1.0
            vector[h % self.dim] += sign
        return _normalize(vector)


def _normalize(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 0 else vector


def default_min_score(embedder: Embedder) -> float:
    return DEFAULT_MIN_SCORES.get(embedder.name, 0.5)


def build_embedder(model_name: str, cache_dir: str | None = None) -> Embedder:
    if model_name == HashingEmbedder.name:
        return HashingEmbedder()
    return FastEmbedEmbedder(model_name, cache_dir=cache_dir)
