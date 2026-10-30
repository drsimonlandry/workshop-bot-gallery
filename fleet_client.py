"""Server-side Fleet integration via the LangGraph Python SDK.

Reference: https://docs.langchain.com/langsmith/fleet/code
Auth: Personal Access Token of the agent owner + `X-Auth-Scheme: langsmith-api-key`.
"""

import asyncio
import logging

import httpx

from security import validate_api_url

log = logging.getLogger(__name__)

TIMEOUT_S = 90.0
VERIFY_TIMEOUT_S = 20.0
TRANSIENT_RETRIES = 1


class FleetError(Exception):
    """Carries a user-safe message; never includes credentials or raw API bodies."""

    def __init__(self, message: str, kind: str = "unknown", detail: str = ""):
        super().__init__(message)
        self.kind = kind
        self.detail = detail  # structure only, never content; shown when DEBUG is on


def _make_client(api_url: str, pat: str, timeout: float):
    from langgraph_sdk import get_client

    return get_client(
        url=api_url,
        api_key=pat,
        headers={"X-Auth-Scheme": "langsmith-api-key"},
        timeout=timeout,
    )


def _status_of(exc: Exception):
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def translate_error(exc: Exception, context: str = "run") -> FleetError:
    """Map SDK/HTTP failures to friendly messages. Never echoes the raw error."""
    if isinstance(exc, FleetError):
        return exc
    status = _status_of(exc)
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        return FleetError("The agent took too long to respond. Try again.", "timeout")
    if status in (401, 403):
        return FleetError("Authentication failed. Check your Personal Access Token.", "auth")
    if status == 404:
        msg = (
            "Couldn't connect to your agent. Check the Agent ID, API URL, and that the token "
            "belongs to the agent's owner."
            if context == "verify"
            else "This bot is unavailable. Its owner may need to update it."
        )
        return FleetError(msg, "not_found")
    if status == 429:
        return FleetError("The agent is rate limited right now. Try again shortly.", "rate_limit")
    if status == 402:
        return FleetError("This agent has used up its Fleet allowance.", "quota")
    if status is not None and status >= 500:
        return FleetError("Your agent could not be reached. Try again.", "unavailable")
    if isinstance(exc, httpx.TransportError):
        return FleetError("Your agent could not be reached. Try again.", "unavailable")
    # Log the exception class only: messages can embed request details.
    log.warning("Unexpected Fleet failure (%s): %s", context, type(exc).__name__)
    return FleetError("Something went wrong talking to the agent. Try again.", "unknown")


async def _with_retry(factory):
    """Retry only transient failures (timeouts, 5xx, transport errors); never auth errors."""
    attempt = 0
    while True:
        try:
            return await factory()
        except Exception as exc:  # noqa: BLE001
            err = translate_error(exc)
            if err.kind in ("unavailable",) and attempt < TRANSIENT_RETRIES:
                attempt += 1
                await asyncio.sleep(1.0)
                continue
            raise err from None


def _describe(result) -> str:
    """Shape of a response (keys, message types, content kinds) without any content."""
    if not isinstance(result, dict):
        return f"result={type(result).__name__}"
    msgs = result.get("messages")
    parts = []
    for m in msgs if isinstance(msgs, list) else []:
        if isinstance(m, dict):
            c = m.get("content")
            kinds = [b.get("type") if isinstance(b, dict) else type(b).__name__ for b in c] if isinstance(c, list) else type(c).__name__
            parts.append(f"{m.get('type') or m.get('role')}:{kinds}")
    return f"keys={sorted(result)} messages={parts}"


def extract_text(result) -> str:
    """Return the final user-visible assistant text from a runs.wait() result.

    Ignores tool messages, reasoning blocks, and non-text content.
    """
    messages = result.get("messages") if isinstance(result, dict) else None
    if not isinstance(messages, list):
        raise FleetError("The agent returned an unexpected response. Try again.", "bad_response", _describe(result))
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("type") or message.get("role") or "").lower()
        if role not in ("ai", "assistant", "aimessage", "aimessagechunk"):
            continue
        text = _content_to_text(message.get("content"))
        if text:
            return text
    if isinstance(result, dict) and result.get("__interrupt__"):
        raise FleetError("This agent is waiting for an approval and can't answer in the gallery.", "bad_response", _describe(result))
    raise FleetError("The agent didn't send a reply. Try again.", "bad_response", _describe(result))


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts).strip()
    return ""


async def averify_agent(api_url: str, agent_id: str, pat: str) -> None:
    api_url = validate_api_url(api_url)
    client = _make_client(api_url, pat, VERIFY_TIMEOUT_S)

    async def go():
        try:
            return await client.assistants.get(agent_id)
        except Exception as exc:  # noqa: BLE001
            raise translate_error(exc, "verify") from None

    await _with_retry(go)


async def acreate_thread(api_url: str, pat: str) -> str:
    client = _make_client(validate_api_url(api_url), pat, VERIFY_TIMEOUT_S)
    thread = await _with_retry(lambda: client.threads.create())
    thread_id = thread.get("thread_id") if isinstance(thread, dict) else None
    if not thread_id:
        raise FleetError("The agent returned an unexpected response. Try again.", "bad_response")
    return thread_id


async def asend_message(api_url: str, agent_id: str, pat: str, thread_id: str, message: str) -> str:
    """Send only the new message; the Fleet thread holds the history."""
    client = _make_client(validate_api_url(api_url), pat, TIMEOUT_S)

    async def go():
        return await client.runs.wait(
            thread_id,
            agent_id,
            input={"messages": [{"role": "user", "content": message}]},
        )

    # No retry on runs: a retry could duplicate the user's message in the thread.
    try:
        result = await asyncio.wait_for(go(), timeout=TIMEOUT_S + 5)
    except Exception as exc:  # noqa: BLE001
        raise translate_error(exc) from None
    return extract_text(result)


# Sync wrappers for Streamlit.

def verify_agent(api_url: str, agent_id: str, pat: str) -> None:
    asyncio.run(averify_agent(api_url, agent_id, pat))


def create_thread(api_url: str, pat: str) -> str:
    return asyncio.run(acreate_thread(api_url, pat))


def send_message(api_url: str, agent_id: str, pat: str, thread_id: str, message: str) -> str:
    return asyncio.run(asend_message(api_url, agent_id, pat, thread_id, message))
