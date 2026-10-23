#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Amir Temur haqida 30 soniyalik vertikal (1080x1920) tarixiy video generatori.

Ishga tushirish:
  Web servis :  uvicorn main:app --host 0.0.0.0 --port 10000
  CLI        :  python main.py --cli          (PREVIEW=1 bilan tez sinov)
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import wave
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

# ----------------------------------------------------------------------------
# SOZLAMALAR
# ----------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
log = logging.getLogger("temur")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(BASE_DIR / "output")))
CACHE_DIR = Path(os.getenv("CACHE_DIR", str(BASE_DIR / "cache")))
ASSETS_DIR = BASE_DIR / "assets"
OUTPUT_MP4 = OUTPUT_DIR / "amir_temur.mp4"
OUTPUT_SRT = OUTPUT_DIR / "amir_temur.srt"
OUTPUT_CREDITS = OUTPUT_DIR / "credits.json"

PREVIEW = os.getenv("PREVIEW", "0") == "1"          # 540x960 tez sinov
W, H = (540, 960) if PREVIEW else (1080, 1920)
SCALE = W / 1080.0
FPS = _env_int("VIDEO_FPS", 24)
DURATION = 30                                         # aniq 30 soniya
XFADE = 0.6                                           # sahnalar orasidagi o‘tish
SR = 44100                                            # audio sample rate

X264_PRESET = os.getenv("X264_PRESET", "veryfast")
X264_CRF = os.getenv("X264_CRF", "22")

TTS_ENGINE = os.getenv("TTS_ENGINE", "auto").lower()  # auto|edge|gtts|espeak
TTS_VOICE = os.getenv("TTS_VOICE", "uz-UZ-SardorNeural")
TTS_RATE = os.getenv("TTS_RATE", "+0%")
TTS_PITCH = os.getenv("TTS_PITCH", "-8Hz")
MAX_TEMPO = _env_float("MAX_TEMPO", 1.35)             # nutqni eng ko‘pi bilan shuncha tezlashtirish
VOICE_OFFSET = 0.3                                    # sahna boshidan keyin nutq boshlanishi
VOICE_GAIN = _env_float("VOICE_GAIN", 1.0)
MUSIC_VOLUME = _env_float("MUSIC_VOLUME", 0.35)
DUCK_AMOUNT = _env_float("DUCK_AMOUNT", 0.55)
MUSIC_PATH = os.getenv("MUSIC_PATH", str(ASSETS_DIR / "music.mp3"))

SUB_BOTTOM_RATIO = _env_float("SUB_BOTTOM_RATIO", 0.76)  # subtitr pastki chegarasi (interfeysdan yuqorida)
STRICT_IMAGES = os.getenv("STRICT_IMAGES", "0") == "1"

WIKI_API = "https://commons.wikimedia.org/w/api.php"
WIKI_USER_AGENT = os.getenv(
    "WIKI_USER_AGENT",
    "AmirTemurVideoBot/1.0 (educational project; set WIKI_USER_AGENT env with your contact email)",
)
ALLOWED_LICENSES_ENV = os.getenv("ALLOWED_LICENSES", "")  # masalan: "public domain,cc0"

API_TOKEN = os.getenv("API_TOKEN", "")

TITLE_TEXT = "AMIR TEMUR — TARIXDA O‘CHMAS IZ"

