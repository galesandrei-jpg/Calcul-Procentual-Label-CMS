"""
One-time helper to obtain a refresh_token for the YouTube Analytics API.

Run locally:
  python scripts/get_refresh_token.py

It will open a browser window. After you approve access, it prints a refresh_token.
Put that token into Streamlit secrets under [youtube_oauth].refresh_token.

Docs note: monetary revenue requires yt-analytics-monetary.readonly scope.
"""
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/yt-analytics-monetary.readonly"]

def main():
    flow = InstalledAppFlow.from_client_secrets_file("oauth_client.json", SCOPES)
    creds = flow.run_local_server(port=8080, prompt="consent")
    print("\n✅ refresh_token:")
    print(creds.refresh_token)

if __name__ == "__main__":
    main()
