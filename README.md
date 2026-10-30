# Workshop Bot Gallery

Streamlit app where workshop participants register their LangSmith Fleet agents and chat with each other's.
Personal accounts and free tiers only. No Autodesk accounts, SSO, or infrastructure.

## Setup (use your personal accounts everywhere)

1. **GitHub (personal):** create a **private** repo and push this folder. `.streamlit/secrets.toml` is gitignored; never commit secrets.
2. **Supabase (personal):** create a free project. In the SQL editor, run `schema.sql`. From Project settings > API, copy the project URL and the **service_role** key.
3. **Encryption key:** `python -c "from security import generate_encryption_key as g; print(g())"`. Keep a backup. Losing it makes saved tokens unreadable.
4. **Local run:**
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # fill in values
   streamlit run app.py
   ```
5. **Deploy:** sign in to [Streamlit Community Cloud](https://share.streamlit.io) with your **personal** GitHub/Google account, create an app from the repo (`app.py`), and paste the contents of `secrets.toml` under Advanced settings > Secrets. Set `APP_URL` to the deployed URL.

## Using it

- **Participants:** Add your bot, test the connection, accept the acknowledgment, publish, and **save the management link** (shown once). It's the only way to edit, replace an expired token, or delete.
- **Organizer:** open `/?admin=1` and enter `ORGANIZER_ADMIN_SECRET`. You can pause all chatting, disable or remove bots, and delete everything after the workshop.

## Tests

```bash
pytest
```
Covers encryption, URL/SSRF checks, owner authorization, credential isolation, thread isolation, rate limits, and Fleet response parsing (mocked; no network).

## Security notes

- PATs are Fernet-encrypted at rest; the key lives only in Streamlit Secrets. The app server must decrypt a PAT to call Fleet, so anyone with access to the app secrets can read all tokens.
- Only `https://*.langgraph.app` hosts that resolve to public IPs are accepted (also re-checked on every call).
- Public queries select explicit columns and never return `encrypted_pat` or the management hash. RLS is enabled on both tables with no policies, so only the service-role key works.
- Rate limits (10 messages/min per visitor, 30/min per bot, 3 concurrent per bot) are in-process and reset on restart. Streamlit Community Cloud runs one process, so this holds.
- Chat transcripts are never stored. Fleet keeps thread history on the agent owner's account.
- Management links contain the secret in the URL. The app strips it from the address bar on load, but treat the link as a password.
- Use workshop-safe agents on personal or trial LangSmith accounts, and tokens with short expiry. Use your own test PATs and synthetic data during development.

## Not yet verified

- Live Fleet calls (`assistants.get`, `threads.create`, `runs.wait`) and the Supabase queries have not been run against real services.
- The Fleet API URL host pattern (`*.langgraph.app`) comes from the Fleet docs. If your agent shows a different host, add its suffix to `ALLOWED_HOST_SUFFIXES` in `security.py`.
- Redirect handling relies on the SDK's HTTP client defaults. Check `langgraph_sdk.get_client` behavior before the workshop.