FONT_CANDIDATES = [
    os.getenv("FONT_PATH", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
    str(ASSETS_DIR / "font.ttf"),
]


# ----------------------------------------------------------------------------
# SAHNALAR
# ----------------------------------------------------------------------------
@dataclass
class Scene:
    idx: int
    start: float
    end: float
    name: str
    queries: list
    speech: str       # TTS uchun matn (raqamlar so‘z bilan, apostrof ASCII)
    subtitle: str     # ekranda ko‘rinadigan matn
    motion: tuple     # (zoom0, zoom1, pan_x0, pan_x1)
    is_title: bool = False

    @property
    def duration(self) -> float:
        return self.end - self.start


SCENES = [
    Scene(
        1, 0, 5, "Qadimiy Samarqand",
        ["Registan Samarkand", "Samarkand panorama", "Samarkand Registan sunset"],
        "Samarqand. Buyuk Ipak yo'li bo'yidagi qadimiy shahar.",
        "Samarqand. Buyuk Ipak yo‘li bo‘yidagi qadimiy shahar.",
        (1.00, 1.14, 0.0, 1.0),
    ),
    Scene(
        2, 5, 10, "Amir Temur",
        ["Timur Tamerlane portrait miniature", "Amir Timur monument Shahrisabz",
         "Tamerlane painting", "Amir Timur statue Tashkent"],
        "Amir Temur ming uch yuz o'ttiz oltinchi yilda Kesh yaqinida tug'ilgan.",
        "Amir Temur 1336-yilda Kesh yaqinida tug‘ilgan.",
        (1.15, 1.00, 0.5, 0.5),
    ),
    Scene(
        3, 10, 16, "Qo‘shin",
        ["Timurid army miniature", "Timur army Zafarnama", "Timur enthroned miniature horsemen",
         "Timurid cavalry miniature"],
        "U ming uch yuz yetmishinchi yilda Samarqandni poytaxt qildi.",
        "U 1370-yilda Samarqandni poytaxt qildi.",
        (1.00, 1.15, 1.0, 0.0),
    ),
    Scene(
        4, 16, 22, "Yurishlar",
        ["Timur battle miniature", "Battle of Ankara 1402 painting", "Tamerlane Bayezid Ankara",
         "Zafarnama battle miniature"],
        "Yurishlari Eron, Oltin O'rda, Hindiston va Anatoliyagacha yetdi.",
        "Yurishlari Eron, Oltin O‘rda, Hindiston va Anatoliyagacha yetdi.",
        (1.12, 1.00, 0.0, 1.0),
    ),
    Scene(
        5, 22, 27, "Temuriylar me’morchiligi",
        ["Gur-e-Amir Samarkand", "Bibi-Khanym Mosque Samarkand", "Shah-i-Zinda Samarkand"],
        "Go'ri Amir va Bibixonim masjidi Temuriylar me'morchiligidan dalolat beradi.",
        "Go‘ri Amir va Bibixonim masjidi Temuriylar me’morchiligidan dalolat beradi.",
        (1.00, 1.16, 0.5, 0.5),
    ),
    Scene(
        6, 27, 30, "Yakuniy kadr",
        ["Amir Timur monument Tashkent", "Timur statue", "Registan Samarkand night"],
        "Amir Temur. Tarixda o'chmas iz.",
        "",
        (1.00, 1.08, 0.5, 0.5),
        is_title=True,
    ),
]


# ----------------------------------------------------------------------------
# UMUMIY YORDAMCHILAR
# ----------------------------------------------------------------------------
ProgressFn = Callable[[float, str], None]


def run_cmd(cmd: list, what: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{what} xatosi (kod {proc.returncode}): {proc.stderr[-800:]}")


def check_tools() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        raise RuntimeError(
            f"{', '.join(missing)} topilmadi. Dockerfile orqali ishga tushiring yoki FFmpeg o‘rnating."
        )


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        ch = w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data


def write_wav(path: Path, x: np.ndarray) -> None:
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        return -1.0


def moving_avg(x: np.ndarray, k: int) -> np.ndarray:
    k = max(1, int(k))
    c = np.cumsum(np.insert(x.astype(np.float64), 0, 0.0))
    out = (c[k:] - c[:-k]) / k
    left = k // 2
    right = len(x) - len(out) - left
    return np.pad(out, (left, max(0, right)), mode="edge")[: len(x)]


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_CANDIDATES:
        if p and Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    log.warning("TrueType shrift topilmadi — standart shrift ishlatiladi (o‘ʻ belgilari buzilishi mumkin).")
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


# ----------------------------------------------------------------------------
# 1) TASVIRLAR (Wikimedia Commons)
# ----------------------------------------------------------------------------
BAD_TITLE = re.compile(r"\b(logo|flag|coat of arms|stamp|banknote|coin|map|locator|icon|diagram)\b", re.I)


def strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def license_ok(name: str) -> bool:
    n = (name or "").lower().strip()
    if not n:
        return False
    if ALLOWED_LICENSES_ENV:
        allowed = [a.strip().lower() for a in ALLOWED_LICENSES_ENV.split(",") if a.strip()]
        return any(n.startswith(a) for a in allowed)
    tokens = re.split(r"[\s\-_/]+", n)
    if "nc" in tokens or "nd" in tokens:
        return False
    return n.startswith(("public domain", "cc0", "pd", "cc by", "cc-by", "no restrictions"))


def http_get(session: requests.Session, url: str, params: Optional[dict] = None, tries: int = 3):
    last: Optional[Exception] = None
    for i in range(tries):
        try:
            r = session.get(url, params=params, timeout=40)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except (requests.RequestException, OSError) as e:
            last = e
            wait = 2 ** i
            log.warning("So‘rov xatosi (%s): %s — %s soniyadan keyin qayta uriniladi", url[:70], e, wait)
            time.sleep(wait)
    raise RuntimeError(f"Tarmoq xatosi: {url[:80]} … ({last}). Internet/DNS yoki Wikimedia cheklovini tekshiring.")


def search_commons(session: requests.Session, query: str) -> list:
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": query, "gsrnamespace": 6, "gsrlimit": 25,
        "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1600,
    }
    data = http_get(session, WIKI_API, params).json()
    pages = (data.get("query") or {}).get("pages") or {}
    out = []
    for p in sorted(pages.values(), key=lambda x: x.get("index", 0)):
        ii = (p.get("imageinfo") or [None])[0]
        if not ii:
            continue
        meta = ii.get("extmetadata") or {}

        def mv(key: str) -> str:
            return strip_html((meta.get(key) or {}).get("value", ""))

        out.append({
            "title": p.get("title", ""),
            "url": ii.get("thumburl") or ii.get("url"),
            "page": ii.get("descriptionurl", ""),
            "mime": ii.get("mime", ""),
            "width": ii.get("width", 0),
            "height": ii.get("height", 0),
            "license": mv("LicenseShortName"),
            "license_url": mv("LicenseUrl"),
            "author": mv("Artist") or mv("Credit"),
        })
    return out


def candidate_ok(c: dict, used: set) -> bool:
    return (
        c["url"]
        and c["page"] not in used
        and c["mime"] in ("image/jpeg", "image/png")
        and max(c["width"], c["height"]) >= 900
        and not BAD_TITLE.search(c["title"])
        and license_ok(c["license"])
    )


def find_online(session: requests.Session, scene: Scene, used: set):
    for q in scene.queries:
        log.info("Sahna %d: Commons qidiruvi: %r", scene.idx, q)
        try:
            results = search_commons(session, q)
        except Exception as e:
            log.error("Qidiruv muvaffaqiyatsiz (%r): %s", q, e)
            continue
        time.sleep(0.5)
        tried = 0
        for c in results:
            if not candidate_ok(c, used):
                continue
            tried += 1
            if tried > 4:
                break
            try:
                r = http_get(session, c["url"])
                img = Image.open(io.BytesIO(r.content))
                img = ImageOps.exif_transpose(img).convert("RGB")
                if min(img.size) < 500:
                    continue
                return img, c
            except Exception as e:
                log.error("Rasm yuklab olinmadi (%s): %s", c["title"], e)
    return None


def find_local(idx: int) -> Optional[Path]:
    for ext in ("jpg", "jpeg", "png", "webp"):
        p = ASSETS_DIR / "images" / f"scene{idx}.{ext}"
        if p.exists():
            return p
    return None


def make_placeholder(scene: Scene) -> Image.Image:
    """Internet yoki litsenziyali rasm topilmasa ham xavfsiz zaxira kadr yaratadi."""
    # PixelAccess orqali minglab alohida yozish o'rniga, barqaror NumPy gradient.
    yy = np.linspace(0, 1, 1600, dtype=np.float32)[:, None]
    base = (18 + 55 * (1 - np.abs(yy - 0.5) / 0.5)).clip(0, 255)
    rgb = np.zeros((1600, 1600, 3), dtype=np.uint8)
    rgb[:, :, 0] = (base + 14).astype(np.uint8)
    rgb[:, :, 1] = (base + 6).astype(np.uint8)
    rgb[:, :, 2] = base.astype(np.uint8)
    img = Image.fromarray(rgb, "RGB")
    d = ImageDraw.Draw(img)
    f = load_font(110)
    title = scene.name.upper()
    # Uzun nomlar ham kadrga sig'ishi uchun shriftni moslaymiz.
    f = fit_font(title, 110, 1450)
    tw = d.textlength(title, font=f)
    d.text(((1600 - tw) / 2, 760), title, font=f, fill=(190, 160, 90))
    return img


def fetch_images(warnings: list, progress: ProgressFn):
    img_dir = CACHE_DIR / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = WIKI_USER_AGENT
    used: set = set()
    paths: dict = {}
    credits: list = []

    for k, sc in enumerate(SCENES):
        progress(2 + 13 * k / len(SCENES), f"Tasvir: sahna {sc.idx}/{len(SCENES)}")
        local = find_local(sc.idx)
        if local:
            paths[sc.idx] = local
            credits.append({"scene": sc.idx, "source": "assets/images", "file": local.name,
                            "note": "Litsenziyani o‘zingiz tekshiring"})
            continue

        cached, meta = img_dir / f"scene{sc.idx}.jpg", img_dir / f"scene{sc.idx}.json"
        if cached.exists() and meta.exists():
            info = json.loads(meta.read_text(encoding="utf-8"))
            used.add(info.get("page", ""))
            paths[sc.idx] = cached
            credits.append(info)
            continue

        found = None
        try:
            found = find_online(session, sc, used)
        except Exception as e:  # pragma: no cover
            log.error("Sahna %d tasvir qidirishda kutilmagan xato: %s", sc.idx, e)

        if found:
            img, c = found
            img.save(cached, "JPEG", quality=93)
            info = {"scene": sc.idx, "source": "Wikimedia Commons", "title": c["title"], "page": c["page"],
                    "author": c["author"], "license": c["license"], "license_url": c["license_url"]}
            meta.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
            used.add(c["page"])
            paths[sc.idx] = cached
            credits.append(info)
            log.info("Sahna %d: %s (%s)", sc.idx, c["title"], c["license"])
        else:
            msg = (f"Sahna {sc.idx} ({sc.name}) uchun tasvir olinmadi: internet yoki mos litsenziyali "
                   f"natija yo‘q. assets/images/scene{sc.idx}.jpg qo‘ying yoki qayta urining.")
            log.error(msg)
            if STRICT_IMAGES:
                raise RuntimeError(msg)
            warnings.append(msg)
            ph = img_dir / f"placeholder{sc.idx}.jpg"
            make_placeholder(sc).save(ph, "JPEG", quality=90)
            paths[sc.idx] = ph
            credits.append({"scene": sc.idx, "source": "zaxira kadr (tasvir topilmadi)"})
    return paths, credits


# ----------------------------------------------------------------------------
# 2) OVOZ (TTS)
# ----------------------------------------------------------------------------
async def _edge_tts(text: str, path: Path) -> None:
    import edge_tts
    comm = edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE, pitch=TTS_PITCH)
    await asyncio.wait_for(comm.save(str(path)), timeout=60)


