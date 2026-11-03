"""ইউজার সেটিংস, API key, চরিত্র - MongoDB তে সংরক্ষণ।"""
import os

from bson import ObjectId

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
_DEFAULTS = {"lang": "bn", "subtitles": False, "lipsync": False, "voice": "Kore", "auto": True}


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


# ───────── অ্যাডমিন-নিয়ন্ত্রিত API key (পুরো বটের জন্য, একাধিক key, একটিই ON) ─────────
_keys = db["ai_api_keys"]


async def add_api_key(provider: str, key: str, by: int) -> str:
    """নতুন key যোগ করে। প্রথম key নিজে থেকে ON হয়, পরেরগুলো OFF।"""
    first = await _keys.count_documents({"provider": provider, "active": True}) == 0
    r = await _keys.insert_one({"provider": provider, "key": _enc(key), "last4": key[-4:],
                                "active": first, "by": by})
    return str(r.inserted_id)


async def list_api_keys(provider: str) -> list:
    out = []
    async for d in _keys.find({"provider": provider}).sort("_id", 1):
        out.append({"id": str(d["_id"]), "last4": d.get("last4", "????"), "active": d.get("active", False)})
    return out


async def toggle_api_key(kid: str):
    """ON করলে একই প্রোভাইডারের বাকি সব OFF হয়ে যায় (একটিই কাজ করে)।"""
    d = await _keys.find_one({"_id": ObjectId(kid)})
    if not d:
        return
    if d.get("active"):
        await _keys.update_one({"_id": d["_id"]}, {"$set": {"active": False}})
    else:
        await _keys.update_many({"provider": d["provider"]}, {"$set": {"active": False}})
        await _keys.update_one({"_id": d["_id"]}, {"$set": {"active": True}})


async def delete_api_key(kid: str):
    await _keys.delete_one({"_id": ObjectId(kid)})


async def get_key(uid: int, provider: str):
    """যে key ON আছে সেটি ফেরত দেয় (uid শুধু পুরনো কোডের সাথে মিল রাখতে)।"""
    d = await _keys.find_one({"provider": provider, "active": True})
    return _dec(d["key"]) if d else None


async def has_keys(uid: int = 0) -> dict:
    return {p: await _keys.count_documents({"provider": p, "active": True}) > 0 for p in PROVIDERS}


async def add_char(uid: int, name: str, desc: str, photo: str | None, voice: str, gender: str = ""):
    u = await get_user(uid)
    chars = [c for c in u["chars"] if c["name"].lower() != name.lower()]
    chars.append({"name": name, "desc": desc, "photo": photo, "voice": voice, "gender": gender})
    await set_field(uid, chars=chars)


async def del_char(uid: int, idx: int):
    u = await get_user(uid)
    if 0 <= idx < len(u["chars"]):
        u["chars"].pop(idx)
        await set_field(uid, chars=u["chars"])


def voice_for_gender(gender: str, chars: list) -> str:
    sym = "♀" if str(gender).lower().startswith("f") else "♂"
    pool = [v for v, g in VOICES if g == sym]
    used = {c["voice"] for c in chars}
    return ([v for v in pool if v not in used] or pool)[0]


async def set_char_voice(uid: int, idx: int, voice: str):
    u = await get_user(uid)
    if 0 <= idx < len(u["chars"]):
        u["chars"][idx]["voice"] = voice
        await set_field(uid, chars=u["chars"])
