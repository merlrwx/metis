import os
from dataclasses import dataclass
from typing import Protocol

from langchain_openai import ChatOpenAI


@dataclass(frozen=True)
class ChatCompletion:
    content: str
    model_id: str
    input_tokens: int | None
    output_tokens: int | None


class ChatProvider(Protocol):
    model_id: str

    def generate(self, messages: list[tuple[str, str]]) -> ChatCompletion: ...


class ChatProviderError(RuntimeError):
    pass


class LangChainOpenAICompatibleProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120,
        max_retries: int = 1,
    ):
        self.model_id = f"openai-compatible:{model}"
        self.client = ChatOpenAI(
            base_url=base_url,
            api_key=api_key,
            model=model,
            use_responses_api=False,
            timeout=timeout,
            max_retries=max_retries,
        )

    def generate(self, messages: list[tuple[str, str]]) -> ChatCompletion:
        try:
            response = self.client.invoke(messages)
        except Exception as error:
            raise ChatProviderError("Chat provider request failed") from error

        content = response.content
        if not isinstance(content, str):
            raise ChatProviderError("Chat provider returned non-text content")
        usage = response.usage_metadata or {}
        if not isinstance(usage, dict):
            usage = {}
        response_usage = response.response_metadata.get("token_usage", {})
        if not isinstance(response_usage, dict):
            response_usage = {}

        return ChatCompletion(
            content=content,
            model_id=self.model_id,
            input_tokens=_token_count(
                usage.get("input_tokens", response_usage.get("prompt_tokens"))
            ),
            output_tokens=_token_count(
                usage.get("output_tokens", response_usage.get("completion_tokens"))
            ),
        )


def _token_count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def get_chat_provider() -> ChatProvider:
    try:
        timeout = float(os.environ.get("GPTMOCK_TIMEOUT", "120"))
        retries = int(os.environ.get("GPTMOCK_MAX_RETRIES", "1"))
        if timeout <= 0 or retries < 0:
            raise ValueError("Timeout must be positive and retries nonnegative")
        return LangChainOpenAICompatibleProvider(
            base_url=os.environ.get("GPTMOCK_BASE_URL", "http://127.0.0.1:8000/v1"),
            api_key=os.environ.get("GPTMOCK_API_KEY", "chatmock"),
            model=os.environ.get("GPTMOCK_MODEL", "gpt-6-luna"),
            timeout=timeout,
            max_retries=retries,
        )
    except Exception as error:
        raise ChatProviderError("Chat provider configuration is invalid") from error
