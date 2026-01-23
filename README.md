# YouTube CMS Revenue → Google Sheets (Streamlit)

This app:
- queries **monthly estimatedRevenue** for 3 YouTube Analytics **groups** (Total + US-only)
- writes the values into your Google Sheet under the headers:
  - `<Group Name>` and `<Group Name> US`

## Why these scopes?
Revenue requires the YouTube Analytics *monetary* scope:
- https://www.googleapis.com/auth/yt-analytics-monetary.readonly

(See YouTube Analytics channel/content owner reports docs.)

## Setup (local)

1) Create a Python venv and install deps:
```bash
python -m venv .venv
source .venv/bin/activate   # mac/linux
# .venv\Scripts\activate    # windows
pip install -r requirements.txt
```

2) Google Cloud:
- Enable **YouTube Analytics API** and **Google Sheets API**
- Create an OAuth client (Web app is fine; for local token script you can also use Desktop)
- Download the OAuth client JSON as `oauth_client.json` in the repo root

3) Get a refresh token (one-time):
```bash
python scripts/get_refresh_token.py
```
Copy the printed refresh_token.

4) Service Account for Sheets:
- Create a service account key JSON
- Share your Google Sheet with the service account `client_email` (Editor)

5) Add secrets:
- Create `.streamlit/secrets.toml` and paste from `.streamlit/secrets.toml.example`, filling all fields.

6) Run the app:
```bash
streamlit run app.py
```

## Deploy (Streamlit Community Cloud)
- Push repo to GitHub
- Create a new Streamlit app from the repo
- In the Streamlit Cloud UI: **Settings → Secrets**
  - paste the contents of your secrets.toml there
