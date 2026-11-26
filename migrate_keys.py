import os
import sys

# Ensure d:\winter is in the path to import security module
base_dir = os.path.dirname(os.path.abspath(__file__))
if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

from security.keyring_manager import set_secret_chunked, get_secret_chunked

def migrate():
    token_path = os.path.join(base_dir, 'token.json')
    passwords_path = os.path.join(base_dir, 'vault', 'passwords.json')
    
    print("--- Starting Staged Keyring Migration ---")
    
    # 1. Migrate token.json
    if os.path.exists(token_path):
        print("Migrating token.json...")
        with open(token_path, 'r', encoding='utf-8') as f:
            token_data = f.read()
            
        set_secret_chunked("WINTER_SYSTEM", "google_oauth_token", token_data)
        
        # Verify read-back
        read_back = get_secret_chunked("WINTER_SYSTEM", "google_oauth_token")
        if read_back == token_data:
            print("  -> Google OAuth token safely stored in Windows Credential Manager and verified!")
        else:
            print("  -> ERROR: Token read-back verification failed!")
            
    # 2. Migrate passwords.json
    if os.path.exists(passwords_path):
        print("Migrating passwords.json...")
        with open(passwords_path, 'r', encoding='utf-8') as f:
            pass_data = f.read()
            
        set_secret_chunked("WINTER_SYSTEM", "user_passwords", pass_data)
        
        # Verify read-back
        read_back = get_secret_chunked("WINTER_SYSTEM", "user_passwords")
        if read_back == pass_data:
            print("  -> Passwords safely stored in Windows Credential Manager and verified!")
        else:
            print("  -> ERROR: Passwords read-back verification failed!")
            
    print("--- Migration Script Finished ---")
    print("Note: Flat files were NOT deleted per the staged migration policy.")

if __name__ == "__main__":
    migrate()
