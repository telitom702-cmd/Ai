"""Gemini (টেক্সট/ভয়েস/ভিডিও) ও Replicate (লিপ-সিঙ্ক) API ক্লায়েন্ট।"""
import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path

import aiohttp

GEMINI = "https://generativelanguage.googleapis.com/v1beta"
TEXT_MODEL = os.environ.get("GEMINI_TEXT_MODEL", "").strip()
TTS_MODEL = os.environ.get("GEMINI_TTS_MODEL", "").strip()
VEO_MODEL = os.environ.get("VEO_MODEL", "").strip()
LIPSYNC_MODEL = os.environ.get("LIPSYNC_MODEL", "sync/lipsync-2")
LS_VIDEO = os.environ.get("LIPSYNC_VIDEO_FIELD", "video")
LS_AUDIO = os.environ.get("LIPSYNC_AUDIO_FIELD", "audio")
REPLICATE = "https://api.replicate.com/v1"


class AIError(Exception):
    pass


async def _req(method, url, *, headers=None, body=None, data=None, timeout=300, raw=False):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.request(method, url, headers=headers, json=body, data=data) as r:
            if raw:
                if r.status >= 400:
                    raise AIError(f"HTTP {r.status}")
                return await r.read()
            try:
                js = await r.json(content_type=None)
            except Exception:
                js = {"error": {"message": (await r.text())[:200]}}
            if r.status >= 400:
                msg = js.get("error", {}) if isinstance(js, dict) else {}
                msg = msg.get("message", msg) if isinstance(msg, dict) else msg
                if not msg and isinstance(js, dict):
                    msg = js.get("detail", "")
                raise AIError(f"HTTP {r.status}: {str(msg)[:200]}")
            return js


def _gh(key):
    return {"x-goog-api-key": key}


def _json(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"[\[{]", text)
    if not m:
        raise AIError("AI থেকে সঠিক JSON আসেনি")
    try:
        return json.JSONDecoder().raw_decode(text[m.start():])[0]
    except json.JSONDecodeError as e:
        raise AIError(f"JSON পার্স হয়নি: {e}")


# ───────────── মডেল নিজে খুঁজে নেওয়া ─────────────
_MODELS: dict = {}  # key -> (time, [(name, methods)])
_FALLBACK = {"text": "gemini-3.8-flash", "tts": "gemini-2.5-flash-preview-tts", "veo": "veo-3.0-generate-001"}
_SKIP = ("tts", "image", "live", "audio", "embedding", "robotics", "computer", "exp", "imagen", "veo", "aqa")


