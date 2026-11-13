"""Password encryption shared by publish.py (on the Mac) and build_static.py (on GitHub).

AES-256-GCM with a key from PBKDF2-SHA256(password, random salt). The same format is
decrypted in the browser with WebCrypto (see dashboard.html, STATIC mode).
"""
import base64
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITERATIONS = 310_000


def _key(password, salt, iterations):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(password.encode("utf-8"))


def encrypt(data: bytes, password: str) -> bytes:
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(password, salt, ITERATIONS)).encrypt(iv, data, None)
    b64 = lambda b: base64.b64encode(b).decode()
    return json.dumps({"v": 1, "iter": ITERATIONS, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}).encode()


def decrypt(blob: bytes, password: str) -> bytes:
    d = json.loads(blob)
    b = lambda k: base64.b64decode(d[k])
    return AESGCM(_key(password, b("salt"), d["iter"])).decrypt(b("iv"), b("ct"), None)


# --------------------------------------------------------------------------- shared files (v2)
# One random data key per file, wrapped once per allowed password ("slots"). A password's
# key-encryption key is derived once per build (one salt per password), so the browser only
# runs PBKDF2 once per password even when it opens several files.

def make_kek(password):
    salt = os.urandom(16)
    return {"salt": salt, "iter": ITERATIONS, "key": _key(password, salt, ITERATIONS)}


def encrypt_for(data: bytes, keks) -> bytes:
    b64 = lambda b: base64.b64encode(b).decode()
    dkey, iv = AESGCM.generate_key(256), os.urandom(12)
    slots = []
    for k in keks:
        siv = os.urandom(12)
        slots.append({"salt": b64(k["salt"]), "iter": k["iter"], "iv": b64(siv),
                      "ct": b64(AESGCM(k["key"]).encrypt(siv, dkey, None))})
    return json.dumps({"v": 2, "slots": slots, "iv": b64(iv),
                       "ct": b64(AESGCM(dkey).encrypt(iv, data, None))}).encode()


def decrypt_for(blob: bytes, password: str) -> bytes:
    d = json.loads(blob)
    b = base64.b64decode
    for s in d["slots"]:
        try:
            dkey = AESGCM(_key(password, b(s["salt"]), s["iter"])).decrypt(b(s["iv"]), b(s["ct"]), None)
        except Exception:
            continue
        return AESGCM(dkey).decrypt(b(d["iv"]), b(d["ct"]), None)
    raise ValueError("wrong password")
