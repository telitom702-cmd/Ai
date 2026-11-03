"""ভিডিও তৈরি ও ডাবিং পাইপলাইন।"""
import logging
import os
import re
import shutil
import tempfile

from plugins import ai, media, store

LOGGER = logging.getLogger(__name__)
_DIALOGUE = re.compile(r"^\s*([^\s:：][^:：\n]{0,29}?)\s*[:：]\s*(.+?)\s*$")


def parse_script(text: str, chars: list, strict: bool):
    """'নাম: সংলাপ' লাইন আলাদা করে। strict=True হলে শুধু সংরক্ষিত চরিত্রের নাম ধরে।"""
    by_name = {c["name"].lower(): c for c in chars}
    scene, lines, used = [], [], []
    for ln in text.splitlines():
        if not ln.strip():
            continue
        m = _DIALOGUE.match(ln)
        if m:
            name, said = m.group(1).strip(), m.group(2)
            c = by_name.get(name.lower())
            if c or (not strict and len(name.split()) <= 2):
                lines.append((name, said))
                if c and c not in used:
                    used.append(c)
                continue
        scene.append(ln.strip())
    return scene, lines, used


def _voice_picker(u: dict):
    assigned = {}
    saved = {c["name"].lower(): c["voice"] for c in u["chars"]}

    def pick(name):
        if not name:
            return u["voice"]
        k = name.lower()
        if k in saved:
            return saved[k]
        if k not in assigned:
            assigned[k] = store.VOICE_NAMES[(len(assigned) + 1) % len(store.VOICE_NAMES)]
        return assigned[k]
    return pick


async def synthesize(key, lines, u, lang_name, prog=None, p0=0, p1=100):
    """lines=[(speaker|None, text)] -> (pcm, [(start,end,text)])"""
    texts = await ai.translate_lines(key, [t for _, t in lines], lang_name) if lang_name else [t for _, t in lines]
    pick = _voice_picker(u)
    pcm, entries, t = b"", [], 0.0
    for i, ((spk, _), text) in enumerate(zip(lines, texts)):
        if prog:
            await prog.set(p0 + (p1 - p0) * i / len(lines), f"ভয়েস তৈরি হচ্ছে ({i + 1}/{len(lines)})")
        seg = await ai.tts(key, text, pick(spk))
        d = media.pcm_len(seg)
        entries.append((t, t + d, text))
        pcm += seg + media.silence(0.25)
        t += d + 0.25
    return pcm, entries


MAX_SCENES = int(os.environ.get("MAX_SCENES", "3"))
NO_TEXT = "No subtitles or on-screen text."


async def _render_scene(wd, i, key, rkey, u, prompt, img, lines, tr_lang, prog, p0, p1):
    """একটি দৃশ্য: Veo ভিডিও + ভয়েস + (লিপ-সিঙ্ক বা মিক্স)। -> (path, entries, note)"""
    span = p1 - p0
    label = f"দৃশ্য {i + 1}: AI ভিডিও জেনারেট হচ্ছে (২-৫ মিনিট)"

    async def tick(sec):
        await prog.set(p0 + min(span * 0.55, sec / 6), label)

    await prog.set(p0, label)
    raw = os.path.join(wd, f"raw{i}.mp4")
    open(raw, "wb").write(await ai.veo_generate(key, prompt, img, tick=tick))

    note, entries = "", []
    if lines:
        pcm, entries = await synthesize(key, lines, u, tr_lang, prog, p0 + span * 0.6, p0 + span * 0.8)
    else:
        pcm = media.silence(await media.duration(raw))
    wav = os.path.join(wd, f"voice{i}.wav")
    media.write_wav(wav, pcm)
    out = os.path.join(wd, f"scene{i}.mp4")
    if lines and u["lipsync"] and rkey:
        try:
            await prog.set(p0 + span * 0.82, f"দৃশ্য {i + 1}: লিপ-সিঙ্ক হচ্ছে")
            open(out, "wb").write(await ai.lipsync(rkey, raw, wav))
            return out, entries, note
        except Exception as e:
            LOGGER.warning("lipsync failed: %s", e)
            note = "\n⚠️ কিছু দৃশ্যে লিপ-সিঙ্ক হয়নি"
    elif lines and u["lipsync"]:
        note = "\nℹ️ লিপ-সিঙ্কের জন্য Replicate API যোগ করুন"
    await prog.set(p0 + span * 0.9, f"দৃশ্য {i + 1}: ভয়েস বসানো হচ্ছে")
    await media.mux(raw, wav, out)
    return out, entries, note


def _scene_prompt(visual, used, has_lines):
    parts = [visual]
    parts += [f"Character {c['name']}: {c['desc']}" for c in used if c.get("desc")]
    if used:
        parts.append("Keep every character's face, hair and clothes identical and consistent.")
    parts.append(NO_TEXT + (" Characters speak with natural mouth movements." if has_lines else ""))
    return " ".join(parts)


