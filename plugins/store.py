"""ইউজার সেটিংস, API key, চরিত্র - MongoDB তে সংরক্ষণ।"""
import os

from plugins.database import db

try:
    from cryptography.fernet import Fernet
except ImportError:  # pragma: no cover
    Fernet = None

_ENC = os.environ.get("ENCRYPT_KEY", "").strip()
_fernet = Fernet(_ENC.encode()) if (Fernet and _ENC) else None

PROVIDERS = {
    "gemini": "Gemini (টেক্সট • ভয়েস • ভিডিও)",
    "replicate": "Replicate (লিপ-সিঙ্ক)",
}
LANGS = {"bn": "Bengali", "hi": "Hindi"}
LANG_LABEL = {"bn": "বাংলা", "hi": "হিন্দি"}
VOICES = [
    ("Kore", "♀"), ("Aoede", "♀"), ("Leda", "♀"), ("Zephyr", "♀"),
    ("Puck", "♂"), ("Charon", "♂"), ("Fenrir", "♂"), ("Orus", "♂"),
]
VOICE_NAMES = [v for v, _ in VOICES]

_users = db["ai_users"]
_DEFAULTS = {"lang": "bn", "subtitles": False, "lipsync": False, "voice": "Kore"}


def _enc(s: str) -> str:
    return "f:" + _fernet.encrypt(s.encode()).decode() if _fernet else "p:" + s


def _dec(s: str):
    try:
        if s.startswith("f:"):
            return _fernet.decrypt(s[2:].encode()).decode() if _fernet else None
        return s[2:]
    except Exception:
        return None


async def get_user(uid: int) -> dict:
    d = await _users.find_one({"_id": uid}) or {}
    out = {**_DEFAULTS, **d}
    out["chars"] = list(d.get("chars") or [])
    out["keys"] = dict(d.get("keys") or {})
    return out


async def set_field(uid: int, **kw):
    await _users.update_one({"_id": uid}, {"$set": kw}, upsert=True)


async def set_key(uid: int, provider: str, key: str):
    await _users.update_one({"_id": uid}, {"$set": {f"keys.{provider}": _enc(key)}}, upsert=True)


async def del_key(uid: int, provider: str):
    await _users.update_one({"_id": uid}, {"$unset": {f"keys.{provider}": ""}})


async def get_key(uid: int, provider: str):
    u = await get_user(uid)
    raw = u["keys"].get(provider)
    return _dec(raw) if raw else None


async def has_keys(uid: int) -> dict:
    u = await get_user(uid)
    return {p: p in u["keys"] for p in PROVIDERS}


async def add_char(uid: int, name: str, desc: str, photo: str | None, voice: str):
    u = await get_user(uid)
    chars = [c for c in u["chars"] if c["name"].lower() != name.lower()]
    chars.append({"name": name, "desc": desc, "photo": photo, "voice": voice})
    await set_field(uid, chars=chars)


async def del_char(uid: int, idx: int):
    u = await get_user(uid)
    if 0 <= idx < len(u["chars"]):
        u["chars"].pop(idx)
        await set_field(uid, chars=u["chars"])


async def set_char_voice(uid: int, idx: int, voice: str):
    u = await get_user(uid)
    if 0 <= idx < len(u["chars"]):
        u["chars"][idx]["voice"] = voice
        await set_field(uid, chars=u["chars"])
