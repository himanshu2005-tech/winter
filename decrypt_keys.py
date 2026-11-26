import sys
import base64

SECRET = "WINTER_SECURE_2026_DEPLOYMENT_KEY_XYZ"
def xor_crypt(text, key):
    return "".join(chr(ord(c) ^ ord(key[i % len(key)])) for i, c in enumerate(text))

out_path = "d:/winter/config.bin"
try:
    with open(out_path, "r", encoding="utf-8") as f:
        encoded = f.read()
    encrypted = base64.b64decode(encoded).decode("utf-8")
    data = xor_crypt(encrypted, SECRET)
    with open("d:/winter/.env", "w", encoding="utf-8") as f:
        f.write(data)
    print(".env created successfully!")
except Exception as e:
    print(f"Error: {e}")
