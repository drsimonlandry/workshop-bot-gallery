import uuid

import pytest

import fleet_client
import registration as reg
import security as sec

AGENT = str(uuid.uuid4())
URL = "https://bot.us.langgraph.app"
PAT = "lsv2_pt_supersecret"


class FakeRepo:
    """In-memory stand-in for BotRepository, mirroring its public/private split."""

    def __init__(self):
        self.rows, self.disabled = {}, False

    def create_bot(self, **kw):
        bid = str(uuid.uuid4())
        self.rows[bid] = {"id": bid, "status": "active", **kw}
        return {"id": bid, "name": kw["name"]}

    def list_public_bots(self):
        keys = ("id", "name", "creator_name", "description", "status")
        return [{k: r[k] for k in keys} for r in self.rows.values()]

    def get_invocation_target(self, bid):
        r = self.rows.get(bid)
        return r and {k: r[k] for k in ("id", "agent_id", "api_url", "encrypted_pat", "status")}

    def get_manage_hash(self, bid):
        r = self.rows.get(bid)
        return r and r["manage_token_hash"]

    def delete_bot(self, bid):
        self.rows.pop(bid, None)

    def replace_pat(self, bid, enc):
        self.rows[bid]["encrypted_pat"] = enc

    def update_details(self, bid, name, desc):
        self.rows[bid].update(name=name, description=desc)

    def set_status(self, bid, s):
        self.rows[bid]["status"] = s

    def invocation_disabled(self):
        return self.disabled


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(sec, "_resolve", lambda h: ["93.184.216.34"])
    monkeypatch.setattr(sec.validate_api_url, "__defaults__", (lambda h: ["93.184.216.34"],))
    monkeypatch.setattr(fleet_client, "verify_agent", lambda *a: None)


KEY = sec.generate_encryption_key()


def register(repo, **over):
    args = dict(name="Bot", creator_name="Sam", description="Does things", agent_id=AGENT, api_url=URL, pat=PAT, acknowledged=True)
    args.update(over)
    return reg.register_bot(repo, KEY, **args)


def test_register_encrypts_and_hashes():
    repo = FakeRepo()
    bot, token = register(repo)
    row = repo.rows[bot["id"]]
    assert PAT not in row["encrypted_pat"] and sec.decrypt_secret(row["encrypted_pat"], KEY) == PAT
    assert token not in row["manage_token_hash"] and sec.verify_token(token, row["manage_token_hash"])


def test_minimal_registration_needs_only_name_and_connection():
    repo = FakeRepo()
    bot, _ = reg.register_bot(repo, KEY, name="Bot", agent_id=AGENT, api_url=URL, pat=PAT, acknowledged=True)
    assert repo.rows[bot["id"]]["creator_name"] == "" and repo.rows[bot["id"]]["description"] == ""
    with pytest.raises(reg.ValidationError):
        reg.register_bot(repo, KEY, name=" ", agent_id=AGENT, api_url=URL, pat=PAT, acknowledged=True)


def test_public_listing_never_exposes_credentials():
    repo = FakeRepo()
    register(repo)
    assert "encrypted_pat" not in repo.list_public_bots()[0]
    assert PAT not in repr(repo.list_public_bots())


def test_failed_validation_blocks_publish(monkeypatch):
    def boom(*a):
        raise fleet_client.FleetError("Authentication failed.", "auth")
    monkeypatch.setattr(fleet_client, "verify_agent", boom)
    repo = FakeRepo()
    with pytest.raises(fleet_client.FleetError):
        register(repo)
    assert not repo.rows


def test_requires_acknowledgment_and_safe_url():
    repo = FakeRepo()
    with pytest.raises(reg.ValidationError):
        register(repo, acknowledged=False)
    with pytest.raises(sec.SecurityError):
        register(repo, api_url="https://evil.com")
    with pytest.raises(reg.ValidationError):
        register(repo, agent_id="not-a-uuid")
    assert not repo.rows


def test_owner_management_requires_token():
    repo = FakeRepo()
    bot, token = register(repo)
    with pytest.raises(sec.SecurityError):
        reg.delete_bot(repo, bot["id"], "wrong")
    with pytest.raises(sec.SecurityError):
        reg.update_details(repo, bot["id"], "wrong", "n", "d")
    reg.update_details(repo, bot["id"], token, "New", "Desc")
    assert repo.rows[bot["id"]]["name"] == "New"
    reg.replace_credential(repo, KEY, bot["id"], token, "lsv2_pt_new")
    assert sec.decrypt_secret(repo.rows[bot["id"]]["encrypted_pat"], KEY) == "lsv2_pt_new"
    reg.delete_bot(repo, bot["id"], token)
    assert not repo.rows  # credential gone with the row


