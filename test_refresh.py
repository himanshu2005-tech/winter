import os
import sys

base_dir = os.path.dirname(os.path.abspath(__file__))
if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

from providers.google_api import get_credentials
from google.auth.transport.requests import Request

print("--- Testing Keyring Token Read ---")
# If token.json is read, we will see it because it prints "Falling back to token.json" in the patched code.
# I want to rename token.json temporarily so it is forced to use Keyring!

token_path = os.path.join(base_dir, 'token.json')
temp_token_path = os.path.join(base_dir, 'token.json.tmp')
if os.path.exists(token_path):
    os.rename(token_path, temp_token_path)

try:
    creds = get_credentials(base_dir)
    if creds and creds.valid:
        print("Success! Read valid token from Keyring.")
    else:
        print("Token is expired or invalid. Attempting refresh...")
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            print("Successfully refreshed token using Keyring!")
        else:
            print("Failed to refresh token.")
except Exception as e:
    print(f"Error during Keyring OAuth test: {e}")

finally:
    # Restore token.json for safety until user deletes it
    if os.path.exists(temp_token_path):
        os.rename(temp_token_path, token_path)
