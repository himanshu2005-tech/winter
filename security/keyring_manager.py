import keyring
import math
import json

CHUNK_SIZE = 500  # Safe size for Windows Credential Manager payloads

def set_secret_chunked(service_name, key_name, secret_value):
    """
    Saves a secret into the keyring. If the secret exceeds CHUNK_SIZE,
    it automatically splits it into multiple chunks to evade Credential Manager limits.
    """
    # First, clear existing chunks if any
    delete_secret_chunked(service_name, key_name)
    
    chunks = [secret_value[i:i+CHUNK_SIZE] for i in range(0, len(secret_value), CHUNK_SIZE)]
    
    # Store chunk metadata (number of chunks)
    meta = {"chunks": len(chunks)}
    keyring.set_password(service_name, f"{key_name}_meta", json.dumps(meta))
    
    for idx, chunk in enumerate(chunks):
        keyring.set_password(service_name, f"{key_name}_part{idx}", chunk)

def get_secret_chunked(service_name, key_name):
    """
    Retrieves a chunked secret from the keyring and reassembles it.
    """
    meta_raw = keyring.get_password(service_name, f"{key_name}_meta")
    if not meta_raw:
        # Fallback for non-chunked legacy secrets if any
        return keyring.get_password(service_name, key_name)
        
    try:
        meta = json.loads(meta_raw)
        num_chunks = meta.get("chunks", 0)
    except Exception:
        return None
        
    secret_value = ""
    for idx in range(num_chunks):
        chunk = keyring.get_password(service_name, f"{key_name}_part{idx}")
        if chunk is None:
            return None  # Missing chunk, secret corrupted
        secret_value += chunk
        
    return secret_value

def delete_secret_chunked(service_name, key_name):
    """
    Deletes the metadata and all chunks of a secret from the keyring.
    """
    meta_raw = keyring.get_password(service_name, f"{key_name}_meta")
    if meta_raw:
        try:
            meta = json.loads(meta_raw)
            num_chunks = meta.get("chunks", 0)
            for idx in range(num_chunks):
                try:
                    keyring.delete_password(service_name, f"{key_name}_part{idx}")
                except keyring.errors.PasswordDeleteError:
                    pass
            try:
                keyring.delete_password(service_name, f"{key_name}_meta")
            except keyring.errors.PasswordDeleteError:
                pass
        except Exception:
            pass
    else:
        try:
            keyring.delete_password(service_name, key_name)
        except keyring.errors.PasswordDeleteError:
            pass
