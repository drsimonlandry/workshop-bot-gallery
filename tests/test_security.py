import pytest

import security as sec

PUBLIC = lambda host: ["93.184.216.34"]


def test_encrypt_roundtrip_and_not_plaintext():
    key = sec.generate_encryption_key()
    ct = sec.encrypt_secret("lsv2_pt_secret", key)
    assert "lsv2_pt_secret" not in ct
    assert sec.decrypt_secret(ct, key) == "lsv2_pt_secret"


def test_decrypt_with_wrong_key_fails_safely():
    ct = sec.encrypt_secret("lsv2_pt_secret", sec.generate_encryption_key())
    with pytest.raises(sec.SecurityError) as e:
        sec.decrypt_secret(ct, sec.generate_encryption_key())
    assert "lsv2" not in str(e.value)


def test_management_token_hash():
    t = sec.generate_management_token()
    h = sec.hash_token(t)
    assert t not in h and sec.verify_token(t, h)
    assert not sec.verify_token("wrong", h)
    assert not sec.verify_token("", h)


@pytest.mark.parametrize("url", [
    "http://a.us.langgraph.app",            # not https
    "https://evil.com",                      # not allowlisted
    "https://langgraph.app.evil.com",        # suffix trick
    "https://evillanggraph.app",             # no dot boundary
    "https://localhost",
    "https://127.0.0.1",
    "https://10.0.0.1",
    "https://user:pw@a.us.langgraph.app",    # userinfo
    "https://a.us.langgraph.app:8443",       # odd port
    "",
])
def test_rejects_bad_urls(url):
    with pytest.raises(sec.SecurityError):
        sec.validate_api_url(url, resolver=PUBLIC)


def test_accepts_fleet_url_and_normalizes():
    assert sec.validate_api_url("https://My-Agent.us.langgraph.app/", resolver=PUBLIC) == "https://my-agent.us.langgraph.app"


def test_rejects_allowlisted_host_resolving_to_private_ip():
    with pytest.raises(sec.SecurityError):
        sec.validate_api_url("https://a.us.langgraph.app", resolver=lambda h: ["169.254.169.254"])


def test_redact():
    assert "lsv2_pt_abc123" not in sec.redact("failed with key lsv2_pt_abc123")
    assert "xyz" not in sec.redact("X-Api-Key: xyz")


def test_message_validation():
    assert sec.validate_message("  hi ") == "hi"
    with pytest.raises(sec.SecurityError):
        sec.validate_message("   ")
    with pytest.raises(sec.SecurityError):
        sec.validate_message("x" * (sec.MAX_MESSAGE_CHARS + 1))


def test_rate_limits_and_concurrency():
    now = [0.0]
    lim = sec.UsageLimiter(per_visitor=2, per_bot=3, window_s=60, max_concurrent_per_bot=1, clock=lambda: now[0])
    with lim.acquire("v1", "b1"):
        with pytest.raises(sec.RateLimitError):  # concurrency
            with lim.acquire("v2", "b1"):
                pass
    with lim.acquire("v1", "b1"):
        pass
    with pytest.raises(sec.RateLimitError):  # per visitor
        with lim.acquire("v1", "b1"):
            pass
    with lim.acquire("v2", "b1"):
        pass
    with pytest.raises(sec.RateLimitError):  # per bot
        with lim.acquire("v3", "b1"):
            pass
    now[0] = 61
    with lim.acquire("v1", "b1"):  # window expired
        pass
