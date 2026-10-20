"""A scripted stand-in for the model: each step is a list of tool calls, or a string for a plain reply."""

import json
from types import SimpleNamespace as NS


def call(name, **args):
    return {"name": name, "args": args}


class FakeLLM:
    def __init__(self, steps):
        self.steps = list(steps)
        self.seen = []

    async def complete(self, messages, tools=None, max_completion_tokens=16000):
        self.seen.append(list(messages))
        step = self.steps.pop(0) if self.steps else [call("finish", resolution="escalated", summary="script exhausted")]
        if isinstance(step, str):
            msg = NS(content=step, tool_calls=None)
        else:
            calls = []
            for i, c in enumerate(step):
                raw = c["args"] if isinstance(c["args"], str) else json.dumps(c["args"])
                calls.append(NS(id=f"call_{len(self.seen)}_{i}", function=NS(name=c["name"], arguments=raw)))
            msg = NS(content=None, tool_calls=calls)
        usage = NS(prompt_tokens=100, completion_tokens=20, prompt_tokens_details=NS(cached_tokens=10))
        return NS(choices=[NS(message=msg)], usage=usage)
