from types import SimpleNamespace

from backend import chat


def test_chat_provider_uses_gptmock_configuration_and_records_usage(monkeypatch):
    config = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            config.update(kwargs)

        def invoke(self, messages):
            config["messages"] = messages
            return SimpleNamespace(
                content="Grounded response",
                usage_metadata={"input_tokens": 18, "output_tokens": 9},
                response_metadata={},
            )

    monkeypatch.setattr(chat, "ChatOpenAI", FakeChatOpenAI)
    monkeypatch.setenv("GPTMOCK_BASE_URL", "http://chatmock.test/v1")
    monkeypatch.setenv("GPTMOCK_MODEL", "fixture-model")
    monkeypatch.setenv("GPTMOCK_API_KEY", "test-key")

    provider = chat.get_chat_provider()
    response = provider.generate(
        [("system", "Ground the answer."), ("human", "Question")]
    )

    assert config["base_url"] == "http://chatmock.test/v1"
    assert config["model"] == "fixture-model"
    assert config["api_key"] == "test-key"
    assert config["use_responses_api"] is False
    assert response.content == "Grounded response"
    assert response.model_id == "openai-compatible:fixture-model"
    assert response.input_tokens == 18
    assert response.output_tokens == 9


def test_chat_provider_uses_legacy_openai_token_usage(monkeypatch):
    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            pass

        def invoke(self, messages):
            return SimpleNamespace(
                content="Grounded response",
                usage_metadata=None,
                response_metadata={
                    "token_usage": {"prompt_tokens": 4, "completion_tokens": 3}
                },
            )

    monkeypatch.setattr(chat, "ChatOpenAI", FakeChatOpenAI)
    response = chat.get_chat_provider().generate([("human", "Question")])

    assert response.input_tokens == 4
    assert response.output_tokens == 3
