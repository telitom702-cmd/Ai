"""ffmpeg ও অডিও হেল্পার।"""
import asyncio
import os
import re
import textwrap
import wave
from array import array

RATE = 24000  # Gemini TTS আউটপুট: 24kHz, 16-bit, mono


class MediaError(Exception):
    pass


async def run(*args, cwd=None) -> str:
    p = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await p.communicate()
    if p.returncode:
        raise MediaError(f"{args[0]} ব্যর্থ: {err.decode(errors='ignore')[-300:]}")
    return out.decode(errors="ignore")


async def duration(path: str) -> float:
    out = await run("ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=nw=1:nk=1", path)
    return float(out.strip() or 0)


async def extract_audio(video: str, out_mp3: str):
    await run("ffmpeg", "-y", "-i", video, "-vn", "-ac", "1", "-ar", "16000",
              "-b:a", "48k", out_mp3)


def write_wav(path: str, pcm: bytes):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


def read_wav(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        return w.readframes(w.getnframes())


def silence(sec: float) -> bytes:
    return bytes(int(RATE * sec) * 2)


def pcm_len(pcm: bytes) -> float:
    return len(pcm) / 2 / RATE


async def fit_pcm(pcm: bytes, target: float, tmpdir: str) -> bytes:
    """ডাব করা কথা নির্ধারিত সময়ের চেয়ে লম্বা হলে স্পিড বাড়ায় (সর্বোচ্চ ১.৬x)।"""
    dur = pcm_len(pcm)
    if target <= 0 or dur <= target * 1.02:
        return pcm
    tempo = min(dur / target, 1.6)
    a, b = os.path.join(tmpdir, "fit_in.wav"), os.path.join(tmpdir, "fit_out.wav")
    write_wav(a, pcm)
    await run("ffmpeg", "-y", "-i", a, "-filter:a", f"atempo={tempo:.3f}",
              "-ar", str(RATE), "-ac", "1", b)
    return read_wav(b)


def mix_track(items, total: float) -> bytes:
    """items = [(start_sec, pcm)] -> পুরো ভিডিওর দৈর্ঘ্যের একটি অডিও ট্র্যাক।"""
    buf = array("h", bytes(int(total * RATE) * 2))
    last_end = 0
    for start, pcm in items:
        a = array("h")
        a.frombytes(pcm[: len(pcm) // 2 * 2])
        off = int(start * RATE)
        end = off + len(a)
        if end > len(buf):
            buf.extend(array("h", bytes((end - len(buf)) * 2)))
        if off >= last_end:
            buf[off:end] = a
        else:
            for i, v in enumerate(a, off):
                buf[i] = max(-32768, min(32767, buf[i] + v))
        last_end = max(last_end, end)
    return buf.tobytes()


async def mux(video: str, wav: str, out: str):
    """ভিডিওর অডিও বদলে নতুন অডিও বসায়। অডিও লম্বা হলে শেষ ফ্রেম ধরে রাখে।"""
    vd, ad = await duration(video), await duration(wav)
    if ad > vd + 0.15:
        await run("ffmpeg", "-y", "-i", video, "-i", wav, "-filter_complex",
                  f"[0:v]tpad=stop_mode=clone:stop_duration={ad - vd:.2f}[v]",
                  "-map", "[v]", "-map", "1:a:0", "-c:v", "libx264", "-preset", "veryfast",
                  "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out)
    else:
        await run("ffmpeg", "-y", "-i", video, "-i", wav, "-map", "0:v:0", "-map", "1:a:0",
                  "-c:v", "copy", "-c:a", "aac", "-af", "apad", "-shortest", out)


async def to_ogg(wav: str, ogg: str):
    await run("ffmpeg", "-y", "-i", wav, "-c:a", "libopus", "-b:a", "48k", ogg)


def _ts(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02}:{ms % 3600000 // 60000:02}:{ms % 60000 // 1000:02},{ms % 1000:03}"


def make_srt(entries) -> str:
    out = []
    for i, (s, e, text) in enumerate(entries, 1):
        text = "\n".join(textwrap.wrap(text, 38)[:3])
        out.append(f"{i}\n{_ts(s)} --> {_ts(max(e, s + 0.4))}\n{text}\n")
    return "\n".join(out)


def font_for(text: str) -> str:
    if re.search(r"[\u0980-\u09FF]", text):
        return "Noto Sans Bengali"
    if re.search(r"[\u0900-\u097F]", text):
        return "Noto Sans Devanagari"
    return "Noto Sans"


async def burn_subs(video: str, entries, out: str, workdir: str):
    srt = os.path.join(workdir, "subs.srt")
    with open(srt, "w", encoding="utf-8") as f:
        f.write(make_srt(entries))
    font = font_for(" ".join(t for _, _, t in entries))
    style = f"FontName={font},FontSize=18,Outline=2,Shadow=0,MarginV=25"
    await run("ffmpeg", "-y", "-i", video, "-vf", f"subtitles=subs.srt:force_style='{style}'",
              "-c:a", "copy", out, cwd=workdir)
