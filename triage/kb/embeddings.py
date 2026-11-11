"""Embedding providers behind one small interface.

  gemini        gemini-embedding-001, with task types: documents are embedded as
                RETRIEVAL_DOCUMENT and questions as RETRIEVAL_QUERY (asymmetric retrieval)
  azure_openai  an Azure OpenAI embedding deployment (config swap only)
  hash          offline and deterministic: hashed bag of words. A LEXICAL baseline for
                tests, CI and demos without an API key. It matches words, not meaning.

Vectors are L2-normalised here, so inner product == cosine similarity in the index.
(Gemini only normalises at its full 3072 dimensions, so this matters at 768.)
"""

import hashlib
import re
from typing import Any, Protocol

import numpy as np

from triage.config import Settings
from triage.llm import LLMConfigError


class Embedder(Protocol):
    name: str  # stored in the index manifest; a mismatch at load time is an error
    dims: int

    def documents(self, texts: list[str]) -> np.ndarray: ...
    def query(self, text: str) -> np.ndarray: ...


def normalise(vectors: np.ndarray) -> np.ndarray:
    vectors = np.asarray(vectors, dtype="float32")
    if vectors.ndim == 1:
        vectors = vectors[None, :]
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.where(norms == 0, 1, norms)


class LangChainEmbedder:
    """Wraps a LangChain Embeddings object, adding per-call kwargs (task types)."""

    def __init__(
        self,
        embeddings: Any,
        name: str,
        dims: int,
        doc_kwargs: dict[str, Any] | None = None,
        query_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._lc = embeddings
        self.name = name
        self.dims = dims
        self._doc_kwargs = doc_kwargs or {}
        self._query_kwargs = query_kwargs or {}

    def documents(self, texts: list[str]) -> np.ndarray:
        return normalise(np.array(self._lc.embed_documents(texts, **self._doc_kwargs)))

    def query(self, text: str) -> np.ndarray:
        return normalise(np.array(self._lc.embed_query(text, **self._query_kwargs)))[0]


_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "my",
        "me",
        "not",
        "of",
        "on",
        "or",
        "our",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "this",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "which",
        "who",
        "will",
        "with",
        "you",
        "your",
    }
)


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


class HashEmbedder:
    """Deterministic hashed bag-of-words (unigrams + bigrams). Uses hashlib, not hash(),
    because Python's hash() is randomised per process and would break saved indexes."""

    def __init__(self, dims: int = 1024) -> None:
        self.dims = dims
        self.name = f"hash:{dims}"

    def _vector(self, text: str) -> np.ndarray:
        words = [_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOPWORDS]
        features = words + [f"{a}_{b}" for a, b in zip(words, words[1:], strict=False)]
        vec = np.zeros(self.dims, dtype="float32")
        for feature in features:
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            value = int.from_bytes(digest, "little")
            vec[value % self.dims] += 1.0 if (value >> 63) & 1 else -1.0
        return vec

    def documents(self, texts: list[str]) -> np.ndarray:
        return normalise(np.stack([self._vector(t) for t in texts]))

    def query(self, text: str) -> np.ndarray:
        return normalise(self._vector(text))[0]


def make_embedder(settings: Settings) -> Embedder:
    """Build the configured embedder. No network call happens here."""
    if settings.embed_provider == "hash":
        return HashEmbedder()

    if settings.embed_provider == "gemini":
        if not settings.gemini_api_key:
            raise LLMConfigError("Gemini embeddings need GEMINI_API_KEY in .env")
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        lc = GoogleGenerativeAIEmbeddings(
            model=settings.embed_model,
            google_api_key=settings.gemini_api_key.get_secret_value(),
        )
        dims = settings.embed_dims
        return LangChainEmbedder(
            lc,
            name=f"gemini:{settings.embed_model}:{dims}",
            dims=dims,
            doc_kwargs={"task_type": "RETRIEVAL_DOCUMENT", "output_dimensionality": dims},
            query_kwargs={"task_type": "RETRIEVAL_QUERY", "output_dimensionality": dims},
        )

    required = {
        "AZURE_OPENAI_ENDPOINT": settings.azure_openai_endpoint,
        "AZURE_OPENAI_API_KEY": settings.azure_openai_api_key,
        "AZURE_OPENAI_API_VERSION": settings.azure_openai_api_version,
        "EMBED_MODEL (the embedding deployment)": settings.embed_model,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise LLMConfigError(f"Azure OpenAI embeddings need {', '.join(missing)} in .env")
    from langchain_openai import AzureOpenAIEmbeddings

    lc = AzureOpenAIEmbeddings(
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key.get_secret_value(),  # type: ignore[union-attr]
        api_version=settings.azure_openai_api_version,
        azure_deployment=settings.embed_model,
        dimensions=settings.embed_dims,
    )
    return LangChainEmbedder(
        lc, name=f"azure:{settings.embed_model}:{settings.embed_dims}", dims=settings.embed_dims
    )
