import asyncio

import httpx
import pytest

import fleet_client as fc

PUBLIC = lambda host: ["93.184.216.34"]


def http_error(status):
    req = httpx.Request("GET", "https://x")
    return httpx.HTTPStatusError("boom lsv2_pt_secret", request=req, response=httpx.Response(status, request=req))


def test_extract_text_plain_and_blocks():
    assert fc.extract_text({"messages": [{"type": "human", "content": "q"}, {"type": "ai", "content": "hello"}]}) == "hello"
    blocks = [{"type": "thinking", "thinking": "secret"}, {"type": "text", "text": "Hi "}, {"type": "text", "text": "there"}]
    assert fc.extract_text({"messages": [{"type": "ai", "content": blocks}]}) == "Hi there"


def test_extract_text_skips_tool_messages_and_empty_ai():
    msgs = [
        {"type": "ai", "content": "final answer"},
        {"type": "tool", "content": "raw tool output"},
        {"type": "ai", "content": ""},
    ]
    assert fc.extract_text({"messages": msgs}) == "final answer"


@pytest.mark.parametrize("bad", [None, {}, {"messages": "x"}, {"messages": [{"type": "tool", "content": "t"}]}])
def test_extract_text_bad_responses(bad):
    with pytest.raises(fc.FleetError):
        fc.extract_text(bad)


@pytest.mark.parametrize("status,kind", [(401, "auth"), (403, "auth"), (404, "not_found"), (429, "rate_limit"), (402, "quota"), (503, "unavailable")])
def test_error_translation_never_leaks(status, kind):
    err = fc.translate_error(http_error(status))
    assert err.kind == kind and "lsv2" not in str(err)


def test_timeout_translation():
    assert fc.translate_error(httpx.ReadTimeout("t")).kind == "timeout"


class FakeClient:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail
        self.assistants = self
        self.threads = self
        self.runs = self

    async def get(self, agent_id):
        self.calls.append(("get", agent_id))
        if self.fail:
            raise http_error(self.fail)
        return {"assistant_id": agent_id}

    async def create(self):
        self.calls.append(("create",))
        return {"thread_id": "t-" + str(len(self.calls))}

    async def wait(self, thread_id, agent_id, input):
        self.calls.append(("wait", thread_id, agent_id, input))
        return {"messages": [{"type": "ai", "content": "ok"}]}


def patch(monkeypatch, client):
    monkeypatch.setattr(fc, "_make_client", lambda *a, **k: client)
    monkeypatch.setattr(fc, "validate_api_url", lambda u: u)


def test_verify_agent_success(monkeypatch):
    c = FakeClient()
    patch(monkeypatch, c)
    asyncio.run(fc.averify_agent("https://a.us.langgraph.app", "agent-1", "pat"))
    assert c.calls == [("get", "agent-1")]


def test_verify_auth_failure_not_retried(monkeypatch):
    c = FakeClient(fail=401)
    patch(monkeypatch, c)
    with pytest.raises(fc.FleetError) as e:
        asyncio.run(fc.averify_agent("https://a.us.langgraph.app", "agent-1", "pat"))
    assert e.value.kind == "auth" and len(c.calls) == 1


def test_send_only_new_message_with_thread(monkeypatch):
    c = FakeClient()
    patch(monkeypatch, c)
    out = asyncio.run(fc.asend_message("u", "agent-1", "pat", "thread-9", "hello"))
    assert out == "ok"
    assert c.calls == [("wait", "thread-9", "agent-1", {"messages": [{"role": "user", "content": "hello"}]})]


def test_client_built_with_owner_auth_scheme(monkeypatch):
    captured = {}

    class Dummy:
        pass

    import sys, types
    mod = types.ModuleType("langgraph_sdk")
    mod.get_client = lambda **kw: captured.update(kw) or Dummy()
    monkeypatch.setitem(sys.modules, "langgraph_sdk", mod)
    fc._make_client("https://a.us.langgraph.app", "pat", 5)
    assert captured["headers"] == {"X-Auth-Scheme": "langsmith-api-key"}
    assert captured["api_key"] == "pat"
