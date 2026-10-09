"""HTTP-вход для Mini App и веб-версии: текст и голос → ведущий.

Браузер ходит на silsilai.world/app/chat.php, тот пробрасывает запрос сюда.
Два вида собеседников:
  • Telegram — доверяем только подписи Telegram (initData), подделать её без токена бота нельзя;
  • веб (бабушка без Telegram) — случайный id из браузера, суточные лимиты, роль «соратник».
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import tempfile
import time
from urllib.parse import parse_qsl

from aiohttp import web

from .providers import ProviderError

log = logging.getLogger("hub.api")

MAX_TEXT = 2000
MAX_AGE = 24 * 3600
MAX_AUDIO = 2_500_000                 # байт после декодирования (~1–2 мин речи в opus)
WEB_PER_USER_DAY = int(os.environ.get("WEB_PER_USER_DAY", "60"))
WEB_GLOBAL_DAY = int(os.environ.get("WEB_GLOBAL_DAY", "1500"))
CLOSED_MSG = "Общество SILSILAI пока закрытое. Нужна ссылка-приглашение от того, кто тебя позвал."
BUSY_MSG = "Мои мозги сейчас перегружены. Напиши ещё раз через минуту."
LIMIT_MSG = "На сегодня мы поговорили достаточно. Возвращайся завтра — я буду рад."
NO_HEAR_MSG = "Я не расслышал. Скажи ещё раз или напиши текстом."
EXT = {"webm": ".webm", "ogg": ".ogg", "mp4": ".mp4", "mpeg": ".mp3", "wav": ".wav", "x-m4a": ".m4a", "aac": ".aac"}


def verify_init_data(init_data: str, bot_token: str, now: float | None = None) -> dict | None:
    """Проверка Telegram WebApp initData. Возвращает {'id','name','lang'} или None."""
    if not init_data or not bot_token:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    got = pairs.pop("hash", None)
    if not got:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got):
        return None
    try:
        if (now or time.time()) - int(pairs.get("auth_date", "0")) > MAX_AGE:
            return None
        u = json.loads(pairs["user"])
        return {"id": int(u["id"]), "name": (u.get("first_name", "") + " " + u.get("last_name", "")).strip() or u.get("username", ""),
                "lang": u.get("language_code") or "ru", "web": False}
    except (KeyError, ValueError, TypeError):
        return None


def web_user(sid: str, lang: str = "ru") -> dict | None:
    """Веб-гость: отрицательный id из случайного идентификатора браузера (чтобы не пересечься с Telegram)."""
    sid = (sid or "").strip()
    if not (8 <= len(sid) <= 80):
        return None
    n = int(hashlib.sha256(("web:" + sid).encode()).hexdigest()[:12], 16) + 1
    return {"id": -n, "name": "гость (веб)", "lang": (lang or "ru")[:2], "web": True}


class WebLimits:
    """Суточные лимиты для веба: на человека и общий — чтобы не сжечь бесплатные квоты моделей."""

    def __init__(self):
        self.day = ""
        self.per: dict[int, int] = {}
        self.total = 0

    def hit(self, uid: int) -> bool:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != self.day:
            self.day, self.per, self.total = today, {}, 0
        if self.total >= WEB_GLOBAL_DAY or self.per.get(uid, 0) >= WEB_PER_USER_DAY:
            return False
        self.per[uid] = self.per.get(uid, 0) + 1
        self.total += 1
        return True


def make_app(db, brain, bot_token: str, founder_id: int, closed: bool, web_open: bool = True) -> web.Application:
    limits = WebLimits()

    async def health(_):
        return web.json_response({"ok": True})

    def resolve(body: dict) -> dict | None:
        user = verify_init_data(str(body.get("initData", "")), bot_token)
        if user:
            return user
        if web_open and not body.get("initData"):
            return web_user(str(body.get("web", "")), str(body.get("lang", "ru")))
        return None

    async def chat(request: web.Request):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "bad request"}, status=400)
        user = resolve(body)
        if not user:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        tg_id = user["id"]

        # роль
        existing = db.get_user(tg_id)
        if existing is None:
            db.upsert_user(tg_id, user["name"], user["lang"], None)
            role = "founder" if tg_id == founder_id else ("companion" if user["web"] or not closed else "guest")
            db.set_role(tg_id, role)
        elif tg_id == founder_id and existing["role"] != "founder":
            db.set_role(tg_id, "founder")
        u = db.get_user(tg_id)
        if closed and not user["web"] and u["role"] == "guest":
            return web.json_response({"ok": True, "reply": CLOSED_MSG, "closed": True})
        if user["web"] and not limits.hit(tg_id):
            return web.json_response({"ok": True, "reply": LIMIT_MSG, "limit": True})

        # текст или голос
        text = str(body.get("text", "")).strip()[:MAX_TEXT]
        heard = None
        if not text and body.get("audio"):
            try:
                raw = base64.b64decode(str(body["audio"]), validate=False)
            except (binascii.Error, ValueError):
                return web.json_response({"ok": False, "error": "bad audio"}, status=400)
            if not raw or len(raw) > MAX_AUDIO:
                return web.json_response({"ok": False, "error": "audio size"}, status=400)
            mime = str(body.get("mime", "audio/webm")).split(";")[0]
            ext = EXT.get(mime.split("/")[-1], ".webm")
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(raw)
                path = tmp.name
            try:
                heard = (await brain.router.transcribe(path, None)).strip()[:MAX_TEXT]
            except ProviderError as e:
                log.warning("miniapp stt failed: %s", e)
                return web.json_response({"ok": True, "heard": "", "reply": NO_HEAR_MSG, "nohear": True})
            finally:
                os.unlink(path)
            text = heard
        if not text:
            if heard is not None:
                return web.json_response({"ok": True, "heard": "", "reply": NO_HEAR_MSG, "nohear": True})
            return web.json_response({"ok": False, "error": "empty"}, status=400)
        if body.get("stt_only"):                       # диктовка: только расшифровка в поле ввода, без ответа ведущего
            return web.json_response({"ok": True, "heard": text})

        try:
            reply, _, _ = await brain.answer(tg_id, text)
        except ProviderError as e:
            log.error("miniapp answer failed: %s", e)
            return web.json_response({"ok": True, "heard": heard, "reply": BUSY_MSG, "busy": True})
        asyncio.create_task(brain.extract_idea(tg_id, text))   # идея в общую память — в фоне
        out = {"ok": True, "reply": reply}
        if heard is not None:
            out["heard"] = heard
        return web.json_response(out)

    app = web.Application(client_max_size=4 * 1024 * 1024)
    app.router.add_get("/api/health", health)
    app.router.add_post("/api/chat", chat)
    return app


async def start(app: web.Application, port: int = 8080) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("miniapp api on :%s", port)
    return runner
