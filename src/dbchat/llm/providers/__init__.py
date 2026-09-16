"""provider 实现 —— 每个模块把一家的线协议翻译成统一事件。

新增一个 provider：写一个模块，暴露
`stream(client, *, model, system, messages, tools, max_tokens, temperature, cancel)`
与 `complete(client, *, model, system, messages, max_tokens, temperature)`，
然后在 `llm/client.py` 的分发处加一行。
"""

from dbchat.llm.providers import anthropic, openai

__all__ = ["anthropic", "openai"]
