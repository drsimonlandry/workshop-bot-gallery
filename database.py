"""Supabase access. All writes go through these application-controlled methods."""

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Columns safe to return to visitors. Never includes encrypted_pat or manage_token_hash.
PUBLIC_COLUMNS = "id,name,creator_name,description,status,created_at,chat_count,thumbs_up,thumbs_down"


class DatabaseError(Exception):
    """User-safe database failure. `detail` is for organizer debugging only."""

    detail = ""


class DuplicateBotError(DatabaseError):
    pass


def _wrap(exc: Exception) -> DatabaseError:
    if getattr(exc, "code", None) == "23505":
        return DuplicateBotError("That agent is already registered in the gallery.")
    detail = f"{type(exc).__name__} code={getattr(exc, 'code', None)} message={str(getattr(exc, 'message', exc))[:300]}"
    print("Database error:", detail, flush=True)  # stdout shows in Streamlit logs; no credentials in PostgREST errors
    err = DatabaseError("Something went wrong saving your data. Try again.")
    err.detail = detail
    return err


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BotRepository:
    def __init__(self, client):
        self._db = client

    @classmethod
    def from_secrets(cls, url: str, service_role_key: str):
        from supabase import create_client

        return cls(create_client(url, service_role_key))

    def _run(self, query):
        try:
            return query.execute().data
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc) from None

    # --- public reads ---
    def list_public_bots(self) -> list[dict]:
        return self._run(
            self._db.table("bots").select(PUBLIC_COLUMNS).neq("status", "disabled")
            .order("chat_count").order("created_at", desc=True)  # least popular first
        )

    def get_public_bot(self, bot_id: str) -> dict | None:
        rows = self._run(self._db.table("bots").select(PUBLIC_COLUMNS).eq("id", bot_id).neq("status", "disabled"))
        return rows[0] if rows else None

    # --- server-only reads ---
    def get_invocation_target(self, bot_id: str) -> dict | None:
        """Includes the encrypted PAT. Use only to execute an authorized Fleet request."""
        rows = self._run(
            self._db.table("bots").select("id,agent_id,api_url,encrypted_pat,status").eq("id", bot_id)
        )
        return rows[0] if rows else None

    def get_manage_hash(self, bot_id: str) -> str | None:
        rows = self._run(self._db.table("bots").select("manage_token_hash").eq("id", bot_id))
        return rows[0]["manage_token_hash"] if rows else None

    # --- writes ---
    def create_bot(self, *, name, creator_name, description, agent_id, api_url, encrypted_pat, manage_token_hash) -> dict:
        rows = self._run(
            self._db.table("bots").insert(
                {
                    "name": name,
                    "creator_name": creator_name,
                    "description": description,
                    "agent_id": agent_id,
                    "api_url": api_url,
                    "encrypted_pat": encrypted_pat,
                    "manage_token_hash": manage_token_hash,
                    "status": "active",
                }
            )
        )
        return {k: rows[0][k] for k in ("id", "name")}

    def update_details(self, bot_id: str, name: str, description: str) -> None:
        self._run(
            self._db.table("bots").update({"name": name, "description": description, "updated_at": _now()}).eq("id", bot_id)
        )

    def replace_pat(self, bot_id: str, encrypted_pat: str) -> None:
        self._run(
            self._db.table("bots")
            .update({"encrypted_pat": encrypted_pat, "status": "active", "updated_at": _now()})
            .eq("id", bot_id)
        )

    def set_status(self, bot_id: str, status: str) -> None:
        if status not in ("active", "unavailable", "disabled"):
            raise ValueError("invalid status")
        self._run(self._db.table("bots").update({"status": status, "updated_at": _now()}).eq("id", bot_id))

    def delete_bot(self, bot_id: str) -> None:
        """Deleting the row also deletes the stored credential."""
        self._run(self._db.table("bots").delete().eq("id", bot_id))

    def delete_all_bots(self) -> None:
        self._run(self._db.table("bots").delete().neq("id", "00000000-0000-0000-0000-000000000000"))

    def bump(self, bot_id: str, chats: int = 0, up: int = 0, down: int = 0) -> None:
        """Atomically adjust popularity counters. Counts only; no chat content is stored."""
        try:
            self._db.rpc("bump_bot", {"p_bot": bot_id, "p_chats": chats, "p_up": up, "p_down": down}).execute()
        except Exception as exc:  # noqa: BLE001
            raise _wrap(exc) from None

    # --- organizer ---
    def list_all_for_admin(self) -> list[dict]:
        return self._run(self._db.table("bots").select(PUBLIC_COLUMNS + ",agent_id").order("created_at", desc=True))

    def invocation_disabled(self) -> bool:
        rows = self._run(self._db.table("app_settings").select("value").eq("key", "invocation_disabled"))
        return bool(rows) and rows[0]["value"] == "true"

    def set_invocation_disabled(self, disabled: bool) -> None:
        self._run(
            self._db.table("app_settings").upsert({"key": "invocation_disabled", "value": "true" if disabled else "false"})
        )
