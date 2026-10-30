"""Workshop Bot Gallery: browse, register, and chat with LangSmith Fleet agents."""

import uuid

import streamlit as st

import fleet_client
import registration
import security
from database import BotRepository, DatabaseError

st.set_page_config(page_title="Workshop Bot Gallery", page_icon="🤖", layout="wide")

st.markdown(
    """
    <style>
      .block-container { max-width: 1100px; padding-top: 2rem; }
      [data-testid="stVerticalBlockBorderWrapper"] { border-radius: 14px; }
      /* user messages on the right */
      [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) { flex-direction: row-reverse; text-align: right; }
    </style>
    """,
    unsafe_allow_html=True,
)


# --- shared resources ------------------------------------------------------

@st.cache_resource
def get_repo() -> BotRepository:
    return BotRepository.from_secrets(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_SERVICE_ROLE_KEY"])


@st.cache_resource
def get_limiter() -> security.UsageLimiter:
    return security.UsageLimiter()


def enc_key() -> str:
    return st.secrets["ENCRYPTION_KEY"]


def ss():
    s = st.session_state
    s.setdefault("view", "gallery")
    s.setdefault("visitor_id", uuid.uuid4().hex)  # per browser session; never shared
    s.setdefault("convs", {})  # bot_id -> conversation
    return s


def go(view: str, **extra):
    st.session_state.view = view
    st.session_state.update(extra)
    st.rerun()


def guarded(fn, *args, **kwargs):
    """Run fn and show a friendly error instead of a traceback. Returns (ok, value)."""
    try:
        return True, fn(*args, **kwargs)
    except (registration.ValidationError, security.SecurityError, fleet_client.FleetError, DatabaseError) as exc:
        st.error(str(exc))
    except Exception:  # noqa: BLE001 - never leak internals
        st.error("Something went wrong. Try again.")
    return False, None


# --- gallery ---------------------------------------------------------------

def view_gallery():
    left, right = st.columns([4, 1], vertical_alignment="center")
    with left:
        st.title("Workshop Bot Gallery")
        st.caption("Explore and test AI agents built by workshop participants.")
    with right:
        if st.button("Add your bot", type="primary", use_container_width=True):
            go("register")

    ok, bots = guarded(get_repo().list_public_bots)
    if not ok:
        return
    if not bots:
        st.info("No bots yet. Be the first to share your AI agent.")
        if st.button("Add your bot", type="primary", key="add_empty"):
            go("register")
        return

    query = st.text_input("Search", placeholder="Search by name, creator, or description", label_visibility="collapsed").lower()
    shown = [b for b in bots if not query or any(query in b[k].lower() for k in ("name", "creator_name", "description"))]
    st.caption(f"{len(bots)} registered bot{'s' if len(bots) != 1 else ''}"
               + (f" · {len(shown)} match" if query else ""))

    cols = st.columns(3)
    for i, bot in enumerate(shown):
        with cols[i % 3].container(border=True):
            st.subheader(bot["name"])
            st.caption(f"by {bot['creator_name']}")
            st.write(bot["description"])
            available = bot["status"] == "active"
            st.markdown(":green[● Available]" if available else ":gray[● Unavailable]")
            if st.button("Chat", key=f"chat_{bot['id']}", disabled=not available, use_container_width=True, type="primary"):
                go("chat", bot_id=bot["id"])


# --- register --------------------------------------------------------------

HELP = """
**Agent ID and API URL**
1. Open LangSmith Fleet.
2. Open your agent.
3. Expand **Advanced settings**.
4. Under **Developer**, select **View code snippets**.
5. Copy the Agent ID and API URL.

**Personal Access Token**
1. Open LangSmith's **API Keys** page.
2. Click **Create API Key**.
3. Select **Personal Access Token**.
4. Enter "Workshop Bot Gallery" as the description.
5. Choose an expiration date that covers the workshop and no longer.
6. Generate and copy the key.

This token acts as **you**, not just for one bot. Anyone who uses your bot in the gallery runs it with your authorization.
Use a personal or trial account, and don't publish agents connected to private documents, email, or tools that can take sensitive actions.
"""


def view_register():
    if "published" in st.session_state:
        return view_published()

    if st.button("← Back to gallery"):
        go("gallery")
    st.title("Add your bot")

    with st.expander("Where do I find this information?"):
        st.markdown(HELP)

    name = st.text_input("Bot name", max_chars=registration.MAX_NAME)
    creator = st.text_input("Your name", max_chars=registration.MAX_NAME)
    desc = st.text_area("Description", max_chars=registration.MAX_DESCRIPTION, help="What does your bot do?")
    agent_id = st.text_input("Agent ID")
    api_url = st.text_input("API URL", placeholder="https://your-agent.us.langgraph.app")
    pat = st.text_input("Personal Access Token", type="password")
    ack = st.checkbox(
        "I understand that anyone with the gallery link can use my agent with my authorization, "
        "and my agent has no private data or sensitive tools."
    )

    c1, c2 = st.columns(2)
    if c1.button("Test connection", use_container_width=True):
        with st.spinner("Testing connection…"):
            ok, _ = guarded(registration.test_connection, agent_id, api_url, pat)
        if ok:
            st.success("Connection successful")
    if c2.button("Publish bot", type="primary", use_container_width=True):
        with st.spinner("Publishing…"):
            ok, result = guarded(
                registration.register_bot, get_repo(), enc_key(),
                name=name, creator_name=creator, description=desc,
                agent_id=agent_id, api_url=api_url, pat=pat, acknowledged=ack,
            )
        if ok:
            bot, token = result
            st.session_state.published = {"bot_id": bot["id"], "token": token, "name": bot["name"]}
            st.rerun()


def view_published():
    p = st.session_state.published
    st.success(f"{p['name']} is live in the gallery.")
    st.warning("Save your private management link now. It's shown once and is the only way to edit or remove your bot.")
    base = st.secrets.get("APP_URL", "").rstrip("/")
    st.code(f"{base}/?manage={p['bot_id']}.{p['token']}", language=None)
    if st.button("Go to gallery", type="primary"):
        del st.session_state.published
        go("gallery")


# --- chat ------------------------------------------------------------------

def view_chat():
    s = st.session_state
    bot_id = s.get("bot_id")
    ok, bot = guarded(get_repo().get_public_bot, bot_id) if bot_id else (True, None)
    if not ok:
        return
    if not bot:
        st.warning("That bot isn't available.")
        if st.button("Back to gallery"):
            go("gallery")
        return

    conv = s.convs.setdefault(bot_id, {"thread_id": None, "messages": [], "pending": None, "error": None})

    top, b1, b2 = st.columns([4, 1, 1], vertical_alignment="center")
    with top:
        st.title(bot["name"])
        st.caption(f"by {bot['creator_name']} · {bot['description']}")
    if b1.button("← Gallery", use_container_width=True):
        go("gallery")
    if b2.button("New conversation", use_container_width=True):
        s.convs[bot_id] = {"thread_id": None, "messages": [], "pending": None, "error": None}
        st.rerun()

    for m in conv["messages"]:
        with st.chat_message(m["role"]):
            st.write(m["content"])

    text = st.chat_input("Type a message", max_chars=security.MAX_MESSAGE_CHARS)
    retry = False
    if conv["error"] and conv["pending"]:
        st.error(conv["error"])
        retry = st.button("Retry")

    if text and not conv["pending"]:
        try:
            text = security.validate_message(text)
        except security.SecurityError as exc:
            st.error(str(exc))
            return
        conv["messages"].append({"role": "user", "content": text})
        conv["pending"] = text
        retry = True  # same code path as retry, so one submission = one request
        with st.chat_message("user"):
            st.write(text)

    if retry and conv["pending"]:
        with st.chat_message("assistant"):
            with st.spinner("Thinking…"):
                try:
                    conv["thread_id"], reply = registration.chat_with_bot(
                        get_repo(), enc_key(), get_limiter(), s.visitor_id,
                        bot_id, conv["thread_id"], conv["pending"],
                    )
                except (registration.ValidationError, security.SecurityError,
                        fleet_client.FleetError, DatabaseError) as exc:
                    conv["error"] = str(exc)
                except Exception:  # noqa: BLE001
                    conv["error"] = "Something went wrong. Try again."
                else:
                    conv["messages"].append({"role": "assistant", "content": reply})
                    conv["pending"], conv["error"] = None, None
        st.rerun()


# --- manage ----------------------------------------------------------------

def view_manage():
    s = st.session_state
    st.title("Manage your bot")
    if st.button("← Back to gallery"):
        s.pop("manage", None)
        go("gallery")

    if "manage" not in s:
        link = st.text_input("Paste your management link or code", type="password")
        if st.button("Continue") and link:
            code = link.split("manage=")[-1].strip()
            bot_id, _, token = code.partition(".")
            s.manage = {"bot_id": bot_id, "token": token}
            st.rerun()
        return

    bot_id, token = s.manage["bot_id"], s.manage["token"]
    repo = get_repo()
    ok, authorized = guarded(registration.authorize_owner, repo, bot_id, token)
    if not ok:
        return
    if not authorized:
        st.error("That management link isn't valid.")
        s.pop("manage", None)
        return
    ok, bot = guarded(repo.get_public_bot, bot_id)
    if not ok or not bot:
        return

    st.subheader("Edit details")
    name = st.text_input("Bot name", bot["name"], max_chars=registration.MAX_NAME)
    desc = st.text_area("Description", bot["description"], max_chars=registration.MAX_DESCRIPTION)
    if st.button("Save changes"):
        ok, _ = guarded(registration.update_details, repo, bot_id, token, name, desc)
        if ok:
            st.success("Saved")

    st.subheader("Replace credential")
    st.caption("Use this when your token expires. The new token is tested before it's saved.")
    pat = st.text_input("New Personal Access Token", type="password")
    if st.button("Replace token"):
        with st.spinner("Testing…"):
            ok, _ = guarded(registration.replace_credential, repo, enc_key(), bot_id, token, pat)
        if ok:
            st.success("Token replaced")

    st.subheader("Remove bot")
    if st.checkbox("I want to remove this bot and delete its saved credential") and st.button("Delete bot", type="primary"):
        ok, _ = guarded(registration.delete_bot, repo, bot_id, token)
        if ok:
            s.pop("manage", None)
            go("gallery")


# --- organizer -------------------------------------------------------------

def view_admin():
    st.title("Organizer")
    if st.button("← Back to gallery"):
        go("gallery")
    if not st.session_state.get("is_admin"):
        secret = st.text_input("Organizer secret", type="password")
        if st.button("Sign in"):
            if security.check_admin_secret(secret, st.secrets.get("ORGANIZER_ADMIN_SECRET", "")):
                st.session_state.is_admin = True
                st.rerun()
            st.error("Wrong secret.")
        return

    repo = get_repo()
    ok, off = guarded(repo.invocation_disabled)
    if not ok:
        return
    if st.toggle("Pause all chatting", value=off) != off:
        repo.set_invocation_disabled(not off)
        st.rerun()

    ok, bots = guarded(repo.list_all_for_admin)
    for b in bots or []:
        c = st.columns([3, 1, 1, 1], vertical_alignment="center")
        c[0].write(f"**{b['name']}** by {b['creator_name']} ({b['status']})")
        nxt = "active" if b["status"] == "disabled" else "disabled"
        if c[1].button("Enable" if nxt == "active" else "Disable", key=f"t{b['id']}"):
            repo.set_status(b["id"], nxt)
            st.rerun()
        if c[2].button("Remove", key=f"d{b['id']}"):
            repo.delete_bot(b["id"])
            st.rerun()

    st.divider()
    st.subheader("End of workshop")
    if st.checkbox("Delete ALL bots and saved credentials") and st.button("Delete everything", type="primary"):
        repo.delete_all_bots()
        st.success("All bots deleted.")


# --- router ----------------------------------------------------------------

def main():
    s = ss()
    params = st.query_params
    if "manage" in params:  # private link: capture then strip from the URL
        bot_id, _, token = params["manage"].partition(".")
        s.manage = {"bot_id": bot_id, "token": token}
        s.view = "manage"
        st.query_params.clear()
    elif "admin" in params:
        s.view = "admin"
        st.query_params.clear()

    {"gallery": view_gallery, "register": view_register, "chat": view_chat,
     "manage": view_manage, "admin": view_admin}.get(s.view, view_gallery)()


main()
