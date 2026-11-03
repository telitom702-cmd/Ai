"""টেলিগ্রাম মেনু, বাটন ও মেসেজ হ্যান্ডলার।"""
import asyncio
import logging
import os
import re

from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery
from pyrogram.types import InlineKeyboardButton as B
from pyrogram.types import InlineKeyboardMarkup as M

from plugins import ai, media, pipelines, store
from plugins.progress import Progress

LOGGER = logging.getLogger(__name__)
STATE: dict[int, dict] = {}
BUSY: set[int] = set()
TASKS: set = set()
MAX_SEC = int(os.environ.get("MAX_DUB_SECONDS", "600"))
CANCEL = M([[B("❌ বাতিল", "m:main")]])
BACK = M([[B("⬅️ মেনু", "m:main")]])


def onoff(v):
    return "ON ✅" if v else "OFF ❌"


def spawn(coro):
    t = asyncio.create_task(coro)
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)


async def show(target, text, kb=None):
    """CallbackQuery হলে মেসেজ এডিট, নইলে নতুন মেসেজ।"""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb)
        except MessageNotModified:
            pass
    else:
        await target.reply_text(text, reply_markup=kb)


# ───────────── মেনু ─────────────
async def main_menu(uid):
    u = await store.get_user(uid)
    k = await store.has_keys(uid)
    txt = (
        "🤖 **AI ভিডিও তৈরি ও ডাবিং বট**\n\n"
        f"🔑 Gemini: {'✅' if k['gemini'] else '❌'}   Replicate: {'✅' if k['replicate'] else '❌'}\n"
        f"🌐 ভয়েস ভাষা: {store.LANG_LABEL[u['lang']]}\n"
        f"👤 চরিত্র: {len(u['chars'])} টি   🤖 অটো মোড: {onoff(u['auto'])}\n\n"
        "টেক্সট লিখে পাঠালে বট নিজেই বুঝে কাজ করবে, অথবা নিচের বাটন ব্যবহার করুন।"
    )
    kb = M([
        [B("🎬 ভিডিও তৈরি", "vid:new"), B("🎙 AI ভয়েস", "vc:menu")],
        [B("🇧🇩 বাংলা ডাবিং", "dub:bn"), B("🇮🇳 হিন্দি ডাবিং", "dub:hi")],
        [B(f"👄 লিপ-সিঙ্ক: {onoff(u['lipsync'])}", "ls:t"), B(f"📝 সাবটাইটেল: {onoff(u['subtitles'])}", "sb:t")],
        [B(f"🤖 অটো মোড: {onoff(u['auto'])}", "au:t"), B("🔑 API সেটিংস", "api:menu")],
    ])
    return txt, kb


async def api_menu(uid):
    k = await store.has_keys(uid)
    rows, lines = [], ["🔑 **API সেটিংস**\n"]
    for p, label in store.PROVIDERS.items():
        lines.append(f"{'✅' if k[p] else '❌'} {label}")
        if k[p]:
            rows.append([B(f"🔄 {p.title()} বদলান", f"api:add:{p}"), B(f"🗑 {p.title()} ডিলেট", f"api:del:{p}")])
        else:
            rows.append([B(f"➕ {p.title()} API যোগ করুন", f"api:add:{p}")])
    rows.append([B("⬅️ মেনু", "m:main")])
    lines.append("\nGemini বাধ্যতামূলক। Replicate শুধু লিপ-সিঙ্কের জন্য (ঐচ্ছিক)।")
    return "\n".join(lines), M(rows)


async def voice_menu(uid):
    u = await store.get_user(uid)
    rows = [[B(("✅ " if u["lang"] == c else "") + store.LANG_LABEL[c], f"vc:lang:{c}") for c in store.LANGS]]
    for i, c in enumerate(u["chars"]):
        rows.append([B(f"👤 {c['name']} — {c['voice']}", f"vc:ch:{i}")])
    rows.append([B("➕ নতুন চরিত্র", "vc:add"), B(f"🎚 ডিফল্ট: {u['voice']}", "vc:dv")])
    rows.append([B("🎙 ভয়েস তৈরি করুন", "vc:speak")])
    rows.append([B("⬅️ মেনু", "m:main")])
    txt = ("🎙 **AI ভয়েস**\n\nভাষা বেছে নিন, চরিত্র যোগ করুন এবং প্রতিটি চরিত্রের আলাদা ভয়েস দিন।\n"
           "একাধিক চরিত্রের জন্য লিখুন:\n`রহিম: কেমন আছো?`\n`করিম: ভালো আছি`")
    return txt, M(rows)