def _to_wav(src: Path, dst: Path) -> None:
    run_cmd(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
             "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", str(dst)], "Audio konvertatsiyasi")


def synth_speech(text: str, out_wav: Path, workdir: Path) -> str:
    order = {"auto": ["edge", "gtts", "espeak"]}.get(TTS_ENGINE, [TTS_ENGINE])
    for eng in order:
        try:
            if eng == "edge":
                mp3 = workdir / (out_wav.stem + "_edge.mp3")
                asyncio.run(_edge_tts(text, mp3))
                _to_wav(mp3, out_wav)
            elif eng == "gtts":
                from gtts import gTTS
                mp3 = workdir / (out_wav.stem + "_gtts.mp3")
                gTTS(text=text, lang="uz").save(str(mp3))
                _to_wav(mp3, out_wav)
            elif eng == "espeak":
                if shutil.which("espeak-ng") is None:
                    raise RuntimeError("espeak-ng o‘rnatilmagan")
                raw = workdir / (out_wav.stem + "_espeak.wav")
                run_cmd(["espeak-ng", "-v", "uz", "-s", "150", "-w", str(raw), text], "espeak-ng")
                _to_wav(raw, out_wav)
            else:
                raise RuntimeError(f"Noma’lum TTS: {eng}")
            if out_wav.exists() and wav_duration(out_wav) > 0.2:
                return eng
        except Exception as e:
            log.error("TTS '%s' ishlamadi: %s", eng, e)
    return ""