def test_credential_isolation_between_bots(monkeypatch):
    repo = FakeRepo()
    a, _ = register(repo, pat="pat-A", agent_id=str(uuid.uuid4()))
    b, _ = register(repo, pat="pat-B", agent_id=str(uuid.uuid4()))
    used = []
    monkeypatch.setattr(fleet_client, "create_thread", lambda u, p: "t")
    monkeypatch.setattr(fleet_client, "send_message", lambda u, aid, p, t, m: used.append((aid, p)) or "r")
    lim = sec.UsageLimiter()
    reg.chat_with_bot(repo, KEY, lim, "v", a["id"], None, "hi")
    reg.chat_with_bot(repo, KEY, lim, "v", b["id"], None, "hi")
    assert used[0][1] == "pat-A" and used[1][1] == "pat-B"
    assert used[0][0] == repo.rows[a["id"]]["agent_id"]


def test_thread_isolation_between_visitors(monkeypatch):
    repo = FakeRepo()
    bot, _ = register(repo)
    counter = iter(range(100))
    monkeypatch.setattr(fleet_client, "create_thread", lambda u, p: f"thread-{next(counter)}")
    seen = []
    monkeypatch.setattr(fleet_client, "send_message", lambda u, a, p, t, m: seen.append(t) or "r")
    lim = sec.UsageLimiter()
    t1, _ = reg.chat_with_bot(repo, KEY, lim, "visitor1", bot["id"], None, "hi")
    t2, _ = reg.chat_with_bot(repo, KEY, lim, "visitor2", bot["id"], None, "hi")
    assert t1 != t2
    t1b, _ = reg.chat_with_bot(repo, KEY, lim, "visitor1", bot["id"], t1, "again")
    assert t1b == t1 and seen == [t1, t2, t1]  # same visitor reuses own thread only


def test_chat_blocked_when_paused_or_disabled(monkeypatch):
    repo = FakeRepo()
    bot, _ = register(repo)
    monkeypatch.setattr(fleet_client, "create_thread", lambda u, p: "t")
    monkeypatch.setattr(fleet_client, "send_message", lambda *a: "r")
    repo.disabled = True
    with pytest.raises(sec.SecurityError):
        reg.chat_with_bot(repo, KEY, sec.UsageLimiter(), "v", bot["id"], None, "hi")
    repo.disabled = False
    repo.rows[bot["id"]]["status"] = "disabled"
    with pytest.raises(sec.SecurityError):
        reg.chat_with_bot(repo, KEY, sec.UsageLimiter(), "v", bot["id"], None, "hi")


def test_auth_failure_marks_bot_unavailable(monkeypatch):
    repo = FakeRepo()
    bot, _ = register(repo)
    monkeypatch.setattr(fleet_client, "create_thread", lambda u, p: "t")
    def boom(*a):
        raise fleet_client.FleetError("Authentication failed.", "auth")
    monkeypatch.setattr(fleet_client, "send_message", boom)
    with pytest.raises(fleet_client.FleetError):
        reg.chat_with_bot(repo, KEY, sec.UsageLimiter(), "v", bot["id"], None, "hi")
    assert repo.rows[bot["id"]]["status"] == "unavailable"


@pytest.mark.parametrize("prev,new,expected", [
    (None, 1, (1, 0)), (None, 0, (0, 1)), (1, 0, (-1, 1)), (0, 1, (1, -1)),
    (1, None, (-1, 0)), (0, None, (0, -1)), (1, 1, (0, 0)), (None, None, (0, 0)),
])
def test_feedback_delta(prev, new, expected):
    assert reg.feedback_delta(prev, new) == expected


def test_repository_orders_least_popular_first_and_bumps_atomically():
    from database import BotRepository

    calls = []

    class Q:
        def __getattr__(self, name):
            def f(*a, **k):
                calls.append((name, a, k))
                return self
            return f

        def execute(self):
            class R:
                data = []
            return R()

    class C:
        def table(self, name):
            return Q()

        def rpc(self, fn, params):
            calls.append(("rpc", fn, params))
            return Q()

    repo = BotRepository(C())
    repo.list_public_bots()
    order = [c for c in calls if c[0] == "order"]
    assert order[0][1] == ("chat_count",) and order[1][1] == ("created_at",)
    repo.bump("b1", chats=1)
    assert ("rpc", "bump_bot", {"p_bot": "b1", "p_chats": 1, "p_up": 0, "p_down": 0}) in calls
