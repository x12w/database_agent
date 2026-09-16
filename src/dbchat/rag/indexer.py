"""RAG 索引 —— 向量检索为主，关键词检索兜底。

三件事和历史上不一样，都是为了"能在事件循环里安全调用"：

1. **状态是原子快照**。历史实现把 `_ready/_chunks/_index/_model` 作为四个可变
   字段，启动后台线程写、`search()` 读，中间没有任何同步；读到 `_ready=True`
   但 `_chunks` 还是空列表的窗口是真实存在的。现在只有一个 `_state` 引用，
   写完整体替换（读者拿到的要么是旧快照要么是新快照，不会看到半成品）。
2. **`search()` / `build()` 有异步孪生** `asearch()` / `abuild()`，把 CPU 密集的
   编码与建索引丢到线程池；同步版本保留，供脚本与测试直接调用。
3. **索引落盘在 `Settings.rag_dir`**，语料未变时直接加载，省掉一次全量编码。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import anyio

from dbchat.config import Settings, get_settings
from dbchat.rag.chunker import chunk_markdown

logger = logging.getLogger(__name__)

#: 中文友好的多语言句向量模型。换模型会改变 `_fingerprint`，索引自动失效。
EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"

#: 项目根目录下的默认语料（历史 `RAG_DOC_PATH` / `RAG_STRUCTURE_DOC_PATH`）。
DEFAULT_DOC_FILENAMES = ("training_exam_db_rag.md", "training_exam_db_structure.md")

#: 模型下载/编码的墙钟上限（秒），可用 `RAG_BUILD_TIMEOUT` 覆盖。
DEFAULT_BUILD_TIMEOUT_S = 120

INDEX_FILENAME = "index.faiss"
INDEX_META_FILENAME = "index_meta.json"

#: 管理员问答语料提供者：profile_id → markdown（None 表示全部 profile）。
ClarificationsProvider = Callable[[str | None], str]


class _BuildTimeout(Exception):
    """模型下载/编码超时。"""


def _raise_build_timeout(signum: int, frame: Any) -> None:
    raise _BuildTimeout("模型下载或编码超时")


@dataclass(frozen=True, slots=True)
class IndexState:
    """一次构建的完整不可变快照。"""

    chunks: tuple[str, ...] = ()
    index: Any | None = None
    model: Any | None = None
    fallback: bool = False
    ready: bool = False
    fingerprint: str = ""
    built_at: float = 0.0

    @property
    def size(self) -> int:
        return len(self.chunks)


class RAGIndexer:
    """文档检索索引。

    Args:
        settings: 配置来源，决定 `rag_dir`、切块参数与 `top_k`。
        doc_paths: 基础文档语料路径；不传则取项目根目录下的默认文档
            （或 `Settings.rag_doc_path` / `RAG_DOC_PATH` 的覆盖值）。
        clarifications_provider: 管理员问答语料的提供者。默认走
            `dbchat.storage.clarifications`，注入后即可脱离数据库测试。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        doc_paths: Sequence[str | Path] | None = None,
        clarifications_provider: ClarificationsProvider | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._doc_paths = (
            tuple(Path(p) for p in doc_paths)
            if doc_paths is not None
            else default_doc_paths(self._settings)
        )
        self._clarifications_provider = clarifications_provider
        self._state = IndexState()
        # 写者互斥；读者不取锁，只读一次引用。
        self._lock = threading.RLock()

    # ---- 只读视图 ----

    @property
    def ready(self) -> bool:
        return self._state.ready

    @property
    def state(self) -> IndexState:
        """当前快照（诊断用）。"""
        return self._state

    @property
    def doc_paths(self) -> tuple[Path, ...]:
        return self._doc_paths

    # ---- 构建 ----

    def build(
        self,
        doc_paths: Sequence[str | Path] | None = None,
        *,
        admin_doc: str | None = None,
        profile_id: str | None = None,
        force: bool = False,
    ) -> bool:
        """构建（或复用）索引。

        Args:
            doc_paths: 覆盖构造时的文档语料。
            admin_doc: 已拼好的管理员问答 markdown；None 表示按
                `profile_id` 现取。传空字符串表示"这次不要管理员语料"。
            profile_id: 只纳入该 profile 的管理员问答；None 表示全部。
            force: 忽略磁盘缓存，强制重新编码。

        Returns:
            索引可用返回 True；语料为空且当前也没有可用索引时返回 False。
        """
        text = self._compose_text(doc_paths, admin_doc=admin_doc, profile_id=profile_id)
        if not text.strip():
            logger.warning("RAG 语料为空（文档路径: %s），沿用现有索引", self._doc_paths)
            return self._state.ready

        fingerprint = self._fingerprint(text)

        if not force:
            cached = self._load_state(fingerprint)
            if cached is not None:
                self._swap(cached)
                logger.info("RAG 索引命中磁盘缓存: %d 个片段", cached.size)
                return True

        chunks = tuple(
            chunk_markdown(
                text,
                chunk_size=self._settings.rag_chunk_size,
                overlap=self._settings.rag_chunk_overlap,
            )
        )
        if not chunks:
            logger.warning("RAG 切块结果为空，索引未更新")
            return False

        state = self._encode(chunks, fingerprint)
        self._swap(state)
        if state.ready and not state.fallback:
            self._save_state(state)
        logger.info(
            "RAG 索引构建完成: %d 个片段, 向量检索=%s", state.size, not state.fallback
        )
        return state.ready

    def build_admin_index(
        self,
        admin_doc: str | None = None,
        profile_id: str | None = None,
        *,
        force: bool = True,
    ) -> bool:
        """纳入管理员问答后重建索引。

        管理员刚给出新答案时必须真正重建（`force=True`），否则指纹未变
        （问答内容变了但文档没变时指纹会变，这里只是防止调用方误用缓存）。
        """
        return self.build(admin_doc=admin_doc, profile_id=profile_id, force=force)

    async def abuild(
        self,
        doc_paths: Sequence[str | Path] | None = None,
        *,
        admin_doc: str | None = None,
        profile_id: str | None = None,
        force: bool = False,
    ) -> bool:
        """`build` 的异步版本 —— 编码与建索引在线程池里跑。"""
        return await anyio.to_thread.run_sync(
            partial(
                self.build,
                doc_paths,
                admin_doc=admin_doc,
                profile_id=profile_id,
                force=force,
            )
        )

    async def abuild_admin_index(
        self,
        admin_doc: str | None = None,
        profile_id: str | None = None,
        *,
        force: bool = True,
    ) -> bool:
        """`build_admin_index` 的异步版本。"""
        return await anyio.to_thread.run_sync(
            partial(self.build_admin_index, admin_doc, profile_id, force=force)
        )

    # ---- 检索 ----

    def search(self, query: str, top_k: int | None = None) -> list[tuple[str, float]]:
        """检索最相关的片段，返回 `[(片段, 分数), ...]`。

        分数口径：向量检索是归一化内积（余弦相似度）；关键词检索是词命中数
        （精确短语额外 +5）。两种口径不可直接比较 —— 历史实现也是如此。
        """
        state = self._state
        if not state.ready or not state.chunks:
            return []

        limit = top_k if top_k is not None else self._settings.rag_top_k
        limit = max(1, min(limit, len(state.chunks)))

        if state.fallback or state.index is None or state.model is None:
            return _keyword_search(state.chunks, query, limit)

        try:
            import numpy as np

            embedding = state.model.encode([query], normalize_embeddings=True)
            scores, indices = state.index.search(
                np.array(embedding, dtype=np.float32), limit
            )
            hits: list[tuple[str, float]] = []
            for score, idx in zip(scores[0], indices[0], strict=False):
                if 0 <= idx < len(state.chunks):
                    hits.append((state.chunks[idx], float(score)))
            return hits
        except Exception as exc:
            # 查询侧失败不该让整个提问失败：退回关键词检索并留下日志。
            logger.warning("向量检索失败，退回关键词检索: %s", exc, exc_info=True)
            return _keyword_search(state.chunks, query, limit)

    async def asearch(self, query: str, top_k: int | None = None) -> list[tuple[str, float]]:
        """`search` 的异步版本 —— 编码 query 是 CPU 活，不占事件循环。"""
        return await anyio.to_thread.run_sync(partial(self.search, query, top_k))

    # ---- 语料 ----

    def _compose_text(
        self,
        doc_paths: Sequence[str | Path] | None,
        *,
        admin_doc: str | None,
        profile_id: str | None,
    ) -> str:
        parts: list[str] = []
        for path in doc_paths if doc_paths is not None else self._doc_paths:
            text = _read_text(Path(path))
            if text:
                parts.append(text)

        if admin_doc is None:
            admin_doc = self._clarifications_text(profile_id)
        if admin_doc and admin_doc.strip():
            parts.append(admin_doc)

        return "\n\n".join(part for part in parts if part.strip())

    def _clarifications_text(self, profile_id: str | None) -> str:
        provider = self._clarifications_provider or _default_clarifications_provider
        try:
            return provider(profile_id) or ""
        except Exception as exc:
            # 管理员语料拿不到只是少了一份上下文，不该中断建索引；但要留痕。
            logger.warning("读取管理员问答语料失败: %s", exc, exc_info=True)
            return ""

    def _fingerprint(self, text: str) -> str:
        """语料 + 切块参数 + 模型名的指纹，用于判断磁盘缓存是否还能用。"""
        digest = hashlib.sha256()
        digest.update(EMBEDDING_MODEL.encode("utf-8"))
        digest.update(f"|{self._settings.rag_chunk_size}|{self._settings.rag_chunk_overlap}|".encode())
        digest.update(text.encode("utf-8"))
        return digest.hexdigest()

    # ---- 编码 ----

    def _encode(self, chunks: tuple[str, ...], fingerprint: str) -> IndexState:
        """编码所有片段并建 FAISS 索引；任何失败都退回关键词检索。"""
        try:
            model = self._load_model()
            index = self._build_faiss_index(model, chunks)
        except Exception as exc:
            logger.warning(
                "向量索引不可用（%s: %s），退回关键词检索",
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return IndexState(
                chunks=chunks,
                fallback=True,
                ready=True,
                fingerprint=fingerprint,
                built_at=time.time(),
            )
        return IndexState(
            chunks=chunks,
            index=index,
            model=model,
            fallback=False,
            ready=True,
            fingerprint=fingerprint,
            built_at=time.time(),
        )

    def _load_model(self) -> Any:
        """加载句向量模型（首次会下载权重，用镜像与超时兜住）。"""
        mirror = os.getenv("HF_ENDPOINT", "")
        if mirror:
            os.environ["HF_ENDPOINT"] = mirror
            logger.info("使用 HuggingFace 镜像: %s", mirror)

        with _wall_clock_timeout(self._build_timeout_s()):
            from sentence_transformers import SentenceTransformer

            return SentenceTransformer(EMBEDDING_MODEL)

    def _build_faiss_index(self, model: Any, chunks: tuple[str, ...]) -> Any:
        import faiss
        import numpy as np

        embeddings = model.encode(
            list(chunks),
            show_progress_bar=False,
            normalize_embeddings=True,
        )
        index = faiss.IndexFlatIP(embeddings.shape[1])  # 归一化后内积 = 余弦
        index.add(np.array(embeddings, dtype=np.float32))
        return index

    def _build_timeout_s(self) -> int:
        override = getattr(self._settings, "rag_build_timeout_s", None)
        if override:
            return int(override)
        return int(os.getenv("RAG_BUILD_TIMEOUT", str(DEFAULT_BUILD_TIMEOUT_S)))

    # ---- 磁盘缓存 ----

    def _index_paths(self) -> tuple[Path, Path]:
        rag_dir = self._settings.rag_dir
        return rag_dir / INDEX_FILENAME, rag_dir / INDEX_META_FILENAME

    def _save_state(self, state: IndexState) -> None:
        """落盘。写失败只记日志 —— 缓存是优化，不是正确性的一部分。"""
        index_path, meta_path = self._index_paths()
        try:
            import faiss

            index_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_index = index_path.with_suffix(".faiss.tmp")
            faiss.write_index(state.index, str(tmp_index))
            tmp_index.replace(index_path)

            payload = {
                "fingerprint": state.fingerprint,
                "model": EMBEDDING_MODEL,
                "built_at": state.built_at,
                "chunks": list(state.chunks),
            }
            tmp_meta = meta_path.with_suffix(".json.tmp")
            tmp_meta.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            tmp_meta.replace(meta_path)
        except Exception as exc:
            logger.warning("RAG 索引落盘失败: %s", exc, exc_info=True)

    def _load_state(self, fingerprint: str) -> IndexState | None:
        """尝试从磁盘恢复索引；任何不一致都返回 None（重新构建）。"""
        index_path, meta_path = self._index_paths()
        if not index_path.exists() or not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("RAG 索引元数据不可读，将重建: %s", exc)
            return None

        if meta.get("fingerprint") != fingerprint or meta.get("model") != EMBEDDING_MODEL:
            return None

        chunks = tuple(meta.get("chunks") or ())
        if not chunks:
            return None

        try:
            import faiss

            index = faiss.read_index(str(index_path))
        except Exception as exc:
            logger.warning("RAG 索引文件不可读，将重建: %s", exc, exc_info=True)
            return None

        # 查询侧仍需模型把 query 编码；模型不可用就退回关键词检索，
        # 但片段和索引照用 —— 不必再编码一遍。
        model: Any | None = None
        fallback = False
        try:
            model = self._load_model()
        except Exception as exc:
            logger.warning("句向量模型不可用，退回关键词检索: %s", exc, exc_info=True)
            fallback = True

        return IndexState(
            chunks=chunks,
            index=index,
            model=model,
            fallback=fallback,
            ready=True,
            fingerprint=fingerprint,
            built_at=float(meta.get("built_at") or 0.0),
        )

    def _swap(self, state: IndexState) -> None:
        """原子替换状态快照。"""
        with self._lock:
            self._state = state


def _read_text(path: Path) -> str:
    """读语料文件；缺失是正常情况（只建一部分语料），记一条日志即可。"""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("RAG 语料文件不存在: %s", path)
    except OSError as exc:
        logger.warning("RAG 语料文件读取失败 %s: %s", path, exc)
    return ""


def _keyword_search(
    chunks: tuple[str, ...], query: str, top_k: int
) -> list[tuple[str, float]]:
    """jieba 分词后的关键词命中检索（无向量模型时的兜底）。"""
    try:
        import jieba

        tokens = set(jieba.cut(query))
    except ImportError:
        logger.warning("jieba 不可用，改用字符级匹配")
        tokens = set(query)

    scored: list[tuple[int, int]] = []
    for i, chunk in enumerate(chunks):
        lowered = chunk.lower()
        score = sum(1 for token in tokens if token and token.lower() in lowered)
        if query in chunk:
            score += 5  # 整句命中加权
        if score > 0:
            scored.append((i, score))

    scored.sort(key=lambda item: item[1], reverse=True)
    return [(chunks[i], float(score)) for i, score in scored[:top_k]]


@contextmanager
def _wall_clock_timeout(seconds: int) -> Iterator[None]:
    """给模型下载套一个墙钟上限。

    `SIGALRM` 只能装在主线程；在工作线程（`anyio.to_thread` 里）会
    `ValueError`。此时静默降级为不限时，由调用方的整体超时兜底 ——
    这条路径在历史上会直接抛异常，把整个建索引流程打挂。
    """
    if seconds <= 0 or threading.current_thread() is not threading.main_thread():
        yield
        return

    try:
        previous = signal.signal(signal.SIGALRM, _raise_build_timeout)
    except ValueError:
        logger.debug("当前线程不支持 SIGALRM，跳过模型加载超时保护")
        yield
        return

    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def default_doc_paths(settings: Settings | None = None) -> tuple[Path, ...]:
    """基础文档语料路径。

    优先 `Settings.rag_doc_path` / `rag_structure_doc_path`（配置层后续补字段
    即可生效），其次同名环境变量，最后回落到项目根目录下的默认文档名。
    """
    s = settings or get_settings()
    rag_doc = getattr(s, "rag_doc_path", None) or os.getenv("RAG_DOC_PATH")
    structure_doc = getattr(s, "rag_structure_doc_path", None) or os.getenv(
        "RAG_STRUCTURE_DOC_PATH"
    )
    names = (rag_doc, structure_doc)
    if any(names):
        return tuple(Path(n) for n in names if n)
    return tuple(s.project_root / name for name in DEFAULT_DOC_FILENAMES)


def _default_clarifications_provider(profile_id: str | None) -> str:
    """默认从 SQLite 里的管理员问答生成 markdown 语料。

    延迟导入：`rag` 只在这一刻才需要 `storage`，也让注入式测试不碰数据库。
    """
    from dbchat.storage.clarifications import get_clarification_repository

    return get_clarification_repository().build_rag_document(profile_id)


_indexer: RAGIndexer | None = None
_indexer_lock = threading.Lock()


def get_indexer(settings: Settings | None = None) -> RAGIndexer:
    """进程内共享的索引器。"""
    global _indexer
    if settings is not None:
        return RAGIndexer(settings)
    with _indexer_lock:
        if _indexer is None:
            _indexer = RAGIndexer()
        return _indexer


__all__ = [
    "DEFAULT_BUILD_TIMEOUT_S",
    "DEFAULT_DOC_FILENAMES",
    "EMBEDDING_MODEL",
    "INDEX_FILENAME",
    "INDEX_META_FILENAME",
    "IndexState",
    "RAGIndexer",
    "default_doc_paths",
    "get_indexer",
]
