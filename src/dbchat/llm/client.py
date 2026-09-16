"""跨 provider 的 LLM 客户端。

对外只有两个动作：

* `stream(...)`   —— 流式，产出 `llm.events` 里的统一事件；
* `complete(...)` —— 非流式，直接拿到一段文本。

两者都是协程，都跑在事件循环上。`complete` 的存在是为了修掉一个真实缺陷：
历史代码在 `orchestrator._evaluate_results` / `_plan_computation` 里直接调用
同步 `.create()`，把事件循环线程堵在网络上；`router.classify` 更严重 ——
它用 `asyncio.get_event_loop().run_until_complete()` 在已运行的循环里跑协程，
必然抛 `RuntimeError`，又被 `except Exception: pass` 吞掉，于是"LLM 分类"
这条分支从未真正执行过。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import anyio

from dbchat.config import Settings, get_settings
from dbchat.domain.errors import LlmError, LlmUnavailableError
from dbchat.llm.events import CancelToken, LLMEvent
from dbchat.llm.providers import anthropic as anthropic_provider
from dbchat.llm.providers import openai as openai_provider

logger = logging.getLogger(__name__)

#: `Settings.llm_api_base` 的默认值属于 OpenAI；provider 是 anthropic 时
#: 不能把默认值当网关地址传下去。
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

#: 意图分类的三种结果。
CLASS_DATA_QUESTION = "DATA_QUESTION"
CLASS_DOC_QUESTION = "DOC_QUESTION"
CLASS_AMBIGUOUS = "AMBIGUOUS"

#: 分类只要求一个词，给足 20 个 token 已经非常宽松。
CLASSIFY_MAX_TOKENS = 20

_CLASSIFY_SYSTEM = "You are a query classifier. Respond with exactly one word."
_CLASSIFY_PROMPT = (
    "Given a user question about a database, classify it as:\n"
    "DATA_QUESTION - asks about actual data records, people, counts, values, statistics.\n"
    "DOC_QUESTION - asks about database structure, table schemas, field meanings.\n"
    "AMBIGUOUS - borderline.\n\n"
    "When uncertain, respond DATA_QUESTION.\n\n"
    "Question: {question}\n\n"
    "Respond with exactly one word:"
)


@dataclass(slots=True, frozen=True)
class LLMUsage:
    """一次调用的元信息（仅用于日志/统计）。"""

    provider: str
    model: str
    max_tokens: int


class LLMClient:
    """一个客户端，两种 provider。

    provider 与可用性在**构造时**按 `Settings` 定死，不做惰性冻结 ——
    历史实现第一次访问 `provider` 属性时才写 `self._provider`，多线程下
    两个请求可能同时写，且一旦写错就再也纠正不回来。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._provider = self._settings.effective_provider
        self._available = self._settings.llm_available
        self._client: Any | None = None
        #  SDK 导入与建连只做一次；用线程锁保护，因为构建发生在工作线程里。
        self._build_lock = threading.Lock()

    # ---- 基本信息 ----

    @property
    def provider(self) -> str:
        """`"anthropic"` 或 `"openai"`。"""
        return self._provider

    @property
    def available(self) -> bool:
        """是否配置了 API key。

        只反映配置，不反映网络 —— 真正建连失败会在 `stream`/`complete` 里
        变成带有上下文的 `LlmError`，而不是被悄悄吞掉。
        """
        return self._available

    @property
    def model(self) -> str:
        return self._settings.llm_model

    def usage(self, max_tokens: int) -> LLMUsage:
        """本次调用的元信息。"""
        return LLMUsage(provider=self._provider, model=self.model, max_tokens=max_tokens)

    # ---- 客户端构建（惰性，只在需要时导入 SDK）----

    def _build_client(self) -> Any:
        """同步构建 SDK 客户端。**在工作线程中调用** —— SDK 导入不便宜。"""
        with self._build_lock:
            if self._client is not None:
                return self._client
            api_key = self._settings.effective_api_key
            if not api_key:
                raise LlmUnavailableError("缺少 LLM_API_KEY / ANTHROPIC_API_KEY")

            max_retries = self._settings.llm_max_retries
            try:
                if self._provider == "anthropic":
                    import anthropic

                    kwargs: dict[str, Any] = {
                        "api_key": api_key,
                        "timeout": _http_timeout(anthropic, self._settings),
                        "max_retries": max_retries,
                    }
                    base_url = self._anthropic_base_url()
                    if base_url:
                        kwargs["base_url"] = base_url
                    self._client = anthropic.AsyncAnthropic(**kwargs)
                else:
                    import openai
                    from openai import AsyncOpenAI

                    self._client = AsyncOpenAI(
                        base_url=self._settings.llm_api_base,
                        api_key=api_key,
                        timeout=_http_timeout(openai, self._settings),
                        max_retries=max_retries,
                    )
            except ImportError as exc:
                raise LlmError(
                    f"未安装 {self._provider} SDK，无法调用模型服务",
                    detail=f"{type(exc).__name__}: {exc}",
                    cause=exc,
                ) from exc
            except LlmError:
                raise
            except Exception as exc:
                # 不吞异常：这里失败意味着整个 agent 不可用，必须让调用方看见。
                logger.error("初始化 %s 客户端失败: %s", self._provider, exc, exc_info=True)
                raise LlmError(
                    "模型客户端初始化失败",
                    detail=f"{type(exc).__name__}: {exc}",
                    cause=exc,
                ) from exc
            return self._client

    def _anthropic_base_url(self) -> str | None:
        """Anthropic 网关地址。配置项默认值是 OpenAI 的地址，不能直接用。"""
        base = (self._settings.llm_api_base or "").strip()
        if not base or base.rstrip("/") == DEFAULT_OPENAI_BASE_URL:
            return None
        return base

    async def _client_or_raise(self) -> Any:
        if not self._available:
            raise LlmUnavailableError("未配置 LLM_API_KEY / ANTHROPIC_API_KEY")
        if self._client is None:
            return await anyio.to_thread.run_sync(self._build_client)
        return self._client

    # ---- 调用 ----

    async def stream(
        self,
        system: str,
        messages: Sequence[Any],
        tools: Sequence[Any] | None = None,
        cancel: CancelToken | None = None,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """流式调用。

        Args:
            system: 系统提示词（provider 会各自安放，绝不混进 messages）。
            messages: 任意线格式或规范形态的历史，见 `llm.messages`。
            tools: Anthropic 形状的工具定义 dict 列表。
            cancel: 取消信号，只要实现 `is_set()`；触发后以
                `Done(STOP_CANCELLED)` 收尾。
            model / max_tokens: 覆盖 `Settings` 里的默认值。

        Yields:
            `ThinkingDelta` / `TextDelta` / `ToolUse` / `Done` / `ErrorEvent`。

        Raises:
            LlmUnavailableError: 未配置 API key —— 这种情况不该开始一次流。
            LlmError: SDK 不可用或客户端初始化失败。
        """
        client = await self._client_or_raise()
        resolved_model = model or self._settings.llm_model
        resolved_max_tokens = max_tokens or self._settings.llm_max_tokens
        provider_stream = (
            anthropic_provider.stream if self._provider == "anthropic" else openai_provider.stream
        )
        async for event in provider_stream(
            client,
            model=resolved_model,
            system=system,
            messages=messages,
            tools=tools or (),
            max_tokens=resolved_max_tokens,
            temperature=self._settings.llm_temperature,
            cancel=cancel,
        ):
            yield event

    async def complete(
        self,
        system: str,
        messages: Sequence[Any],
        *,
        max_tokens: int | None = None,
        model: str | None = None,
        temperature: float | None = None,
    ) -> str:
        """非流式调用，返回正文文本。

        Raises:
            LlmUnavailableError: 未配置 API key。
            LlmError: 调用失败（含 provider 返回的错误）。
        """
        client = await self._client_or_raise()
        resolved_model = model or self._settings.llm_model
        resolved_max_tokens = max_tokens or self._settings.llm_max_tokens
        resolved_temperature = (
            self._settings.llm_temperature if temperature is None else temperature
        )
        provider_complete = (
            anthropic_provider.complete
            if self._provider == "anthropic"
            else openai_provider.complete
        )
        try:
            return await provider_complete(
                client,
                model=resolved_model,
                system=system,
                messages=messages,
                max_tokens=resolved_max_tokens,
                temperature=resolved_temperature,
            )
        except LlmError:
            raise
        except Exception as exc:
            logger.warning("LLM 非流式调用失败: %s", exc, exc_info=True)
            raise LlmError(
                "模型调用失败", detail=f"{type(exc).__name__}: {exc}", cause=exc
            ) from exc

    # ---- 轻量判定 ----

    async def classify(self, question: str) -> str:
        """把问题粗分成数据类 / 文档类。

        这是**尽力而为**的启发式：只有模型确实答了才用它，任何失败都退回
        `AMBIGUOUS`，让调用方走关键词规则。失败会记日志（历史实现在这里
        吞掉了 RuntimeError，导致这条分支静默失效）。
        """
        if not self._available:
            return CLASS_AMBIGUOUS
        try:
            text = await self.complete(
                _CLASSIFY_SYSTEM,
                [{"role": "user", "content": _CLASSIFY_PROMPT.format(question=question)}],
                max_tokens=CLASSIFY_MAX_TOKENS,
                temperature=0.0,
            )
        except LlmError as exc:
            logger.warning("LLM 意图分类失败，回退关键词规则: %s", exc.detail or exc)
            return CLASS_AMBIGUOUS

        upper = text.strip().upper()
        if "DATA" in upper:
            return CLASS_DATA_QUESTION
        if "DOC" in upper:
            return CLASS_DOC_QUESTION
        return CLASS_AMBIGUOUS

    async def aclose(self) -> None:
        """释放底层 HTTP 连接池（应用关闭时调用）。"""
        if self._client is None:
            return
        try:
            await self._client.close()
        except Exception as exc:
            logger.warning("关闭 LLM 客户端失败: %s", exc, exc_info=True)
        finally:
            self._client = None


def _sdk_http_module(sdk_module: Any) -> Any | None:
    """找出 SDK 内部实际使用的 HTTP 客户端模块。

    这些 SDK 会把 HTTP 库整体改名打包（本环境的 anthropic/openai 用的是
    `httpx2`），并且会**显式拒绝**来自另一个包的同名对象。因此不能想当然
    `import httpx` —— 必须用 SDK 自己那份。
    """
    base_client = getattr(sdk_module, "_base_client", None)
    for name in ("httpx2", "httpx"):
        module = getattr(base_client, name, None)
        if module is not None:
            return module
    return None


def _http_timeout(sdk_module: Any, settings: Settings) -> Any:
    """由 Settings 构造超时（总时长 + 建连时长分开配）。

    取不到 SDK 的 HTTP 模块时退回一个裸秒数 —— 两个 SDK 都接受数字形态的
    timeout，比传一个来源不对的 Timeout 对象安全得多。
    """
    total = float(settings.llm_timeout_s)
    connect = float(settings.llm_connect_timeout_s)
    http_module = _sdk_http_module(sdk_module)
    if http_module is not None:
        try:
            return http_module.Timeout(total, connect=connect)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "无法为 %s 构造分项超时（%s），退回单一超时值",
                getattr(sdk_module, "__name__", sdk_module),
                exc,
            )
    return max(total, connect)


_cached_client: LLMClient | None = None
_cache_lock = threading.Lock()


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    """进程内共享的客户端。

    不传 settings 时使用全局单例配置；传了 settings（例如测试或
    多配置场景）则新建一个，不复用缓存。
    """
    global _cached_client
    if settings is not None:
        return LLMClient(settings)
    with _cache_lock:
        if _cached_client is None:
            _cached_client = LLMClient()
        return _cached_client


__all__ = [
    "CLASSIFY_MAX_TOKENS",
    "CLASS_AMBIGUOUS",
    "CLASS_DATA_QUESTION",
    "CLASS_DOC_QUESTION",
    "DEFAULT_OPENAI_BASE_URL",
    "LLMClient",
    "LLMUsage",
    "get_llm_client",
]
