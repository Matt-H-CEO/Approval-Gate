"""Time-lock for the final judging set. Standard library only.

The final set ships inside the repository from day one, encrypted. Organizers release the passphrase in the last 30
minutes. Format: PBKDF2-HMAC-SHA256 -> (encryption key, MAC key); HMAC-SHA256 in counter mode as the keystream;
encrypt-then-MAC. This keeps the set unreadable until release; it is not a defence against someone who already knows
the passphrase.
"""
import base64
import hashlib
import hmac
import json
import os

VERSION = 1
ITERATIONS = 600_000


class SealError(Exception):
    pass


def _keys(passphrase, salt, iterations):
    material = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, iterations, dklen=64)
    return material[:32], material[32:]


def _xor_stream(key, nonce, data):
    out = bytearray(len(data))
    for block, start in enumerate(range(0, len(data), 32)):
        pad = hmac.new(key, nonce + block.to_bytes(8, "big"), hashlib.sha256).digest()
        chunk = data[start:start + 32]
        out[start:start + len(chunk)] = bytes(a ^ b for a, b in zip(chunk, pad))
    return bytes(out)


def _tag(mac_key, salt, nonce, iterations, ciphertext):
    header = f"{VERSION}|{iterations}|".encode() + salt + nonce
    return hmac.new(mac_key, header + ciphertext, hashlib.sha256).digest()


def seal(plaintext, passphrase, iterations=ITERATIONS):
    salt, nonce = os.urandom(16), os.urandom(16)
    enc_key, mac_key = _keys(passphrase, salt, iterations)
    ciphertext = _xor_stream(enc_key, nonce, plaintext)
    b64 = lambda raw: base64.b64encode(raw).decode()
    return {"version": VERSION, "kdf": "pbkdf2-sha256", "iterations": iterations, "salt": b64(salt),
            "nonce": b64(nonce), "ciphertext": b64(ciphertext),
            "mac": b64(_tag(mac_key, salt, nonce, iterations, ciphertext))}


def unseal(envelope, passphrase):
    try:
        if envelope["version"] != VERSION or envelope["kdf"] != "pbkdf2-sha256":
            raise SealError("Unsupported sealed-set format")
        iterations = int(envelope["iterations"])
        salt, nonce, ciphertext, mac = (base64.b64decode(envelope[k]) for k in ("salt", "nonce", "ciphertext", "mac"))
    except (KeyError, TypeError, ValueError) as e:
        raise SealError("The sealed set file is damaged") from e
    if not 1 <= iterations <= 10_000_000:
        raise SealError("The sealed set file is damaged")
    enc_key, mac_key = _keys(passphrase.strip(), salt, iterations)
    if not hmac.compare_digest(mac, _tag(mac_key, salt, nonce, iterations, ciphertext)):
        raise SealError("Wrong passphrase, or the sealed set file was modified")
    return _xor_stream(enc_key, nonce, ciphertext)


def seal_json(data, passphrase, iterations=ITERATIONS):
    return seal(json.dumps(data, sort_keys=True).encode(), passphrase, iterations)


def unseal_json(envelope, passphrase):
    return json.loads(unseal(envelope, passphrase))