def _ver(name: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else 0.0


async def _list_models(key):
    c = _MODELS.get(key)
    if c and time.monotonic() - c[0] < 3600:
        return c[1]
    out, token = [], None
    for _ in range(5):
        url = f"{GEMINI}/models?pageSize=200" + (f"&pageToken={token}" if token else "")
        d = await _req("GET", url, headers=_gh(key), timeout=30)
        out += [(m["name"].split("/", 1)[-1], m.get("supportedGenerationMethods", [])) for m in d.get("models", [])]
        token = d.get("nextPageToken")
        if not token:
            break
    _MODELS[key] = (time.monotonic(), out)
    return out


async def _pick(key, kind: str) -> str:
    env = {"text": TEXT_MODEL, "tts": TTS_MODEL, "veo": VEO_MODEL}[kind]
    if env:
        return env
    try:
        models = await _list_models(key)
    except Exception:
        return _FALLBACK[kind]
    if kind == "text":
        c = [(n, m) for n, m in models if n.startswith("gemini-") and "flash" in n and "generateContent" in m
             and not any(x in n for x in _SKIP) and "lite" not in n]
        rank = lambda n: (_ver(n), "preview" not in n)
    elif kind == "tts":
        c = [(n, m) for n, m in models if "tts" in n and "generateContent" in m]
        rank = lambda n: (_ver(n), "flash" in n, "preview" not in n)
    else:
        c = [(n, m) for n, m in models if n.startswith("veo") and "predictLongRunning" in m]
        rank = lambda n: (_ver(n), "fast" in n or "lite" in n)  # একই ভার্সনে সস্তা ভ্যারিয়েন্ট
    return max((n for n, _ in c), key=rank) if c else _FALLBACK[kind]


# ───────────── key যাচাই ─────────────
async def check_gemini(key: str) -> bool:
    try:
        await _req("GET", f"{GEMINI}/models?pageSize=1", headers=_gh(key), timeout=20)
        return True
    except Exception:
        return False


async def check_replicate(token: str) -> bool:
    try:
        await _req("GET", f"{REPLICATE}/account", headers={"Authorization": f"Bearer {token}"}, timeout=20)
        return True
    except Exception:
        return False


# ───────────── টেক্সট ─────────────
async def gemini_text(key, prompt, extra_parts=None, as_json=False):
    parts = [{"text": prompt}] + (extra_parts or [])
    body = {"contents": [{"parts": parts}]}
    if as_json:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    d = await _req("POST", f"{GEMINI}/models/{await _pick(key, 'text')}:generateContent",
                   headers=_gh(key), body=body, timeout=180)
    try:
        return d["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise AIError("Gemini কোনো উত্তর দেয়নি (কনটেন্ট ফিল্টার হতে পারে)")


async def understand(key, text: str, voices) -> dict:
    prompt = (
        "You are the intent router of a Telegram bot for AI video creation, AI voice and video dubbing. "
        "Read the user's message (Bengali/Hindi/English) and reply ONLY with JSON "
        '{"action": <one of create_video, speak, add_character, set_voice, set_language, subtitles, lipsync, dub, api, chat>, "args": {...}}.\n'
        "- create_video: args.prompt (full scene text)\n- speak: args.text\n"
        "- add_character: args.name, args.description\n"
        f"- set_voice: args.character, args.voice (one of {list(voices)})\n"
        '- set_language: args.language ("bn"|"hi")\n- subtitles / lipsync: args.on (true|false)\n'
        '- dub: args.language ("bn"|"hi")\n- api: args.provider ("gemini"|"replicate"), args.op ("add"|"delete")\n'
        "- chat: args.reply (short helpful reply in the user's language)\n\n"
        f"User message:\n{text}"
    )
    d = _json(await gemini_text(key, prompt, as_json=True))
    return d if isinstance(d, dict) else {"action": "chat", "args": {"reply": "বুঝতে পারিনি।"}}


async def plan_story(key, text: str, lang_name: str, saved: list, max_scenes: int) -> dict:
    """গল্প থেকে চরিত্র + দৃশ্য + সংলাপের পরিকল্পনা বানায়।"""
    known = [{"name": c["name"], "description": c.get("desc", "")} for c in saved]
    prompt = (
        "You are a film director's assistant. Turn the user's story/idea into a plan for short AI video clips "
        f"(each clip is about 8 seconds). Return ONLY JSON of this shape:\n"
        '{"characters":[{"name":str,"gender":"male"|"female","description":str}],'
        '"scenes":[{"visual":str,"continuous":bool,"dialogue":[{"speaker":str,"text":str}]}]}\n'
        f"Rules:\n- At most {max_scenes} scenes, in story order. If the story is long, compress it into the key moments.\n"
        "- 'visual': one cinematic English paragraph (setting, action, camera, lighting). No text/subtitles on screen.\n"
        "- characters: every person/animal who appears. 'description' in English: age, face, hair, skin tone, clothes - "
        "detailed and fixed so the character looks the same in all scenes. Use the character's name exactly as in the story.\n"
        f"- dialogue: spoken lines in natural spoken {lang_name}, at most ~20 words per scene in total. "
        "Use speaker \"\" for a narrator. If the story has no dialogue, write short fitting dialogue or narration. "
        "Keep any dialogue the user wrote.\n"
        "- continuous: true only if the scene continues in the same place right after the previous scene.\n"
        f"- Already saved characters (reuse these exact names, do not redefine them): {json.dumps(known, ensure_ascii=False)}\n\n"
        f"Story:\n{text}"
    )
    d = _json(await gemini_text(key, prompt, as_json=True))
    if not isinstance(d, dict) or not d.get("scenes"):
        raise AIError("গল্প থেকে দৃশ্য বের করা যায়নি, আরেকটু বিস্তারিত লিখুন")
    d["scenes"] = d["scenes"][:max_scenes]
    d["characters"] = [c for c in d.get("characters", []) if isinstance(c, dict) and c.get("name")]
    return d


async def describe_image(key, img: bytes, mime="image/jpeg") -> str:
    part = {"inline_data": {"mime_type": mime, "data": base64.b64encode(img).decode()}}
    return (await gemini_text(
        key,
        "Describe this person/character's appearance in one dense English sentence for a video generator "
        "(face, age, hair, skin tone, clothing, distinctive features). No preamble.",
        [part])).strip()


async def transcribe(key, audio: bytes, mime="audio/mp3") -> list:
    part = {"inline_data": {"mime_type": mime, "data": base64.b64encode(audio).decode()}}
    prompt = (
        "Transcribe all speech in this audio. Return ONLY a JSON array of objects "
        '{"start": seconds(float), "end": seconds(float), "speaker": "S1"|"S2"|..., "text": "..."}. '
        "Split into natural sentences/phrases (max ~12 seconds each), keep the original language, "
        "ignore music and noise. Return [] if no speech."
    )
    d = _json(await gemini_text(key, prompt, [part], as_json=True))
    segs = []
    for x in d if isinstance(d, list) else []:
        try:
            s, e = float(x["start"]), float(x["end"])
            t = str(x["text"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if t and e > s:
            segs.append({"start": s, "end": e, "speaker": str(x.get("speaker", "S1")), "text": t})
    segs.sort(key=lambda z: z["start"])
    return segs


async def translate_lines(key, lines: list, lang_name: str) -> list:
    out = []
    for i in range(0, len(lines), 40):
        chunk = lines[i:i + 40]
        prompt = (
            f"Translate every item of this JSON array into natural spoken {lang_name}. "
            f"Keep the meaning, keep length similar (it will be dubbed). If an item is already in {lang_name}, "
            f"keep it unchanged. Return ONLY a JSON array of strings with exactly {len(chunk)} items.\n"
            + json.dumps(chunk, ensure_ascii=False)
        )
        res = _json(await gemini_text(key, prompt, as_json=True))
        if not isinstance(res, list) or len(res) != len(chunk):
            raise AIError("অনুবাদের লাইন সংখ্যা মেলেনি, আবার চেষ্টা করুন")
        out += [str(x) for x in res]
    return out


# ───────────── ভয়েস ─────────────
async def tts(key, text: str, voice: str) -> bytes:
    """PCM s16le 24kHz mono ফেরত দেয়।"""
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }
    last = None
    for attempt in range(3):
        try:
            d = await _req("POST", f"{GEMINI}/models/{await _pick(key, 'tts')}:generateContent",
                           headers=_gh(key), body=body, timeout=120)
            return base64.b64decode(d["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
        except (KeyError, IndexError, AIError) as e:
            last = e
            await asyncio.sleep(2 * (attempt + 1))
    raise AIError(f"ভয়েস তৈরি ব্যর্থ: {last}")


# ───────────── ভিডিও (Veo) ─────────────
async def veo_generate(key, prompt, image=None, mime="image/jpeg", aspect="16:9", tick=None) -> bytes:
    inst = {"prompt": prompt}
    if image:
        inst["image"] = {"bytesBase64Encoded": base64.b64encode(image).decode(), "mimeType": mime}
    op = await _req("POST", f"{GEMINI}/models/{await _pick(key, 'veo')}:predictLongRunning", headers=_gh(key),
                    body={"instances": [inst], "parameters": {"aspectRatio": aspect}}, timeout=60)
    name = op.get("name")
    if not name:
        raise AIError("ভিডিও জব শুরু হয়নি")
    t0 = time.monotonic()
    while True:
        await asyncio.sleep(8)
        el = time.monotonic() - t0
        if tick:
            await tick(el)
        if el > 720:
            raise AIError("ভিডিও জেনারেট করতে অতিরিক্ত সময় লাগছে")
        st = await _req("GET", f"{GEMINI}/{name}", headers=_gh(key), timeout=60)
        if not st.get("done"):
            continue
        if "error" in st:
            raise AIError(st["error"].get("message", "ভিডিও ব্যর্থ"))
        resp = st.get("response", {}).get("generateVideoResponse", {})
        samples = resp.get("generatedSamples")
        if not samples:
            reasons = resp.get("raiMediaFilteredReasons") or ["নিরাপত্তা ফিল্টার"]
            raise AIError(f"ভিডিও ব্লক হয়েছে: {reasons[0][:150]}")
        return await _req("GET", samples[0]["video"]["uri"], headers=_gh(key), timeout=300, raw=True)


# ───────────── লিপ-সিঙ্ক (Replicate) ─────────────
async def _upload(token, path) -> str:
    fd = aiohttp.FormData()
    fd.add_field("content", Path(path).read_bytes(), filename=os.path.basename(path),
                 content_type="application/octet-stream")
    d = await _req("POST", f"{REPLICATE}/files", headers={"Authorization": f"Bearer {token}"},
                   data=fd, timeout=300)
    return d["urls"]["get"]


async def lipsync(token, video_path, wav_path, tick=None) -> bytes:
    h = {"Authorization": f"Bearer {token}"}
    v, a = await _upload(token, video_path), await _upload(token, wav_path)
    pred = await _req("POST", f"{REPLICATE}/models/{LIPSYNC_MODEL}/predictions",
                      headers={**h, "Prefer": "wait=30"}, body={"input": {LS_VIDEO: v, LS_AUDIO: a}},
                      timeout=120)
    t0 = time.monotonic()
    while pred.get("status") in ("starting", "processing"):
        await asyncio.sleep(5)
        if tick:
            await tick(time.monotonic() - t0)
        if time.monotonic() - t0 > 900:
            raise AIError("লিপ-সিঙ্কে অতিরিক্ত সময় লাগছে")
        pred = await _req("GET", pred["urls"]["get"], headers=h, timeout=60)
    if pred.get("status") != "succeeded":
        raise AIError(f"লিপ-সিঙ্ক ব্যর্থ: {str(pred.get('error'))[:150]}")
    out = pred["output"]
    out = out[0] if isinstance(out, list) else out
    return await _req("GET", out, timeout=300, raw=True)
