"""
Run this ONCE on your own computer to authorize uploads to your YouTube channel.

  1. pip install google-auth-oauthlib
  2. Put the client_secret.json you downloaded from Google Cloud next to this file
  3. python get_youtube_token.py
  4. A browser opens - sign in with the Google account that owns the channel
     (if you manage several channels, pick the right one)
  5. Copy the three values it prints into your GitHub repo secrets
"""

import json
from google_auth_oauthlib.flow import InstalledAppFlow

flow = InstalledAppFlow.from_client_secrets_file(
    "client_secret.json",
    scopes=["https://www.googleapis.com/auth/youtube.upload"],
)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

with open("client_secret.json") as f:
    info = json.load(f)
    info = info.get("installed") or info.get("web")

print("\nAdd these as GitHub secrets:\n")
print(f"YT_CLIENT_ID      = {info['client_id']}")
print(f"YT_CLIENT_SECRET  = {info['client_secret']}")
print(f"YT_REFRESH_TOKEN  = {creds.refresh_token}")