async def _plan_auto(uid, key, u, text, lang):
    """AI গল্প বুঝে চরিত্র সেভ করে এবং দৃশ্যের তালিকা বানায়।"""
    plan = await ai.plan_story(key, text, lang, u["chars"], MAX_SCENES)
    for c in plan["characters"]:
        name = re.sub(r"[:：\n]", " ", str(c["name"]))[:30].strip()
        if name and name.lower() not in {x["name"].lower() for x in u["chars"]}:
            voice = store.voice_for_gender(c.get("gender", ""), u["chars"])
            desc = str(c.get("description", ""))
            await store.add_char(uid, name, desc, None, voice, str(c.get("gender", "")))
            u["chars"].append({"name": name, "desc": desc, "photo": None, "voice": voice})
    by_name = {c["name"].lower(): c for c in u["chars"]}
    scenes = []
    for s in plan["scenes"]:
        lines = [(str(x.get("speaker") or "").strip() or None, str(x.get("text", "")).strip())
                 for x in s.get("dialogue", []) if str(x.get("text", "")).strip()]
        visual = str(s.get("visual", "")).strip()
        used = []
        for n, c in by_name.items():
            if n in visual.lower() or any(sp and sp.lower() == n for sp, _ in lines):
                used.append(c)
        scenes.append({"prompt": _scene_prompt(visual, used, bool(lines)), "lines": lines,
                       "used": used, "continuous": bool(s.get("continuous"))})
    return scenes


async def create_video(client, chat_id, uid, text, photo_msg, prog):
    key = await store.get_key(uid, "gemini")
    rkey = await store.get_key(uid, "replicate")
    u = await store.get_user(uid)
    lang = store.LANGS[u["lang"]]
    wd = tempfile.mkdtemp(prefix="vid_")
    try:
        if u["auto"]:
            await prog.set(3, "🤖 গল্প বুঝে চরিত্র ও দৃশ্য ভাগ করা হচ্ছে")
            scenes, tr_lang = await _plan_auto(uid, key, u, text, lang), None
        else:
            scene, lines, used = parse_script(text, u["chars"], strict=True)
            vis = " ".join(scene).strip() or (
                "A cinematic scene where " + ", ".join(c["name"] for c in used) + " talk to each other")
            scenes = [{"prompt": _scene_prompt(vis, used, bool(lines)), "lines": lines,
                       "used": used, "continuous": False}]
            tr_lang = lang

        user_img = None
        if photo_msg is not None:
            await prog.set(5, "রেফারেন্স ছবি প্রস্তুত হচ্ছে")
            p = await client.download_media(photo_msg, file_name=os.path.join(wd, "user.jpg"))
            user_img = open(p, "rb").read()

        n, finals, entries_all, notes, offset, prev = len(scenes), [], [], set(), 0.0, None
        for i, sc in enumerate(scenes):
            img = user_img if i == 0 else None
            if img is None and sc["continuous"] and prev:
                frame = os.path.join(wd, f"last{i}.jpg")
                try:
                    await media.run("ffmpeg", "-y", "-sseof", "-0.2", "-i", prev, "-frames:v", "1", frame)
                    img = open(frame, "rb").read()
                except media.MediaError:
                    img = None
            if img is None:
                for c in sc["used"]:
                    if c.get("photo"):
                        p = await client.download_media(c["photo"], file_name=os.path.join(wd, f"c{i}.jpg"))
                        img = open(p, "rb").read()
                        break
            p0, p1 = 8 + 80 * i / n, 8 + 80 * (i + 1) / n
            out, ents, note = await _render_scene(wd, i, key, rkey, u, sc["prompt"], img,
                                                  sc["lines"], tr_lang, prog, p0, p1)
            finals.append(out)
            entries_all += [(s_ + offset, e_ + offset, t_) for s_, e_, t_ in ents]
            offset += await media.duration(out)
            prev = out
            if note:
                notes.add(note)

        final = finals[0]
        if n > 1:
            await prog.set(90, "দৃশ্যগুলো জোড়া লাগানো হচ্ছে")
            final = os.path.join(wd, "story.mp4")
            await media.concat(finals, final)
        if u["subtitles"] and entries_all:
            await prog.set(94, "সাবটাইটেল বসানো হচ্ছে")
            out = os.path.join(wd, "sub.mp4")
            await media.burn_subs(final, entries_all, out, wd)
            final = out

        await prog.set(98, "আপলোড হচ্ছে")
        names = ", ".join(c["name"] for c in u["chars"][-5:]) if u["auto"] else ""
        cap = "🎬 আপনার ভিডিও তৈরি!" + (f"\n👤 চরিত্র: {names}" if names else "") + "".join(notes)
        await client.send_video(chat_id, final, caption=cap, supports_streaming=True)
        await prog.set(100, "✅ সম্পন্ন", force=True)
    finally:
        shutil.rmtree(wd, ignore_errors=True)


