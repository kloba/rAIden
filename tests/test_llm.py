import json
from types import SimpleNamespace as NS

from raiden.config import Config
from raiden.llm import AzureLLM, from_responses, to_responses_input, to_responses_tools
from raiden.tools import TOOL_SPECS


def test_the_gpt6_family_uses_the_responses_api():
    assert Config(deployment="gpt-6.1-sol").use_responses
    assert Config(deployment="gpt-6-astra").use_responses
    assert not Config(deployment="gpt-5.5").use_responses
    assert not Config(deployment="gpt-5.6-sol").use_responses
    assert Config(deployment="gpt-5.5", api="responses").use_responses
    assert not Config(deployment="gpt-6.1-sol", api="chat").use_responses


def test_tools_are_flattened_for_responses():
    out = to_responses_tools(TOOL_SPECS)
    assert [t["name"] for t in out] == [t["function"]["name"] for t in TOOL_SPECS]
    assert all(t["type"] == "function" and "function" not in t and t["parameters"]["type"] == "object" for t in out)


def test_history_is_converted_to_input_items():
    raw = [{"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "abc"},
           {"type": "function_call", "call_id": "call_1", "name": "bash", "arguments": '{"command": "id"}'}]
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "TASK"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": '{"command": "id"}'}}], "raw_items": raw},
        {"role": "tool", "tool_call_id": "call_1", "content": "uid=1"},
        {"role": "assistant", "content": "thinking out loud", "tool_calls": [{"id": "call_2", "type": "function", "function": {"name": "finish", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_2", "content": "ok"},
    ]
    instructions, items = to_responses_input(messages)
    assert instructions == "SYS"
    assert items[0] == {"role": "user", "content": "TASK"}
    assert items[1:3] == raw  # replayed verbatim, reasoning included
    assert items[3] == {"type": "function_call_output", "call_id": "call_1", "output": "uid=1"}
    assert items[4] == {"role": "assistant", "content": [{"type": "output_text", "text": "thinking out loud"}]}
    assert items[5] == {"type": "function_call", "call_id": "call_2", "name": "finish", "arguments": "{}"}
    assert items[6] == {"type": "function_call_output", "call_id": "call_2", "output": "ok"}


class Item:
    def __init__(self, **d):
        self.d = d

    def model_dump(self, exclude_none=True):
        return dict(self.d)


def test_a_responses_result_looks_like_a_chat_completion():
    resp = NS(
        output=[
            Item(type="reasoning", id="rs_9", summary=[], encrypted_content="zzz", status="completed"),
            Item(type="message", id="msg_1", role="assistant", status="completed", content=[{"type": "output_text", "text": "Checking."}]),
            Item(type="function_call", id="fc_1", call_id="call_a", name="pg_probe", arguments="{}", status="completed"),
        ],
        usage=NS(input_tokens=1200, output_tokens=80, input_tokens_details=NS(cached_tokens=1000)),
    )
    out = from_responses(resp)
    msg = out.choices[0].message
    assert msg.content == "Checking." and [(c.id, c.function.name, c.function.arguments) for c in msg.tool_calls] == [("call_a", "pg_probe", "{}")]
    assert msg.raw_items[0] == {"type": "reasoning", "id": "rs_9", "summary": [], "encrypted_content": "zzz"}  # status dropped
    assert msg.raw_items[2] == {"type": "function_call", "call_id": "call_a", "name": "pg_probe", "arguments": "{}"}
    assert (out.usage.prompt_tokens, out.usage.completion_tokens, out.usage.prompt_tokens_details.cached_tokens) == (1200, 80, 1000)
    plain = from_responses(NS(output=[Item(type="message", role="assistant", content=[{"type": "output_text", "text": "done"}])], usage=None))
    assert plain.choices[0].message.tool_calls is None and plain.choices[0].message.content == "done" and plain.usage.prompt_tokens == 0


def test_the_request_has_the_right_shape():
    seen = {}

    class FakeResponses:
        def create(self, **kw):
            seen.update(kw)
            return NS(output=[Item(type="function_call", call_id="c1", name="finish", arguments=json.dumps({"resolution": "resolved", "summary": "x"}))], usage=None)

    llm = AzureLLM(Config(deployment="gpt-6.1-sol", reasoning_effort="medium"))
    llm._client = NS(responses=FakeResponses())
    res = llm._call(llm._client, [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}], TOOL_SPECS, 1234)
    assert seen["model"] == "gpt-6.1-sol" and seen["instructions"] == "S" and seen["store"] is False
    assert seen["reasoning"] == {"effort": "medium"} and seen["max_output_tokens"] == 1234
    assert seen["include"] == ["reasoning.encrypted_content"] and seen["input"] == [{"role": "user", "content": "U"}]
    assert res.choices[0].message.tool_calls[0].function.name == "finish"


def test_chat_models_keep_the_chat_request():
    seen = {}

    class FakeChat:
        class completions:
            @staticmethod
            def create(**kw):
                seen.update(kw)
                return "chat-result"

    llm = AzureLLM(Config(deployment="gpt-5.5"))
    llm._client = NS(chat=FakeChat)
    assert llm._call(llm._client, [{"role": "user", "content": "U"}], TOOL_SPECS, 16000) == "chat-result"
    assert seen["reasoning_effort"] == "medium" and seen["max_completion_tokens"] == 16000 and seen["tools"] == TOOL_SPECS