def voice_picker(prefix):
    rows, row = [], []
    for i, (v, g) in enumerate(store.VOICES):
        row.append(B(f"{g} {v}", f"vc:sv:{prefix}:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    rows.append([B("⬅️ ফিরে যান", "vc:menu")])
    return M(rows)


# ───────────── কাজ চালানো ─────────────
async def run_job(client, chat_id, uid, fn):
    if uid in BUSY:
        await client.send_message(chat_id, "⏳ আপনার আগের কাজ এখনো চলছে, শেষ হওয়া পর্যন্ত অপেক্ষা করুন।")
        return
    BUSY.add(uid)
    msg = await client.send_message(chat_id, "⏳ শুরু হচ্ছে…")
    try:
        await fn(Progress(msg))
    except (ai.AIError, media.MediaError) as e:
        await msg.edit_text(f"❌ ব্যর্থ হয়েছে\n{e}")
    except Exception:
        LOGGER.exception("job failed")
        await msg.edit_text("❌ অপ্রত্যাশিত ত্রুটি হয়েছে, পরে আবার চেষ্টা করুন।")
    finally:
        BUSY.discard(uid)


async def need_gemini(client, chat_id, uid) -> bool:
    if await store.get_key(uid, "gemini"):
        return True
    await client.send_message(chat_id, "⚠️ আগে Gemini API যোগ করুন।",
                              reply_markup=M([[B("🔑 API যোগ করুন", "api:add:gemini")]]))
    return False


def video_ok(m):
    v = m.video or (m.document if m.document and (m.document.mime_type or "").startswith("video") else None)
    return v


async def start_dub(client, chat_id, uid, vmsg, lang):
    v = video_ok(vmsg)
    if not v:
        await client.send_message(chat_id, "🎞 অনুগ্রহ করে একটি ভিডিও পাঠান।")
        return
    if getattr(v, "duration", 0) and v.duration > MAX_SEC:
        await client.send_message(chat_id, f"⚠️ ভিডিও সর্বোচ্চ {MAX_SEC // 60} মিনিটের হতে পারবে।")
        return
    if not await need_gemini(client, chat_id, uid):
        return
    STATE.pop(uid, None)
    spawn(run_job(client, chat_id, uid, lambda p: pipelines.dub_video(client, chat_id, uid, vmsg, lang, p)))


async def start_create(client, chat_id, uid, text, photo_msg=None):
    if not text.strip():
        await client.send_message(chat_id, "✍️ ভিডিওর বর্ণনা লিখে পাঠান।")
        return
    if not await need_gemini(client, chat_id, uid):
        return
    STATE.pop(uid, None)
    spawn(run_job(client, chat_id, uid, lambda p: pipelines.create_video(client, chat_id, uid, text, photo_msg, p)))


async def start_speak(client, chat_id, uid, text):
    if not await need_gemini(client, chat_id, uid):
        return
    spawn(run_job(client, chat_id, uid, lambda p: pipelines.speak(client, chat_id, uid, text, p)))


# ───────────── কমান্ড ─────────────
@Client.on_message(filters.private & filters.command(["start", "menu"]))
async def cmd_start(client, m):
    STATE.pop(m.from_user.id, None)
    txt, kb = await main_menu(m.from_user.id)
    await m.reply_text(txt, reply_markup=kb)


# ───────────── বাটন ─────────────
@Client.on_callback_query()
async def on_cb(client, q: CallbackQuery):
    uid, chat_id = q.from_user.id, q.message.chat.id
    d = q.data.split(":")
    a = d[0] + ":" + d[1] if len(d) > 1 else d[0]
    await q.answer()

    if a == "m:main":
        STATE.pop(uid, None)
        return await show(q, *await main_menu(uid))

    # --- API ---
    if a == "api:menu":
        return await show(q, *await api_menu(uid))
    if a == "api:add":
        STATE[uid] = {"mode": "api", "provider": d[2]}
        return await show(q, f"🔑 আপনার **{d[2].title()}** API key পাঠান।\n"
                             "নিরাপত্তার জন্য পাঠানোর পরই বট আপনার মেসেজটি মুছে দেবে।", CANCEL)
    if a == "api:del":
        return await show(q, f"🗑 {d[2].title()} API ডিলেট করবেন?",
                          M([[B("✅ হ্যাঁ, ডিলেট", f"api:ok:{d[2]}"), B("❌ না", "api:menu")]]))
    if a == "api:ok":
        await store.del_key(uid, d[2])
        return await show(q, *await api_menu(uid))

    # --- সুইচ ---
    if a in ("ls:t", "sb:t", "au:t"):
        u = await store.get_user(uid)
        f = {"ls:t": "lipsync", "sb:t": "subtitles", "au:t": "auto"}[a]
        await store.set_field(uid, **{f: not u[f]})
        if f == "lipsync" and not u[f] and not await store.get_key(uid, "replicate"):
            await q.answer("লিপ-সিঙ্কের জন্য Replicate API যোগ করতে হবে", show_alert=True)
        return await show(q, *await main_menu(uid))

    # --- ভয়েস ---
    if a == "vc:menu":
        STATE.pop(uid, None)
        return await show(q, *await voice_menu(uid))
    if a == "vc:lang":
        await store.set_field(uid, lang=d[2])
        return await show(q, *await voice_menu(uid))
    if a == "vc:add":
        STATE[uid] = {"mode": "char_name"}
        return await show(q, "👤 নতুন চরিত্রের **নাম** লিখুন:", CANCEL)
    if a == "vc:dv":
        return await show(q, "🎚 ডিফল্ট ভয়েস বেছে নিন:", voice_picker("d"))
    if a == "vc:ch":
        u = await store.get_user(uid)
        i = int(d[2])
        if i >= len(u["chars"]):
            return await show(q, *await voice_menu(uid))
        c = u["chars"][i]
        kb = voice_picker(str(i))
        kb.inline_keyboard.insert(-1, [B("🗑 চরিত্র ডিলেট", f"vc:cd:{i}")])
        return await show(q, f"👤 **{c['name']}**\n{c.get('desc', '')[:300]}\n\nভয়েস বেছে নিন:", kb)
    if a == "vc:sv":
        voice = store.VOICE_NAMES[int(d[3])]
        if d[2] == "d":
            await store.set_field(uid, voice=voice)
        else:
            await store.set_char_voice(uid, int(d[2]), voice)
        return await show(q, *await voice_menu(uid))
    if a == "vc:cd":
        await store.del_char(uid, int(d[2]))
        return await show(q, *await voice_menu(uid))
    if a == "vc:speak":
        STATE[uid] = {"mode": "speak"}
        return await show(q, "🎙 যে লেখা ভয়েসে চান সেটি পাঠান।\n(বেছে নেওয়া ভাষায় নিজে অনুবাদ হবে)", CANCEL)

    # --- ভিডিও/ডাবিং ---
    if a == "vid:new":
        STATE[uid] = {"mode": "create"}
        return await show(q, "🎬 আপনার গল্প বা আইডিয়া লিখুন।\n"
                             "• অটো মোড ON থাকলে AI নিজেই চরিত্র, ভয়েস, সংলাপ ও দৃশ্য ঠিক করবে\n"
                             "• ছবি সহ পাঠালে ছবি থেকে ভিডিও শুরু হবে (ক্যাপশনে গল্প)", CANCEL)
    if a == "dub:bn" or a == "dub:hi":
        STATE[uid] = {"mode": "dub", "lang": d[1]}
        return await show(q, f"🎞 যে ভিডিও {store.LANG_LABEL[d[1]]}তে ডাব করবেন সেটি পাঠান।", CANCEL)
    if a == "dubv:bn" or a == "dubv:hi":
        vmsg = await client.get_messages(chat_id, int(d[2]))
        return await start_dub(client, chat_id, uid, vmsg, d[1])


# ───────────── মেসেজ ─────────────
MEDIA = filters.text | filters.photo | filters.video | filters.document
@Client.on_message(filters.private & MEDIA & ~filters.command(["start", "menu"]))
async def on_message(client, m):
    uid, chat_id = m.from_user.id, m.chat.id
    st = STATE.get(uid)
    text = (m.text or m.caption or "").strip()

    if st:
        mode = st["mode"]
        if mode == "api" and m.text:
            key = text
            try:
                await m.delete()
            except Exception:
                pass
            checker = ai.check_gemini if st["provider"] == "gemini" else ai.check_replicate
            if not await checker(key):
                return await client.send_message(chat_id, "❌ এই API key কাজ করছে না, আবার পাঠান বা বাতিল করুন।", reply_markup=CANCEL)
            await store.set_key(uid, st["provider"], key)
            STATE.pop(uid, None)
            await client.send_message(chat_id, f"✅ {st['provider'].title()} API যোগ হয়েছে।")
            return await client.send_message(chat_id, *(await api_menu(uid)))
        if mode == "char_name" and m.text:
            name = re.sub(r"[:：\n]", " ", text)[:30].strip()
            STATE[uid] = {"mode": "char_info", "name": name}
            return await m.reply_text(f"👤 **{name}** এর চেহারা সংরক্ষণ করতে একটি ছবি পাঠান, "
                                      "অথবা চেহারার বর্ণনা লিখুন।", reply_markup=CANCEL)
        if mode == "char_info" and (m.photo or m.text):
            if not await need_gemini(client, chat_id, uid):
                return
            u = await store.get_user(uid)
            voice = store.VOICE_NAMES[len(u["chars"]) % len(store.VOICE_NAMES)]
            photo, desc = None, text
            if m.photo:
                key = await store.get_key(uid, "gemini")
                wait = await m.reply_text("🔍 ছবি দেখে চেহারা বিশ্লেষণ হচ্ছে…")
                try:
                    p = await client.download_media(m, in_memory=True)
                    auto = await ai.describe_image(key, bytes(p.getbuffer()))
                    desc = (text + ". " if text else "") + auto
                    photo = m.photo.file_id
                except ai.AIError as e:
                    return await wait.edit_text(f"❌ {e}")
                await wait.delete()
            await store.add_char(uid, st["name"], desc, photo, voice)
            STATE.pop(uid, None)
            await m.reply_text(f"✅ চরিত্র **{st['name']}** সংরক্ষিত (ভয়েস: {voice})।")
            return await client.send_message(chat_id, *(await voice_menu(uid)))
        if mode == "speak" and m.text:
            return await start_speak(client, chat_id, uid, text)
        if mode == "create" and (m.text or m.photo):
            return await start_create(client, chat_id, uid, text, m if m.photo else None)
        if mode == "dub":
            return await start_dub(client, chat_id, uid, m, st["lang"])
        return await m.reply_text("চলমান ধাপ শেষ করুন অথবা বাতিল করুন।", reply_markup=CANCEL)

    # স্টেট নেই
    if video_ok(m):
        return await m.reply_text("🎞 এই ভিডিও কোন ভাষায় ডাব করবেন?", reply_markup=M([[
            B("🇧🇩 বাংলা", f"dubv:bn:{m.id}"), B("🇮🇳 হিন্দি", f"dubv:hi:{m.id}")]]))
    if m.photo:
        return await start_create(client, chat_id, uid, text, m)
    if m.text:
        return await handle_intent(client, m, uid, text)


async def handle_intent(client, m, uid, text):
    """সাধারণ টেক্সট বুঝে অ্যাকশন নেয়।"""
    chat_id = m.chat.id
    key = await store.get_key(uid, "gemini")
    if not key:
        txt, kb = await main_menu(uid)
        await m.reply_text("⚠️ শুরু করতে আগে Gemini API যোগ করুন।", reply_markup=M([[B("🔑 API যোগ করুন", "api:add:gemini")]]))
        return
    try:
        r = await ai.understand(key, text, store.VOICE_NAMES)
    except ai.AIError as e:
        return await m.reply_text(f"❌ {e}")
    act, args = r.get("action", "chat"), r.get("args") or {}

    if act == "create_video":
        return await start_create(client, chat_id, uid, str(args.get("prompt") or text))
    if act == "speak":
        return await start_speak(client, chat_id, uid, str(args.get("text") or text))
    if act == "add_character" and args.get("name"):
        u = await store.get_user(uid)
        voice = store.VOICE_NAMES[len(u["chars"]) % len(store.VOICE_NAMES)]
        await store.add_char(uid, str(args["name"])[:30], str(args.get("description", "")), None, voice)
        return await m.reply_text(f"✅ চরিত্র **{args['name']}** যোগ হয়েছে (ভয়েস: {voice})।")
    if act == "set_voice":
        u = await store.get_user(uid)
        v = next((x for x in store.VOICE_NAMES if x.lower() == str(args.get("voice", "")).lower()), None)
        i = next((i for i, c in enumerate(u["chars"]) if c["name"].lower() == str(args.get("character", "")).lower()), None)
        if v and i is not None:
            await store.set_char_voice(uid, i, v)
            return await m.reply_text(f"✅ {u['chars'][i]['name']} এর ভয়েস এখন {v}।")
        return await m.reply_text("চরিত্র বা ভয়েস খুঁজে পাইনি। 🎙 AI ভয়েস মেনু ব্যবহার করুন।")
    if act == "set_language" and args.get("language") in store.LANGS:
        await store.set_field(uid, lang=args["language"])
        return await m.reply_text(f"✅ ভাষা: {store.LANG_LABEL[args['language']]}")
    if act in ("subtitles", "lipsync"):
        await store.set_field(uid, **{act: bool(args.get("on"))})
        return await m.reply_text(f"✅ {'সাবটাইটেল' if act == 'subtitles' else 'লিপ-সিঙ্ক'}: {onoff(bool(args.get('on')))}")
    if act == "dub" and args.get("language") in store.LANGS:
        STATE[uid] = {"mode": "dub", "lang": args["language"]}
        return await m.reply_text(f"🎞 {store.LANG_LABEL[args['language']]} ডাবিংয়ের জন্য ভিডিও পাঠান।", reply_markup=CANCEL)
    if act == "api":
        return await m.reply_text(*[(await api_menu(uid))[0]], reply_markup=(await api_menu(uid))[1])
    await m.reply_text(str(args.get("reply") or "মেনু খুলতে /menu লিখুন।"))