@dataclass
class VoiceClip:
    scene: Scene
    path: Path
    start: float
    duration: float
    tempo: float


def fit_to_window(src: Path, dst: Path, window: float, warnings: list, tag: str):
    dur = wav_duration(src)
    if dur <= window:
        shutil.copy(src, dst)
        return dur, 1.0
    tempo = dur / window
    if tempo > MAX_TEMPO:
        warnings.append(f"{tag}: nutq juda uzun ({dur:.1f}s > {window:.1f}s), "
                        f"{MAX_TEMPO}x tezlashtirilib kesildi. Matnni qisqartiring.")
        tempo = MAX_TEMPO
    filters = [f"atempo={tempo:.4f}"]
    if dur / tempo > window:
        filters.append(f"afade=t=out:st={max(0.0, window - 0.15):.3f}:d=0.15")
    run_cmd(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
             "-af", ",".join(filters), "-t", f"{window:.3f}", "-ar", str(SR), "-ac", "1",
             "-c:a", "pcm_s16le", str(dst)], "Audio tezligini moslash")
    return wav_duration(dst), tempo


def build_voice(work: Path, warnings: list, progress: ProgressFn) -> dict:
    clips: dict = {}
    for k, sc in enumerate(SCENES):
        progress(16 + 14 * k / len(SCENES), f"Ovoz yaratilmoqda: sahna {sc.idx}/{len(SCENES)}")
        raw = work / f"voice_raw_{sc.idx}.wav"
        eng = synth_speech(sc.speech, raw, work)
        if not eng:
            warnings.append(f"Sahna {sc.idx}: hech bir TTS ishlamadi — bu sahna ovozsiz.")
            continue
        log.info("Sahna %d: TTS=%s, xom davomiylik %.2fs", sc.idx, eng, wav_duration(raw))
        window = sc.duration - VOICE_OFFSET - 0.2
        fitted = work / f"voice_fit_{sc.idx}.wav"
        dur, tempo = fit_to_window(raw, fitted, window, warnings, f"Sahna {sc.idx}")
        clips[sc.idx] = VoiceClip(sc, fitted, sc.start + VOICE_OFFSET, dur, tempo)
        log.info("Sahna %d: davomiylik %.2fs (tempo %.2fx)", sc.idx, dur, tempo)
    return clips


# ----------------------------------------------------------------------------
# 3) SUBTITRLAR
# ----------------------------------------------------------------------------
@dataclass
class Cue:
    start: float
    end: float
    text: str
    on_video: bool = True


def split_cues(text: str, max_chars: int = 38) -> list:
    words, cues, cur = text.split(), [], []
    for w in words:
        if cur and len(" ".join(cur + [w])) > max_chars:
            cues.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        cues.append(" ".join(cur))
    if len(cues) >= 2 and len(cues[-1]) < 8:
        cues[-2] += " " + cues.pop()
    return cues


