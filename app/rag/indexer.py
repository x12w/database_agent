"""Build FAISS index from RAG documentation."""

import os
import signal
from typing import Optional

from app.config import config
from app.rag.chunker import chunk_markdown


def _timeout_handler(signum, frame):
    raise TimeoutError("模型下载超时")


class RAGIndexer:
    """Builds and manages a FAISS index for document search.

    Uses sentence-transformers for embeddings with Chinese support.
    Falls back to keyword search if sentence-transformers is unavailable.
    """

    # HuggingFace mirror for users in regions where HF is blocked
    # Set via .env: HF_ENDPOINT=https://hf-mirror.com
    HF_MIRROR = os.getenv("HF_ENDPOINT", "")

    # Max seconds to wait for model download (default: 120s)
    BUILD_TIMEOUT = int(os.getenv("RAG_BUILD_TIMEOUT", "120"))

    def __init__(self):
        self._chunks: list = []
        self._index: Optional[object] = None  # FAISS index
        self._model: Optional[object] = None  # sentence-transformers model
        self._ready: bool = False
        self._fallback: bool = False

    @property
    def ready(self) -> bool:
        return self._ready

    def build(self, doc_path: str = None) -> bool:
        """Build the RAG index from the markdown document."""
        if doc_path is None:
            doc_path = config.RAG_DOC_PATH

        try:
            with open(doc_path, "r", encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            # Try loading from structure doc as fallback
            try:
                with open(config.RAG_STRUCTURE_DOC_PATH, "r", encoding="utf-8") as f:
                    text = f.read()
            except FileNotFoundError:
                text = ""

        if not text and not self._chunks:
            return False

        # Also load the structure document for additional context
        try:
            with open(config.RAG_STRUCTURE_DOC_PATH, "r", encoding="utf-8") as f:
                text += "\n\n" + f.read()
        except FileNotFoundError:
            pass

        # Load admin clarifications document for additional context (all profiles)
        try:
            from app.rag.admin_clarifications import clarification_store
            admin_doc = clarification_store.build_rag_document()  # profile_id=None → all profiles
            if admin_doc:
                text += "\n\n" + admin_doc
        except Exception:
            pass

        # Chunk the document
        self._chunks = chunk_markdown(
            text,
            chunk_size=config.RAG_CHUNK_SIZE,
            overlap=config.RAG_CHUNK_OVERLAP,
        )

        if not self._chunks:
            return False

        # Try to build FAISS index with embeddings
        try:
            self._build_faiss_index()
        except Exception:
            # Fallback to keyword search
            self._fallback = True

        self._ready = True
        return True

    def build_admin_index(self, admin_doc: str = None, profile_id: str = None) -> bool:
        """Rebuild the entire index including admin clarifications.

        This is called after new admin answers are added, so the RAG index
        reflects the latest accumulated knowledge.

        Args:
            admin_doc: Optional pre-built admin markdown document.
                       If None, builds from clarification_store.
            profile_id: If provided, only include clarifications for this profile.
                        If None, includes ALL profiles' clarifications.
        """
        if admin_doc is None:
            try:
                from app.rag.admin_clarifications import clarification_store
                admin_doc = clarification_store.build_rag_document(profile_id)
            except Exception:
                admin_doc = ""

        # Reload base documents
        text = ""
        try:
            with open(config.RAG_DOC_PATH, "r", encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            pass

        try:
            with open(config.RAG_STRUCTURE_DOC_PATH, "r", encoding="utf-8") as f:
                text += "\n\n" + f.read()
        except FileNotFoundError:
            pass

        # Append admin clarifications
        if admin_doc:
            text += "\n\n" + admin_doc

        if not text.strip():
            return False

        # Re-chunk everything
        self._chunks = chunk_markdown(
            text,
            chunk_size=config.RAG_CHUNK_SIZE,
            overlap=config.RAG_CHUNK_OVERLAP,
        )

        if not self._chunks:
            return False

        # Rebuild FAISS index
        try:
            self._build_faiss_index()
            self._fallback = False
        except Exception:
            self._fallback = True

        self._ready = True
        return True

    def _build_faiss_index(self):
        """Build FAISS index using sentence-transformers.

        Sets a timeout and uses HuggingFace mirror if configured.
        Falls back gracefully on any failure.
        """
        import numpy as np

        # Apply HuggingFace mirror if configured
        if self.HF_MIRROR:
            os.environ["HF_ENDPOINT"] = self.HF_MIRROR
            print(f"  [RAG] 使用 HuggingFace 镜像: {self.HF_MIRROR}")

        # Set a timeout to avoid hanging indefinitely on slow/unreachable HF
        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(self.BUILD_TIMEOUT)
        try:
            from sentence_transformers import SentenceTransformer
            import faiss

            model_name = "paraphrase-multilingual-MiniLM-L12-v2"
            self._model = SentenceTransformer(model_name)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

        # Encode all chunks
        embeddings = self._model.encode(
            self._chunks,
            show_progress_bar=False,
            normalize_embeddings=True,
        )

        # Build FAISS index
        dimension = embeddings.shape[1]
        self._index = faiss.IndexFlatIP(dimension)  # Inner product (cosine with normalized vectors)
        self._index.add(np.array(embeddings, dtype=np.float32))

    def search(self, query: str, top_k: int = None) -> list:
        """
        Search for relevant document chunks.

        Returns list of (chunk_text, score) tuples.
        """
        if not self._ready or not self._chunks:
            return []

        if top_k is None:
            top_k = min(config.RAG_TOP_K, len(self._chunks))

        if self._fallback or self._index is None:
            return self._keyword_search(query, top_k)

        try:
            import numpy as np
            query_embedding = self._model.encode(
                [query],
                normalize_embeddings=True,
            )
            scores, indices = self._index.search(np.array(query_embedding, dtype=np.float32), top_k)

            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx >= 0 and idx < len(self._chunks):
                    results.append((self._chunks[idx], float(score)))
            return results
        except Exception:
            return self._keyword_search(query, top_k)

    def _keyword_search(self, query: str, top_k: int) -> list:
        """Simple keyword-based search fallback using jieba for Chinese."""
        try:
            import jieba
            query_tokens = set(jieba.cut(query))
        except ImportError:
            query_tokens = set(query)

        scored = []
        for i, chunk in enumerate(self._chunks):
            score = 0
            chunk_lower = chunk.lower()
            for token in query_tokens:
                if token.lower() in chunk_lower:
                    score += 1
            # Bonus for exact phrase match
            if query in chunk:
                score += 5
            if score > 0:
                scored.append((i, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [(self._chunks[i], float(s)) for i, s in scored[:top_k]]


# Singleton
rag_indexer = RAGIndexer()
