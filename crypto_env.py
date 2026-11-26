import os
import base64

SECRET = "WINTER_SECURE_2026_DEPLOYMENT_KEY_XYZ"

def xor_crypt(text, key):
    return "".join(chr(ord(c) ^ ord(key[i % len(key)])) for i, c in enumerate(text))

def encrypt_env(env_path, out_path):
    if not os.path.exists(env_path): return
    with open(env_path, "r", encoding="utf-8") as f:
        data = f.read()
    encrypted = xor_crypt(data, SECRET)
    encoded = base64.b64encode(encrypted.encode("utf-8")).decode("utf-8")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(encoded)

def load_encrypted_env(out_path):
    if not os.path.exists(out_path): return
    try:
        with open(out_path, "r", encoding="utf-8") as f:
            encoded = f.read()
        if not encoded: return
        encrypted = base64.b64decode(encoded).decode("utf-8")
        data = xor_crypt(encrypted, SECRET)
        for line in data.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"): continue
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"')
    except Exception as e:
        print(f"Failed to load secure config: {e}")

def update_encrypted_env_key(out_path, target_key, new_val):
    if not os.path.exists(out_path): return False
    try:
        with open(out_path, "r", encoding="utf-8") as f:
            encoded = f.read()
        encrypted = base64.b64decode(encoded).decode("utf-8")
        data = xor_crypt(encrypted, SECRET)
        
        lines = data.split("\n")
        found = False
        for i in range(len(lines)):
            if lines[i].startswith(f"{target_key}="):
                val = f'"{new_val}"' if "PASSWORD" in target_key else new_val
                lines[i] = f"{target_key}={val}"
                found = True
                break
        
        if not found:
            val = f'"{new_val}"' if "PASSWORD" in target_key else new_val
            lines.append(f"{target_key}={val}")
            
        new_data = "\n".join(lines)
        new_enc = xor_crypt(new_data, SECRET)
        new_b64 = base64.b64encode(new_enc.encode("utf-8")).decode("utf-8")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(new_b64)
        return True
    except Exception as e:
        print(f"Failed to update secure config: {e}")
        return False
