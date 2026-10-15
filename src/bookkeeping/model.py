"""Ark Chat Completions adapter for LangChain's OpenAI-compatible transport."""

from typing import Any

import httpx
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from .config import Settings


class ArkChatModel(ChatOpenAI):
    """Keep Ark's reasoning field across tool turns without exposing it in cards."""

    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        body = response if isinstance(response, dict) else response.model_dump()
        for generation, choice in zip(result.generations, body["choices"], strict=True):
            reasoning = choice["message"].get("reasoning_content")
            if reasoning is not None:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _get_request_payload(self, input_, *, stop=None, **kwargs: Any) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        messages = self._convert_input(input_).to_messages()
        for message, encoded in zip(messages, payload["messages"], strict=True):
            if isinstance(message, AIMessage) and "reasoning_content" in message.additional_kwargs:
                encoded["reasoning_content"] = message.additional_kwargs["reasoning_content"]
        return payload


def create_model(settings: Settings, http_client: httpx.AsyncClient) -> ArkChatModel:
    return ArkChatModel(
        model=settings.ark_model,
        api_key=settings.ark_api_key,
        base_url=settings.ark_base_url,
        http_async_client=http_client,
        http_socket_options=(),
        max_retries=0,
        timeout=settings.model_timeout_seconds,
        use_responses_api=False,
        streaming=False,
    )