def build_cues(clips: dict) -> list:
    cues: list = []
    for sc in SCENES:
        if sc.is_title or not sc.subtitle:
            continue
        clip = clips.get(sc.idx)
        start = clip.start if clip else sc.start + VOICE_OFFSET
        dur = clip.duration if clip and clip.duration > 0.3 else min(sc.duration - 0.6, len(sc.subtitle) / 14.0)
        parts = split_cues(sc.subtitle)
        total = sum(len(p) for p in parts)
        t = start
        for i, p in enumerate(parts):
            d = dur * len(p) / total
            end = t + d + (0.3 if i == len(parts) - 1 else 0.0)
            cues.append(Cue(t, min(end, sc.end - 0.05), p))
            t += d
    title_scene = SCENES[-1]
    cues.append(Cue(title_scene.start + 0.3, float(DURATION), TITLE_TEXT, on_video=False))
    return cues


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: list, path: Path) -> None:
    lines = []
    for i, c in enumerate(cues, 1):
        lines += [str(i), f"{srt_time(c.start)} --> {srt_time(c.end)}", c.text, ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def wrap_lines(draw: ImageDraw.ImageDraw, text: str, font, max_w: float) -> list:
    lines, cur = [], ""
    for w in text.split():
        trial = (cur + " " + w).strip()
        if cur and draw.textlength(trial, font=font) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def make_subtitle_layer(text: str, font) -> Image.Image:
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    stroke = max(2, int(5 * SCALE))
    lines = wrap_lines(probe, text, font, W * 0.88)
    lh = int(font.size * 1.28) if hasattr(font, "size") else int(80 * SCALE)
    layer = Image.new("RGBA", (W, lh * len(lines) + 2 * stroke + 8), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i, ln in enumerate(lines):
        tw = d.textlength(ln, font=font)
        d.text(((W - tw) / 2, i * lh + stroke + 2), ln, font=font, fill=(255, 255, 255, 255),
               stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    return layer


def fit_font(text: str, size: int, max_w: float):
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    while size > 20:
        f = load_font(size)
        if probe.textlength(text, font=f) <= max_w:
            return f
        size -= 4
    return load_font(20)


def make_title_layer() -> Image.Image:
    l1, l2 = "AMIR TEMUR —", "TARIXDA O‘CHMAS IZ"
    f1 = fit_font(l1, int(124 * SCALE), W * 0.88)
    f2 = fit_font(l2, int(86 * SCALE), W * 0.88)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    st = max(3, int(6 * SCALE))
    y1, y2 = int(H * 0.40), int(H * 0.40 + 150 * SCALE)
    for txt, f, y in ((l1, f1, y1), (l2, f2, y2)):
        tw = d.textlength(txt, font=f)
        d.text(((W - tw) / 2, y), txt, font=f, fill=(255, 255, 255, 255),
               stroke_width=st, stroke_fill=(0, 0, 0, 255))
    bar_w = int(W * 0.34)
    yb = int(y2 + 150 * SCALE)
    d.rectangle([(W - bar_w) // 2, yb, (W + bar_w) // 2, yb + max(3, int(6 * SCALE))], fill=(212, 175, 55, 255))
    return layer


# ----------------------------------------------------------------------------
# 4) AUDIO: musiqa sintezi + aralashtirish
# ----------------------------------------------------------------------------
def synth_music(duration: float) -> np.ndarray:
    """Original (mualliflik huquqisiz) dramatik fon: Dm–Bb–Gm–A pad + nog‘oralar."""
    n = int(duration * SR)
    psr = 11025
    pn = int(duration * psr)
    pt = np.arange(pn, dtype=np.float32) / psr
    chords = [
        [73.42, 110.00, 146.83, 174.61],   # Dm
        [58.27, 116.54, 146.83, 174.61],   # Bb
        [98.00, 146.83, 196.00, 233.08],   # Gm
        [55.00, 110.00, 138.59, 164.81],   # A
    ]
    seg = duration / len(chords)
    pad = np.zeros(pn, dtype=np.float32)
    for i, freqs in enumerate(chords):
        c = (i + 0.5) * seg
        env = np.clip(1.0 - np.abs(pt - c) / seg, 0.0, 1.0)
        active = env > 0
        tt = pt[active]
        acc = np.zeros(tt.shape, dtype=np.float32)
        for f in freqs:
            for det in (-0.35, 0.35):
                for k in range(1, 5):
                    acc += np.sin(2 * np.pi * (f + det) * k * tt) / (k ** 1.2)
        pad[active] += acc * env[active]
    pad *= (1.0 + 0.08 * np.sin(2 * np.pi * 0.25 * pt))
    pad = np.interp(np.arange(n) / SR, pt, pad).astype(np.float32)
    pad /= max(1e-6, np.max(np.abs(pad)))
    pad *= 0.8

    drums = np.zeros(n, dtype=np.float32)
    rng = np.random.default_rng(7)

    def hit(at: float, amp: float, decay: float = 7.0, length: float = 0.5) -> None:
        s = int(at * SR)
        m = int(length * SR)
        if s >= n:
            return
        m = min(m, n - s)
        tt = np.arange(m, dtype=np.float32) / SR
        freq = 45 + 60 * np.exp(-tt * 18)
        sig = np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-tt * decay)
        sig += rng.standard_normal(m).astype(np.float32) * np.exp(-tt * 40) * 0.25
        drums[s:s + m] += sig.astype(np.float32) * amp

    schedule = [(5, 10, 1.5, 0.35), (10, 16, 0.75, 0.55), (16, 22, 0.375, 0.75), (22, 27, 1.5, 0.5)]
    for a, b, step, amp in schedule:
        t = float(a)
        j = 0
        while t < b:
            hit(t, amp * (1.0 if j % 2 == 0 else 0.7))
            t += step
            j += 1
    hit(27.0, 1.0, decay=2.5, length=2.8)
    drums /= max(1e-6, np.max(np.abs(drums)))
    drums *= 0.9

    mix = pad * 0.75 + drums * 0.65
    mix /= max(1e-6, np.max(np.abs(mix)))
    return (mix * 0.9).astype(np.float32)


def load_music(work: Path, warnings: list) -> np.ndarray:
    mp = Path(MUSIC_PATH)
    if mp.exists():
        try:
            out = work / "music_user.wav"
            fade_out_start = max(0, DURATION - 1.5)
            run_cmd(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1",
                     "-i", str(mp), "-t", str(DURATION), "-ar", str(SR), "-ac", "1",
                     "-af", f"afade=t=in:d=1,afade=t=out:st={fade_out_start}:d=1.5",
                     "-c:a", "pcm_s16le", str(out)], "Musiqa faylini tayyorlash")
            x = read_wav(out)
            log.info("Musiqa: %s", mp)
            return x
        except Exception as e:
            warnings.append(f"Musiqa fayli ishlatilmadi ({e}); sintez qilingan musiqa ishlatiladi.")
    else:
        log.info("assets/music.mp3 yo‘q — musiqa sintez qilinadi.")
    return synth_music(DURATION)


def mix_audio(clips: dict, out_wav: Path, work: Path, warnings: list) -> None:
    n = DURATION * SR
    voice = np.zeros(n, dtype=np.float32)
    for clip in clips.values():
        x = read_wav(clip.path)
        peak = float(np.max(np.abs(x))) if len(x) else 0.0
        if peak > 0:
            x = x * (0.85 / peak)
        s = int(clip.start * SR)
        e = min(n, s + len(x))
        voice[s:e] += x[: e - s]
    voice *= VOICE_GAIN

    music = load_music(work, warnings)
    if len(music) < n:
        music = np.pad(music, (0, n - len(music)))
    music = music[:n].astype(np.float32)

    env = moving_avg(np.abs(voice), int(SR * 0.15))
    gate = np.clip(env / 0.05, 0.0, 1.0)
    gate = moving_avg(gate, int(SR * 0.30))
    music_gain = MUSIC_VOLUME * (1.0 - DUCK_AMOUNT * gate)
    mixed = voice + music * music_gain.astype(np.float32)

    fi, fo = int(0.5 * SR), int(0.8 * SR)
    mixed[:fi] *= np.linspace(0, 1, fi, dtype=np.float32)
    mixed[-fo:] *= np.linspace(1, 0, fo, dtype=np.float32)
    peak = float(np.max(np.abs(mixed)))
    if peak > 0.95:
        mixed *= 0.95 / peak
    write_wav(out_wav, mixed)


# ----------------------------------------------------------------------------
# 5) VIDEO (Pillow kadrlari → FFmpeg)
# ----------------------------------------------------------------------------
def grade(img: Image.Image) -> Image.Image:
    img = ImageOps.autocontrast(img, cutoff=1)
    img = ImageEnhance.Color(img).enhance(0.86)
    img = ImageEnhance.Contrast(img).enhance(1.08)
    r, g, b = img.split()
    r = r.point(lambda v: min(255, int(v * 1.05)))
    b = b.point(lambda v: int(v * 0.92))
    return Image.merge("RGB", (r, g, b))


class KenBurns:
    def __init__(self, path: Path, motion: tuple):
        img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        scale = max(W / img.width, H / img.height)
        size = (max(W, round(img.width * scale)), max(H, round(img.height * scale)))
        self.img = grade(img.resize(size, Image.Resampling.LANCZOS))
        self.z0, self.z1, self.px0, self.px1 = motion

    def frame(self, p: float) -> Image.Image:
        p = min(1.0, max(0.0, p))
        e = p * p * (3 - 2 * p)  # yumshoq harakat
        z = max(1.0, self.z0 + (self.z1 - self.z0) * e)
        cw, ch = W / z, H / z
        max_x, max_y = self.img.width - cw, self.img.height - ch
        fx = self.px0 + (self.px1 - self.px0) * e
        x0 = min(max_x, max(0.0, max_x * fx))
        y0 = max_y * 0.5
        return self.img.resize((W, H), Image.Resampling.BILINEAR, box=(x0, y0, x0 + cw, y0 + ch))


def make_vignette() -> np.ndarray:
    yy, xx = np.ogrid[:H, :W]
    r = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2)
    v = 1.0 - 0.5 * np.clip((r - 0.4) / 0.9, 0, 1) ** 2
    return (v * 256).astype(np.uint16)[:, :, None]


def render_video(image_paths: dict, cues: list, audio_wav: Path, out_path: Path,
                 work: Path, progress: ProgressFn) -> None:
    total = DURATION * FPS
    vig = make_vignette()
    black = Image.new("RGB", (W, H), (0, 0, 0))
    sub_font = load_font(int(64 * SCALE))
    sub_layers = {i: make_subtitle_layer(c.text, sub_font) for i, c in enumerate(cues) if c.on_video}
    title_layer = make_title_layer()
    title_scene = SCENES[-1]
    sub_bottom = int(H * SUB_BOTTOM_RATIO)

    visuals: dict = {}

    def visual(si: int) -> KenBurns:
        for k in [k for k in visuals if k < si - 1]:
            del visuals[k]
        if si not in visuals:
            visuals[si] = KenBurns(image_paths[SCENES[si].idx], SCENES[si].motion)
        return visuals[si]

    def scene_frame(si: int, t: float) -> Image.Image:
        sc = SCENES[si]
        f = visual(si).frame((t - sc.start) / sc.duration)
        if sc.is_title:
            f = Image.blend(f, black, 0.45)
        return f

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", str(audio_wav), "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "libx264", "-preset", X264_PRESET, "-crf", X264_CRF,
           "-pix_fmt", "yuv420p", "-profile:v", "high", "-movflags", "+faststart",
           "-c:a", "aac", "-b:a", "192k", "-ar", str(SR), "-ac", "2",
           "-t", str(DURATION), str(out_path)]
    log_path = work / "ffmpeg_video.log"
    with open(log_path, "wb") as logf:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=logf)
        try:
            for n in range(total):
                t = n / FPS
                si = next((i for i, s in enumerate(SCENES) if s.start <= t < s.end), len(SCENES) - 1)
                frame = scene_frame(si, t)
                sc = SCENES[si]
                if si + 1 < len(SCENES) and t > sc.end - XFADE:
                    a = (t - (sc.end - XFADE)) / XFADE
                    frame = Image.blend(frame, scene_frame(si + 1, SCENES[si + 1].start), min(1.0, a))

                arr = np.asarray(frame, dtype=np.uint8)
                arr = ((arr.astype(np.uint16) * vig) >> 8).astype(np.uint8)
                frame = Image.fromarray(arr, "RGB")

                if t >= title_scene.start:
                    local = t - title_scene.start - 0.3
                    alpha = min(1.0, max(0.0, local / 0.8))
                    if alpha > 0:
                        lay = title_layer.copy()
                        lay.putalpha(lay.getchannel("A").point(lambda v, a=alpha: int(v * a)))
                        frame.paste(lay, (0, 0), lay)
                else:
                    for i, c in enumerate(cues):
                        if c.on_video and c.start <= t < c.end:
                            lay = sub_layers.get(i)
                            if lay is not None:
                                frame.paste(lay, (0, max(0, sub_bottom - lay.height)), lay)
                            break

                fade = min(1.0, t / 0.5, (DURATION - t) / 0.4 if t > DURATION - 0.4 else 1.0)
                if fade < 1.0:
                    frame = Image.blend(black, frame, max(0.0, fade))

                proc.stdin.write(frame.tobytes())
                if n % FPS == 0:
                    progress(36 + 58 * n / total, f"Video kadrlari: {n}/{total}")
            proc.stdin.close()
        except BrokenPipeError:
            pass
        except Exception:
            proc.kill()
            raise
        rc = proc.wait()
    if rc != 0:
        tail = log_path.read_text(errors="ignore")[-800:]
        raise RuntimeError(f"FFmpeg kodlash xatosi (kod {rc}): {tail}")


