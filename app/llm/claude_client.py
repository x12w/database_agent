"""LLM client — supports both Anthropic and OpenAI-compatible APIs.

Provides a unified streaming interface regardless of provider.
All events use the same format so the orchestrator doesn't care which provider is used.
"""

import json
from typing import AsyncGenerator, Optional

from app.config import config


class LLMClient:
    """Multi-provider LLM client with unified streaming interface."""

    def __init__(self):
        self._client = None
        self._available = None
        self._provider = None

    @property
    def provider(self) -> str:
        """Get the effective provider: 'anthropic' or 'openai'."""
        if self._provider is None:
            self._provider = config.effective_provider
        return self._provider

    @property
    def available(self) -> bool:
        """Check if the LLM is available (API key is set and client can init)."""
        if self._available is not None:
            return self._available

        api_key = config.effective_api_key
        if not api_key:
            self._available = False
            return False

        try:
            if self.provider == "anthropic":
                import anthropic
                self._client = anthropic.Anthropic(api_key=api_key)
            else:
                from openai import OpenAI
                import httpx
                self._client = OpenAI(
                    base_url=config.LLM_API_BASE,
                    api_key=api_key,
                    timeout=httpx.Timeout(120.0, connect=30.0),
                )
            self._available = True
        except Exception:
            self._available = False

        return self._available

    async def stream_chat(
        self,
        system_prompt: str,
        messages: list,
        tools: Optional[list] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncGenerator[dict, None]:
        """
        Stream a chat completion. Dispatches to provider-specific implementation.

        Yields unified events:
            {"type": "thinking_delta", "text": "..."}
            {"type": "text_delta", "text": "..."}
            {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
            {"type": "done", "stop_reason": "..."}
            {"type": "error", "message": "..."}
        """
        if not self.available:
            yield {"type": "error", "message": "LLM not available — check LLM_API_KEY in .env"}
            return

        if self.provider == "anthropic":
            async for event in self._stream_anthropic(system_prompt, messages, tools, model, max_tokens):
                yield event
        else:
            async for event in self._stream_openai(system_prompt, messages, tools, model, max_tokens):
                yield event

    # ---- Anthropic Implementation ----

    async def _stream_anthropic(
        self, system_prompt: str, messages: list, tools: list,
        model: str, max_tokens: int,
    ) -> AsyncGenerator[dict, None]:
        model = model or config.LLM_MODEL
        max_tokens = max_tokens or config.LLM_MAX_TOKENS
        api_messages = _format_anthropic_messages(messages)

        try:
            with self._client.messages.stream(
                model=model,
                system=system_prompt,
                messages=api_messages,
                tools=tools or [],
                max_tokens=max_tokens,
                temperature=config.LLM_TEMPERATURE,
            ) as stream:
                current_tool_id = None
                current_tool_name = None
                current_tool_input = ""

                for event in stream:
                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            current_tool_id = block.id
                            current_tool_name = block.name
                            current_tool_input = ""

                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "thinking_delta":
                            yield {"type": "thinking_delta", "text": delta.thinking}
                        elif delta.type == "text_delta":
                            yield {"type": "text_delta", "text": delta.text}
                        elif delta.type == "input_json_delta" and current_tool_id:
                            current_tool_input += delta.partial_json

                    elif event.type == "content_block_stop":
                        if current_tool_id:
                            try:
                                parsed_input = json.loads(current_tool_input)
                            except json.JSONDecodeError:
                                parsed_input = {"raw": current_tool_input}
                            yield {
                                "type": "tool_use",
                                "id": current_tool_id,
                                "name": current_tool_name,
                                "input": parsed_input,
                            }
                            current_tool_id = None
                            current_tool_name = None
                            current_tool_input = ""

                    elif event.type == "error":
                        yield {"type": "error", "message": str(event.error)}

                final = stream.get_final_message()
                stop_reason = None
                for block in final.content:
                    if hasattr(block, 'type') and block.type == 'tool_use':
                        stop_reason = "tool_use"

                yield {
                    "type": "done",
                    "stop_reason": stop_reason or final.stop_reason or "end_turn",
                }

        except Exception as e:
            yield {"type": "error", "message": str(e)}

    # ---- OpenAI Implementation ----

    async def _stream_openai(
        self, system_prompt: str, messages: list, tools: list,
        model: str, max_tokens: int,
    ) -> AsyncGenerator[dict, None]:
        model = model or config.LLM_MODEL
        max_tokens = max_tokens or config.LLM_MAX_TOKENS

        # Build OpenAI-format messages
        api_messages = _build_openai_messages(system_prompt, messages)

        # Convert tools from Anthropic format to OpenAI format if needed
        openai_tools = _convert_tools_to_openai(tools) if tools else None

        try:
            kwargs = {
                "model": model,
                "messages": api_messages,
                "max_tokens": max_tokens,
                "temperature": config.LLM_TEMPERATURE,
                "stream": True,
            }
            if openai_tools:
                kwargs["tools"] = openai_tools
                kwargs["tool_choice"] = "auto"
            # Add extra_body for reasoning effort if available
            if hasattr(self._client, '_extra_body'):
                kwargs["extra_body"] = self._client._extra_body or {}

            stream = self._client.chat.completions.create(**kwargs)

            tool_calls_acc = {}  # index -> {id, name, arguments_str}

            for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta is None:
                    continue

                # Text content
                if delta.content:
                    yield {"type": "text_delta", "text": delta.content}

                # Tool calls
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {
                                "id": tc.id or "",
                                "name": "",
                                "arguments_str": "",
                            }
                        acc = tool_calls_acc[idx]
                        if tc.id:
                            acc["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                acc["name"] = tc.function.name
                            if tc.function.arguments:
                                acc["arguments_str"] += tc.function.arguments

                # Check finish reason
                finish = chunk.choices[0].finish_reason if chunk.choices else None
                if finish == "tool_calls":
                    # Emit accumulated tool calls
                    for idx in sorted(tool_calls_acc.keys()):
                        acc = tool_calls_acc[idx]
                        try:
                            parsed = json.loads(acc["arguments_str"])
                        except json.JSONDecodeError:
                            parsed = {"raw": acc["arguments_str"]}
                        yield {
                            "type": "tool_use",
                            "id": acc["id"],
                            "name": acc["name"],
                            "input": parsed,
                        }
                    yield {"type": "done", "stop_reason": "tool_calls"}
                    return

                elif finish == "stop":
                    yield {"type": "done", "stop_reason": "end_turn"}
                    return

                elif finish == "length":
                    yield {"type": "done", "stop_reason": "max_tokens"}
                    return

            # Stream ended without explicit finish
            yield {"type": "done", "stop_reason": "end_turn"}

        except Exception as e:
            yield {"type": "error", "message": str(e)}

    # ---- Classification ----

    async def classify_quick(self, question: str) -> str:
        """
        Quick classification call (non-streaming).
        Returns: "DATA_QUESTION", "DOC_QUESTION", or "AMBIGUOUS"
        """
        if not self.available:
            return "AMBIGUOUS"

        classify_prompt = (
            "Given a user question about a MySQL database, classify it as:\n"
            "DATA_QUESTION - asks about actual data records, people, counts, values, statistics.\n"
            "DOC_QUESTION - asks about database structure, table schemas, field meanings.\n"
            "AMBIGUOUS - borderline.\n\n"
            "When uncertain, respond DATA_QUESTION.\n\n"
            f"Question: {question}\n\n"
            "Respond with exactly one word:"
        )

        try:
            if self.provider == "anthropic":
                text = self._client.messages.create(
                    model=config.LLM_MODEL,
                    max_tokens=20,
                    temperature=0,
                    system="You are a query classifier. Respond with exactly one word.",
                    messages=[{"role": "user", "content": classify_prompt}],
                ).content[0].text.strip().upper()
            else:
                resp = self._client.chat.completions.create(
                    model=config.LLM_MODEL,
                    max_tokens=20,
                    temperature=0,
                    messages=[
                        {"role": "system", "content": "You are a query classifier. Respond with exactly one word."},
                        {"role": "user", "content": classify_prompt},
                    ],
                )
                text = resp.choices[0].message.content.strip().upper()

            if "DATA" in text:
                return "DATA_QUESTION"
            elif "DOC" in text:
                return "DOC_QUESTION"
            else:
                return "AMBIGUOUS"
        except Exception:
            return "AMBIGUOUS"


# ---- Helper Functions ----

def _format_anthropic_messages(messages: list) -> list:
    """Format messages for Anthropic API."""
    formatted = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, str):
            formatted.append({"role": role, "content": content})
        elif isinstance(content, list):
            formatted.append({"role": role, "content": content})
        else:
            formatted.append({"role": role, "content": str(content)})
    return formatted


def _build_openai_messages(system_prompt: str, messages: list) -> list:
    """Build OpenAI-format messages from conversation history."""
    api_messages = []
    if system_prompt:
        api_messages.append({"role": "system", "content": system_prompt})

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        tool_calls = msg.get("tool_calls")

        # Handle tool role messages (responses to tool calls)
        if role == "tool":
            api_messages.append({
                "role": "tool",
                "tool_call_id": msg.get("tool_call_id", ""),
                "content": content if isinstance(content, str) else str(content),
            })
            continue

        # Handle assistant messages that already have tool_calls (OpenAI native format)
        if role == "assistant" and tool_calls:
            api_messages.append({
                "role": "assistant",
                "content": content if (isinstance(content, str) and content) else None,
                "tool_calls": tool_calls,
            })
            continue

        # Handle assistant messages with Anthropic-format content blocks
        if role == "assistant" and isinstance(content, list):
            converted_tool_calls = []
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    converted_tool_calls.append({
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                        },
                    })
                elif isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    text_parts.append(block)
            if converted_tool_calls:
                api_messages.append({
                    "role": "assistant",
                    "content": " ".join(text_parts) if text_parts else None,
                    "tool_calls": converted_tool_calls,
                })
            else:
                api_messages.append({"role": "assistant", "content": " ".join(text_parts)})
            continue

        # Handle user messages with Anthropic-format tool_result blocks
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text_parts.append(block.get("content", ""))
                elif isinstance(block, str):
                    text_parts.append(block)
            api_messages.append({"role": role, "content": " ".join(text_parts)})
            continue

        # Plain text messages
        if isinstance(content, str):
            api_messages.append({"role": role, "content": content})
        else:
            api_messages.append({"role": role, "content": str(content)})

    return api_messages


def _convert_tools_to_openai(tools: list) -> list:
    """Convert tools from Anthropic format to OpenAI format."""
    openai_tools = []
    for tool in tools:
        if "type" in tool and tool["type"] == "function":
            # Already OpenAI format
            openai_tools.append(tool)
        else:
            # Convert from Anthropic format
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            })
    return openai_tools


# Singleton
llm_client = LLMClient()
