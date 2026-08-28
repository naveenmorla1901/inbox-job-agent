"""One-time OAuth: opens a browser, writes secrets/token.json.

Google Cloud Console -> APIs & Services:
  1. Enable the Gmail API.
  2. OAuth consent screen -> External -> add your own address as a test user.
  3. Credentials -> Create OAuth client ID -> Desktop app -> download JSON.
  4. Save it as secrets/client_secret.json, then run: python -m app.auth_setup
"""

from __future__ import annotations

from google_auth_oauthlib.flow import InstalledAppFlow

from .config import get_settings
from .gmail_client import scopes_for


def main() -> None:
    settings = get_settings()
    secrets_path = settings.path(settings.google_client_secrets)
    if not secrets_path.exists():
        raise SystemExit(f"Missing OAuth client secrets at {secrets_path}. See module docstring.")

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), scopes_for(settings))
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    token_path = settings.path(settings.gmail_token_file)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"Saved token to {token_path}")
    print("For hosted runs, put the contents of that file into the GMAIL_TOKEN_JSON secret.")


if __name__ == "__main__":
    main()
