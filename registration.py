"""Registration and owner-management logic (UI-independent so it can be tested)."""

import uuid

import fleet_client
import security
from database import BotRepository

MAX_NAME = 80
MAX_DESCRIPTION = 500


class ValidationError(Exception):
    """User-safe form validation error."""


def clean_fields(name: str, creator_name: str, description: str) -> tuple[str, str, str]:
    name, creator_name, description = (s.strip() for s in (name, creator_name, description))
    if not name:
        raise ValidationError("Enter a bot name.")
    if len(name) > MAX_NAME or len(creator_name) > MAX_NAME:
        raise ValidationError(f"Names can be up to {MAX_NAME} characters.")
    if len(description) > MAX_DESCRIPTION:
        raise ValidationError(f"Descriptions can be up to {MAX_DESCRIPTION} characters.")
    return name, creator_name, description


def clean_connection(agent_id: str, api_url: str, pat: str) -> tuple[str, str, str]:
    try:
        agent_id = str(uuid.UUID(agent_id.strip()))
    except ValueError:
        raise ValidationError("The Agent ID should look like 123e4567-e89b-12d3-a456-426614174000.") from None
    api_url = security.validate_api_url(api_url)  # SecurityError carries a friendly message
    pat = pat.strip()
    if not pat:
        raise ValidationError("Enter your Personal Access Token.")
    return agent_id, api_url, pat


def test_connection(agent_id: str, api_url: str, pat: str) -> None:
    agent_id, api_url, pat = clean_connection(agent_id, api_url, pat)
    fleet_client.verify_agent(api_url, agent_id, pat)


test_connection.__test__ = False  # not a pytest test


def register_bot(repo: BotRepository, encryption_key: str, *, name, agent_id, api_url, pat, acknowledged: bool,
                 creator_name="", description="") -> tuple[dict, str]:
    """Validate, verify against Fleet, encrypt, and save. Returns (bot, management_token)."""
    if not acknowledged:
        raise ValidationError("Confirm that you understand visitors will use your agent with your authorization.")
    name, creator_name, description = clean_fields(name, creator_name, description)
    agent_id, api_url, pat = clean_connection(agent_id, api_url, pat)
    fleet_client.verify_agent(api_url, agent_id, pat)  # never publish if this fails

    token = security.generate_management_token()
    bot = repo.create_bot(
        name=name,
        creator_name=creator_name,
        description=description,
        agent_id=agent_id,
        api_url=api_url,
        encrypted_pat=security.encrypt_secret(pat, encryption_key),
        manage_token_hash=security.hash_token(token),
    )
    return bot, token


def authorize_owner(repo: BotRepository, bot_id: str, token: str) -> bool:
    return security.verify_token(token, repo.get_manage_hash(bot_id) or "")


def replace_credential(repo: BotRepository, encryption_key: str, bot_id: str, token: str, pat: str) -> None:
    if not authorize_owner(repo, bot_id, token):
        raise security.SecurityError("That management link isn't valid.")
    target = repo.get_invocation_target(bot_id)
    pat = pat.strip()
    if not target or not pat:
        raise ValidationError("Enter your new Personal Access Token.")
    fleet_client.verify_agent(target["api_url"], target["agent_id"], pat)
    repo.replace_pat(bot_id, security.encrypt_secret(pat, encryption_key))


def update_details(repo: BotRepository, bot_id: str, token: str, name: str, description: str = "") -> None:
    if not authorize_owner(repo, bot_id, token):
        raise security.SecurityError("That management link isn't valid.")
    name, _, description = clean_fields(name, "", description)
    repo.update_details(bot_id, name, description)


def delete_bot(repo: BotRepository, bot_id: str, token: str) -> None:
    if not authorize_owner(repo, bot_id, token):
        raise security.SecurityError("That management link isn't valid.")
    repo.delete_bot(bot_id)


def chat_with_bot(repo: BotRepository, encryption_key: str, limiter: security.UsageLimiter,
                  visitor_id: str, bot_id: str, thread_id: str | None, message: str) -> tuple[str, str]:
    """Run one visitor turn. Decrypts the PAT only here. Returns (thread_id, reply)."""
    message = security.validate_message(message)
    if repo.invocation_disabled():
        raise security.SecurityError("Chatting is paused by the organizer right now.")
    target = repo.get_invocation_target(bot_id)
    if not target or target["status"] == "disabled":
        raise security.SecurityError("This bot isn't available.")
    with limiter.acquire(visitor_id, bot_id):
        pat = security.decrypt_secret(target["encrypted_pat"], encryption_key)
        try:
            if not thread_id:
                thread_id = fleet_client.create_thread(target["api_url"], pat)
            reply = fleet_client.send_message(target["api_url"], target["agent_id"], pat, thread_id, message)
        except fleet_client.FleetError as exc:
            if exc.kind in ("auth", "not_found"):
                repo.set_status(bot_id, "unavailable")  # owner must replace their token
            raise
    return thread_id, reply


def feedback_delta(prev, new) -> tuple[int, int]:
    """Counter changes (up, down) when a message's vote moves from prev to new.

    Votes: 1 = thumbs up, 0 = thumbs down, None = no vote.
    """
    up = (new == 1) - (prev == 1)
    down = (new == 0 and new is not None) - (prev == 0 and prev is not None)
    return up, down
