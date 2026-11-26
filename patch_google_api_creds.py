with open('d:\\winter\\providers\\google_api.py', 'r', encoding='utf-8') as f:
    content = f.read()

target = '''def get_credentials(base_dir):
    creds = None
    token_path = os.path.join(base_dir, 'token.json')
    creds_path = os.path.join(base_dir, 'credentials.json')

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(creds_path):
                raise FileNotFoundError(f"Missing {creds_path}. Please place credentials.json in {base_dir}")
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        
        with open(token_path, 'w') as token:
            token.write(creds.to_json())
    return creds'''

injection = '''USE_KEYRING_FALLBACK = True

def get_credentials(base_dir):
    creds = None
    token_path = os.path.join(base_dir, 'token.json')
    creds_path = os.path.join(base_dir, 'credentials.json')

    # 1. Try Keyring First
    try:
        import sys
        if base_dir not in sys.path:
            sys.path.insert(0, base_dir)
        from security.keyring_manager import get_secret_chunked, set_secret_chunked
        keyring_token = get_secret_chunked("WINTER_SYSTEM", "google_oauth_token")
        if keyring_token:
            import json
            creds = Credentials.from_authorized_user_info(json.loads(keyring_token), SCOPES)
    except Exception as e:
        print(f"[OAuth] Keyring read failed: {e}")

    # 2. Fallback to token.json if allowed
    if not creds and USE_KEYRING_FALLBACK and os.path.exists(token_path):
        print("[OAuth] Falling back to token.json")
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    
    # 3. Refresh or Auth
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # For new auth, we still need credentials.json
            if not os.path.exists(creds_path):
                raise FileNotFoundError(f"Missing {creds_path}. Please place credentials.json in {base_dir}")
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        
        # Save updated token strictly to Keyring
        try:
            from security.keyring_manager import set_secret_chunked
            set_secret_chunked("WINTER_SYSTEM", "google_oauth_token", creds.to_json())
            print("[OAuth] Successfully saved refreshed token to Windows Credential Manager.")
        except Exception as e:
            print(f"[OAuth] Failed to write to Keyring: {e}")
            if USE_KEYRING_FALLBACK:
                with open(token_path, 'w') as token:
                    token.write(creds.to_json())
    return creds'''

if target in content:
    content = content.replace(target, injection)
    with open('d:\\winter\\providers\\google_api.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print('Patched get_credentials in google_api.py')
else:
    print('Target block not found!')
