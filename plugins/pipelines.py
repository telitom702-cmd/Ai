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


async def create_video(client, chat_id, uid, text, photo_msg, prog):
    key = await store.get_key(uid, "gemini")
    rkey = await store.get_key(uid, "replicate")
    u = await store.get_user(uid)
    lang = store.LANGS[u["lang"]]
    scene, lines, used = parse_script(text, u["chars"], strict=True)
    scene_txt = " ".join(scene).strip()
    if not scene_txt:
        scene_txt = "A cinematic scene where " + ", ".join(c["name"] for c in used) + " talk to each other"

    parts = [scene_txt]
    parts += [f"Character {c['name']}: {c['desc']}" for c in used if c.get("desc")]
    if used:
        parts.append("Keep every character's face, hair and clothes identical and consistent.")
    parts.append("No subtitles or on-screen text." + (" Characters speak with natural mouth movements." if lines else ""))
    prompt = " ".join(parts)

    wd = tempfile.mkdtemp(prefix="vid_")
    try:
        await prog.set(3, "রেফারেন্স ছবি প্রস্তুত হচ্ছে")
        img = None
        if photo_msg is not None:
            p = await client.download_media(photo_msg, file_name=os.path.join(wd, "ref.jpg"))
            img = open(p, "rb").read()
        else:
            for c in used:
                if c.get("photo"):
                    p = await client.download_media(c["photo"], file_name=os.path.join(wd, "ref.jpg"))
                    img = open(p, "rb").read()
                    break

        async def tick(sec):
            await prog.set(min(60, 8 + sec / 4), "AI ভিডিও জেনারেট হচ্ছে (২-৫ মিনিট)")

        await prog.set(8, "AI ভিডিও জেনারেট হচ্ছে (২-৫ মিনিট)")
        data = await ai.veo_generate(key, prompt, img, tick=tick)
        raw = os.path.join(wd, "raw.mp4")
        open(raw, "wb").write(data)
        final, note, entries = raw, "", []

        if lines:
            pcm, entries = await synthesize(key, lines, u, lang, prog, 62, 78)
            wav = os.path.join(wd, "voice.wav")
            media.write_wav(wav, pcm)
            done = False
            if u["lipsync"] and rkey:
                try:
                    async def t2(sec):
                        await prog.set(min(90, 80 + sec / 10), "লিপ-সিঙ্ক হচ্ছে")
                    await prog.set(80, "লিপ-সিঙ্ক হচ্ছে")
                    out = os.path.join(wd, "ls.mp4")
                    open(out, "wb").write(await ai.lipsync(rkey, raw, wav, t2))
                    final, done = out, True
                except Exception as e:
                    LOGGER.warning("lipsync failed: %s", e)
                    note = "\n⚠️ লিপ-সিঙ্ক হয়নি, সাধারণ ভয়েস বসানো হয়েছে"
            elif u["lipsync"]:
                note = "\nℹ️ লিপ-সিঙ্কের জন্য Replicate API যোগ করুন"
            if not done:
                await prog.set(90, "ভয়েস বসানো হচ্ছে")
                out = os.path.join(wd, "mux.mp4")
                await media.mux(raw, wav, out)
                final = out

        if u["subtitles"] and entries:
            await prog.set(94, "সাবটাইটেল বসানো হচ্ছে")
            out = os.path.join(wd, "sub.mp4")
            await media.burn_subs(final, entries, out, wd)
            final = out

        await prog.set(98, "আপলোড হচ্ছে")
        await client.send_video(chat_id, final, caption="🎬 আপনার ভিডিও তৈরি!" + note, supports_streaming=True)
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