# ----------------------------------------------------------------------------
# 6) PIPELINE
# ----------------------------------------------------------------------------
def run_pipeline(progress: ProgressFn = lambda p, m: None) -> dict:
    t0 = time.time()
    warnings: list = []
    check_tools()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    work = CACHE_DIR / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    log.info("Boshlandi: %dx%d @ %dfps, preview=%s", W, H, FPS, PREVIEW)

    image_paths, credits = fetch_images(warnings, progress)
    clips = build_voice(work, warnings, progress)

    progress(31, "Subtitrlar tayyorlanmoqda")
    cues = build_cues(clips)
    write_srt(cues, OUTPUT_SRT)

    progress(33, "Audio aralashtirilmoqda (musiqa sintezi biroz vaqt oladi)")
    audio_wav = work / "mix.wav"
    mix_audio(clips, audio_wav, work, warnings)

    tmp_mp4 = work / "out.mp4"
    render_video(image_paths, cues, audio_wav, tmp_mp4, work, progress)

    dur = probe_duration(tmp_mp4)
    if dur > 0 and abs(dur - DURATION) > 0.15:
        warnings.append(f"Video davomiyligi {dur:.2f}s (kutilgan {DURATION}s).")
    os.replace(tmp_mp4, OUTPUT_MP4)
    OUTPUT_CREDITS.write_text(json.dumps(credits, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)

    progress(100, "Tayyor")
    result = {"duration": dur, "seconds_taken": round(time.time() - t0, 1), "warnings": warnings}
    log.info("Tayyor: %s | %.2fs | %.0f soniyada | ogohlantirishlar: %d",
             OUTPUT_MP4, dur, result["seconds_taken"], len(warnings))
    return result


# ----------------------------------------------------------------------------
# FASTAPI
# ----------------------------------------------------------------------------
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse  # noqa: E402

JOB_LOCK = threading.Lock()
JOB: dict = {"state": "idle", "progress": 0, "message": "Kutilmoqda", "warnings": [],
             "error": None, "started": None, "finished": None, "result": None}


def _set(**kw) -> None:
    with JOB_LOCK:
        JOB.update(kw)


def _job_main() -> None:
    try:
        res = run_pipeline(lambda p, m: _set(progress=round(p, 1), message=m))
        _set(state="done", progress=100, message="Tayyor", warnings=res["warnings"],
             result=res, finished=time.time())
    except Exception as e:
        log.exception("Pipeline xatosi")
        # Browserda asosiy xato ko'rinadi; to'liq traceback Render Runtime Logs'da qoladi.
        _set(state="error", error=f"{type(e).__name__}: {e}", message="Xato", finished=time.time())


def start_job() -> dict:
    with JOB_LOCK:
        if JOB["state"] == "running":
            return dict(JOB)
        JOB.update(state="running", progress=0, message="Boshlanmoqda", warnings=[], error=None,
                   started=time.time(), finished=None, result=None)
    threading.Thread(target=_job_main, daemon=True).start()
    with JOB_LOCK:
        return dict(JOB)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if os.getenv("GENERATE_ON_START", "0") == "1" and not OUTPUT_MP4.exists():
        log.info("GENERATE_ON_START=1: video yo‘q, yaratish boshlandi")
        start_job()
    yield


app = FastAPI(title="Amir Temur video generatori", lifespan=lifespan)

PAGE = """<!doctype html><html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Amir Temur video</title>
<style>body{font-family:system-ui,sans-serif;background:#111;color:#eee;max-width:640px;margin:2rem auto;padding:0 1rem}
button,a.b{background:#c9a227;color:#111;border:0;padding:.7rem 1.1rem;border-radius:8px;font-size:1rem;cursor:pointer;text-decoration:none;display:inline-block;margin:.3rem .3rem .3rem 0}
progress{width:100%;height:1.2rem}pre{white-space:pre-wrap;background:#1c1c1c;padding:.7rem;border-radius:8px}</style></head><body>
<h1>Amir Temur — 30 soniyalik video</h1>
<button id="go">Videoni yaratish</button>
<p><progress id="p" max="100" value="0"></progress></p><p id="m">Kutilmoqda…</p><pre id="w"></pre>
<div id="dl" style="display:none">
<a class="b" href="/download" download>MP4 yuklab olish</a><a class="b" href="/subtitles.srt">SRT</a><a class="b" href="/credits.json">Tasvir manbalari</a></div>
<p>Jarayon tugaguncha bu sahifani yopmang (bepul Render serveri uxlab qolmasligi uchun).</p>
<script>
let token=localStorage.getItem('tok')||'';
async function poll(){
 try {
  const response = await fetch('/status');
  if(!response.ok) throw new Error('Status HTTP '+response.status);
  const s=await response.json();
  document.getElementById('p').value=s.progress||0;
  document.getElementById('m').textContent=s.state+' — '+s.message+(s.error?(' | XATO: '+s.error):'');
  document.getElementById('w').textContent=(s.warnings||[]).join('\\n');
  document.getElementById('dl').style.display=s.file_ready?'block':'none';
  if(s.state==='running')setTimeout(poll,3000);
 } catch(e) {
  document.getElementById('m').textContent='Server bilan aloqa uzildi: '+e.message;
  setTimeout(poll,5000);
 }
}
document.getElementById('go').onclick=async()=>{
 let r=await fetch('/generate',{method:'POST',headers:{'x-api-token':token}});
 if(r.status===401){token=prompt('API_TOKEN:')||'';localStorage.setItem('tok',token);
  r=await fetch('/generate',{method:'POST',headers:{'x-api-token':token}});}
 if(!r.ok){alert('Xato: '+r.status);return;} poll();};
poll();
</script></body></html>"""


def _auth(request: Request) -> None:
    if not API_TOKEN:
        return
    got = request.headers.get("x-api-token") or request.query_params.get("token", "")
    if not secrets.compare_digest(got, API_TOKEN):
        raise HTTPException(status_code=401, detail="API token noto‘g‘ri")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/generate")
def generate(request: Request) -> dict:
    _auth(request)
    return start_job()


@app.get("/status")
def status() -> dict:
    with JOB_LOCK:
        s = dict(JOB)
    s["file_ready"] = OUTPUT_MP4.exists()
    return s


@app.get("/download")
def download():
    if not OUTPUT_MP4.exists():
        raise HTTPException(404, "Video hali yaratilmagan. Avval POST /generate qiling.")
    return FileResponse(OUTPUT_MP4, media_type="video/mp4", filename="amir_temur.mp4")


@app.get("/subtitles.srt")
def subtitles():
    if not OUTPUT_SRT.exists():
        raise HTTPException(404, "SRT hali yaratilmagan.")
    return FileResponse(OUTPUT_SRT, media_type="application/x-subrip", filename="amir_temur.srt")


@app.get("/credits.json")
def credits_file():
    if not OUTPUT_CREDITS.exists():
        raise HTTPException(404, "Manbalar hali yaratilmagan.")
    return FileResponse(OUTPUT_CREDITS, media_type="application/json")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _cli_progress(p: float, m: str) -> None:
    log.info("[%5.1f%%] %s", p, m)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", action="store_true", help="serversiz, bevosita video yaratish")
    args = ap.parse_args()
    if args.cli:
        try:
            r = run_pipeline(_cli_progress)
            print(json.dumps(r, ensure_ascii=False, indent=2))
        except Exception as exc:
            log.error("XATO: %s", exc)
            sys.exit(1)
    else:
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=_env_int("PORT", 10000))