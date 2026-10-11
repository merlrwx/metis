import logging
import os
from dataclasses import dataclass
from typing import Protocol

from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)


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


class AnswerFormatError(ChatProviderError):
    """The provider responded, but its answer could not be validated."""


class LangChainOpenAICompatibleProvider:
    requires_structured_answers = True

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
            max_tokens=4096,
        )

    def generate(self, messages: list[tuple[str, str]]) -> ChatCompletion:
        return self._generate(messages)

    def generate_structured(self, messages: list[tuple[str, str]]) -> ChatCompletion:
        """Prefer provider native JSON schema, with JSON mode/plain invoke fallback."""
        try:
            structured = self.client.with_structured_output(
                schema={
                    "title": "GroundedAnswer",
                    "type": "object",
                    "properties": {
                        "outcome": {
                            "type": "string",
                            "enum": [
                                "answered",
                                "partially_answered",
                                "clarification_needed",
                                "insufficient_evidence",
                            ],
                        },
                        "answer": {"type": "string"},
                        "calculation": {"type": ["object", "null"]},
                    },
                    "required": ["outcome", "answer"],
                    "additionalProperties": False,
                },
                method="json_schema",
            )
            response = structured.invoke(messages)
            if isinstance(response, dict):
                import json

                return self._completion(json.dumps(response))
        except Exception as error:  # noqa: BLE001 - server capability varies by endpoint
            # Some local OpenAI-compatible servers do not implement json_schema.
            logger.debug("Provider JSON schema mode unavailable: %s", error)
        try:
            response = self.client.bind(response_format={"type": "json_object"}).invoke(
                messages
            )
            if isinstance(response.content, str):
                return self._completion(response.content, response)
        except Exception as error:  # noqa: BLE001 - server capability varies by endpoint
            logger.debug("Provider JSON object mode unavailable: %s", error)
        return self._generate(messages)

    def _generate(self, messages: list[tuple[str, str]]) -> ChatCompletion:
        try:
            response = self.client.invoke(messages)
        except Exception as error:
            raise ChatProviderError("Chat provider request failed") from error

        content = response.content
        if not isinstance(content, str):
            raise ChatProviderError("Chat provider returned non-text content")
        return self._completion(content, response)

    def _completion(self, content: str, response=None) -> ChatCompletion:
        usage = (getattr(response, "usage_metadata", None) or {}) if response else {}
        if not isinstance(usage, dict):
            usage = {}
        response_usage = (
            getattr(response, "response_metadata", {}).get("token_usage", {})
            if response
            else {}
        )
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
        if not 0 < timeout <= 120 or not 0 <= retries <= 1:
            raise ValueError(
                "Timeout must be within 120 seconds and retries at most one"
            )
        return LangChainOpenAICompatibleProvider(
            base_url=os.environ.get("GPTMOCK_BASE_URL", "http://127.0.0.1:8001/v1"),
            api_key=os.environ.get("GPTMOCK_API_KEY", "chatmock"),
            model=os.environ.get("GPTMOCK_MODEL", "gpt-5.6-luna"),
            timeout=timeout,
            max_retries=retries,
        )
    except Exception as error:
        raise ChatProviderError("Chat provider configuration is invalid") from error