async def dub_video(client, chat_id, uid, vmsg, lang_code, prog):
    key = await store.get_key(uid, "gemini")
    rkey = await store.get_key(uid, "replicate")
    u = await store.get_user(uid)
    lang = store.LANGS[lang_code]
    wd = tempfile.mkdtemp(prefix="dub_")
    try:
        await prog.set(3, "ভিডিও ডাউনলোড হচ্ছে")
        src = await client.download_media(vmsg, file_name=os.path.join(wd, "src.mp4"))
        total = await media.duration(src)

        await prog.set(10, "অডিও আলাদা করা হচ্ছে")
        mp3 = os.path.join(wd, "a.mp3")
        await media.extract_audio(src, mp3)

        await prog.set(15, "কথা শুনে টেক্সট বের করা হচ্ছে")
        segs = await ai.transcribe(key, open(mp3, "rb").read())
        if not segs:
            raise ai.AIError("ভিডিওতে কোনো কথা পাওয়া যায়নি")

        await prog.set(35, f"{store.LANG_LABEL[lang_code]} ভাষায় অনুবাদ হচ্ছে")
        texts = await ai.translate_lines(key, [s["text"] for s in segs], lang)

        order, saved = [], [c["voice"] for c in u["chars"]]
        pool = saved + [v for v in store.VOICE_NAMES if v not in saved]
        voice_of = {}
        for s in segs:
            if s["speaker"] not in voice_of:
                voice_of[s["speaker"]] = pool[len(voice_of) % len(pool)]

        items, entries = [], []
        for i, (s, t) in enumerate(zip(segs, texts)):
            await prog.set(45 + 35 * i / len(segs), f"ডাবিং ভয়েস তৈরি ({i + 1}/{len(segs)})")
            nxt = segs[i + 1]["start"] if i + 1 < len(segs) else total
            window = max(0.5, min(nxt, total) - s["start"] - 0.05)
            pcm = await media.fit_pcm(await ai.tts(key, t, voice_of[s["speaker"]]), window, wd)
            items.append((s["start"], pcm))
            entries.append((s["start"], s["start"] + min(media.pcm_len(pcm), window), t))

        await prog.set(82, "অডিও ট্র্যাক সাজানো হচ্ছে")
        wav = os.path.join(wd, "dub.wav")
        media.write_wav(wav, media.mix_track(items, total))

        final, note, done = None, "", False
        if u["lipsync"] and rkey:
            try:
                async def t2(sec):
                    await prog.set(min(92, 85 + sec / 15), "লিপ-সিঙ্ক হচ্ছে")
                await prog.set(85, "লিপ-সিঙ্ক হচ্ছে")
                out = os.path.join(wd, "ls.mp4")
                open(out, "wb").write(await ai.lipsync(rkey, src, wav, t2))
                final, done = out, True
            except Exception as e:
                LOGGER.warning("lipsync failed: %s", e)
                note = "\n⚠️ লিপ-সিঙ্ক হয়নি, সাধারণ ডাবিং করা হয়েছে"
        elif u["lipsync"]:
            note = "\nℹ️ লিপ-সিঙ্কের জন্য Replicate API যোগ করুন"
        if not done:
            await prog.set(92, "ভিডিওতে অডিও বসানো হচ্ছে")
            final = os.path.join(wd, "mux.mp4")
            await media.mux(src, wav, final)

        if u["subtitles"]:
            await prog.set(95, "সাবটাইটেল বসানো হচ্ছে")
            out = os.path.join(wd, "sub.mp4")
            await media.burn_subs(final, entries, out, wd)
            final = out

        await prog.set(98, "আপলোড হচ্ছে")
        await client.send_video(chat_id, final, caption=f"🎙 {store.LANG_LABEL[lang_code]} ডাবিং সম্পন্ন!" + note,
                                supports_streaming=True)
        await prog.set(100, "✅ সম্পন্ন", force=True)
    finally:
        shutil.rmtree(wd, ignore_errors=True)


async def speak(client, chat_id, uid, text, prog):
    key = await store.get_key(uid, "gemini")
    u = await store.get_user(uid)
    _, lines, _ = parse_script(text, u["chars"], strict=False)
    if not lines:
        lines = [(None, text.strip())]
    wd = tempfile.mkdtemp(prefix="spk_")
    try:
        await prog.set(10, "অনুবাদ ও ভয়েস তৈরি হচ্ছে")
        pcm, entries = await synthesize(key, lines, u, store.LANGS[u["lang"]], prog, 15, 85)
        wav, ogg = os.path.join(wd, "v.wav"), os.path.join(wd, "v.ogg")
        media.write_wav(wav, pcm)
        await media.to_ogg(wav, ogg)
        await client.send_voice(chat_id, ogg, caption="\n".join(t for _, _, t in entries)[:900])
        await prog.set(100, "✅ সম্পন্ন", force=True)
    finally:
        shutil.rmtree(wd, ignore_errors=True)
