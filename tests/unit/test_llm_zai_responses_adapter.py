from core.llm.adapters.openai_compatible import OpenAICompatibleAdapter
from core.llm.profiles import ModelProfile


def _profile() -> ModelProfile:
    return ModelProfile(
        profile_id="api:zai:glm-5.3-flash:test",
        provider_id="zai",
        deployment_mode="api",
        model_name="glm-5.3-flash",
        base_url="https://open.bigmodel.cn/api/v1",
        credential_ref="configured",
        disable_thinking=False,
        request_timeout_seconds=99120,
        max_retries=0,
        context_window=1048576,
        supports_json_schema=True,
        supports_tools=True,
    )


class _FakeResponses:
    def __init__(self, response):
        self._response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self._response


def test_zai_uses_responses_contract_and_extracts_output_text():
    usage = type(
        "Usage",
        (),
        {
            "input_tokens": 25,
            "output_tokens": 34,
            "total_tokens": 59,
            "input_tokens_details": {"cached_tokens": 3},
            "output_tokens_details": {"reasoning_tokens": 31},
        },
    )()
    output = type(
        "Output",
        (),
        {
            "type": "message",
            "content": [type("Content", (), {"type": "output_text", "text": "OK"})()],
        },
    )()
    response = type("Response", (), {"output": [output], "usage": usage, "id": "resp_1"})()
    fake_responses = _FakeResponses(response)
    client = type("Client", (), {"responses": fake_responses})()
    adapter = OpenAICompatibleAdapter()
    adapter._build_client = lambda profile, credential: client

    assert adapter._uses_responses_api(_profile()) is True
    result = adapter.generate(
        profile=_profile(),
        credential="ignored",
        messages=[
            {"role": "system", "content": "Only reply OK."},
            {"role": "user", "content": "Please reply OK."},
        ],
        temperature=0,
        max_output_tokens=64,
    )

    assert result.content == "OK"
    assert result.provider_id == "zai"
    assert fake_responses.kwargs["instructions"] == "Only reply OK."
    assert fake_responses.kwargs["max_output_tokens"] == 64
    assert result.usage["prompt_tokens"] == 25
    assert result.usage["completion_tokens"] == 34
    assert result.usage["cached_prompt_tokens"] == 3
    assert result.usage["reasoning_tokens"] == 31
