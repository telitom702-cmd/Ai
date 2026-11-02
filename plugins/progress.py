import time

from pyrogram.errors import FloodWait, MessageNotModified


class Progress:
    """টেলিগ্রাম মেসেজ এডিট করে প্রসেস বার দেখায়।"""

    def __init__(self, msg):
        self.msg = msg
        self.pct = 0
        self._last = 0.0

    async def set(self, pct: float, label: str, force: bool = False):
        pct = int(max(self.pct, min(100, pct)))
        now = time.monotonic()
        if not force and pct < 100 and now - self._last < 2.5:
            return
        self._last, self.pct = now, pct
        filled = pct // 10
        bar = "▰" * filled + "▱" * (10 - filled)
        try:
            await self.msg.edit_text(f"⏳ প্রসেস চলছে\n\n{bar} {pct}%\n{label}")
        except (MessageNotModified, FloodWait):
            pass
        except Exception:
            pass
