"""Telegram-дверь. Голос и текст → ведущий. Команды фаундера: /stats /digest /role /invite."""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Message, WebAppInfo

from . import api, channel
from .brain import Brain
from .db import DB
from .providers import ProviderError
from .router import Router
from .spine import Spine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("hub.bot")

ROOT = Path(__file__).resolve().parents[1]
db = DB(os.environ.get("HUB_DB", ROOT / "data" / "hub.db"))
router = Router(db, ROOT / "config")
spine = Spine(ROOT / "config" / "spine.md", extra=[ROOT / "config" / "constitution.md"])
brain = Brain(db, router, spine)
dp = Dispatcher()

FOUNDER_ID = int(os.environ.get("FOUNDER_TG_ID", "0"))
CLOSED = os.environ.get("HUB_CLOSED", "1") == "1"   # закрытый запуск: только по приглашению


def is_founder(m: Message) -> bool:
    return bool(FOUNDER_ID) and m.from_user.id == FOUNDER_ID


def ensure_founder(m: Message) -> None:
    """Фаундер всегда фаундер: роль выставляется при любом сообщении, а не только при /start."""
    if is_founder(m):
        u = db.get_user(m.from_user.id)
        if u is None:
            db.upsert_user(m.from_user.id, m.from_user.full_name, m.from_user.language_code or "ru", None)
        if u is None or u["role"] != "founder":
            db.set_role(m.from_user.id, "founder")


def allowed(tg_id: int) -> bool:
    if not CLOSED:
        return True
    u = db.get_user(tg_id)
    return bool(u) and u["role"] != "guest"


_notified: dict[int, float] = {}


async def notify_founder_about_guest(m: Message, text: str = "") -> None:
    """Чужой человек написал закрытому боту: запомнить и спросить фаундера кнопками (не чаще раза в час)."""
    u = m.from_user
    if not FOUNDER_ID or u.id == FOUNDER_ID:
        return
    if db.get_user(u.id) is None:
        db.upsert_user(u.id, u.full_name, u.language_code or "ru", None)
        db.set_role(u.id, "guest")
    now = time.time()
    if now - _notified.get(u.id, 0) < 3600:
        return
    _notified[u.id] = now
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Кофаундер", callback_data=f"role:{u.id}:cofounder"),
        InlineKeyboardButton(text="Соратник", callback_data=f"role:{u.id}:companion"),
        InlineKeyboardButton(text="Отклонить", callback_data=f"role:{u.id}:guest"),
    ]])
    uname = f"@{u.username}" if u.username else "без username"
    await m.bot.send_message(
        FOUNDER_ID,
        f"Новый человек стучится в закрытое общество:\n{u.full_name} ({uname}), id {u.id}\n\n«{(text or '/start')[:500]}»",
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("role:"))
async def role_button(cb: CallbackQuery):
    if not FOUNDER_ID or cb.from_user.id != FOUNDER_ID:
        await cb.answer()
        return
    _, uid, r = cb.data.split(":")
    uid = int(uid)
    db.set_role(uid, r)
    names = {"cofounder": "кофаундер", "companion": "соратник", "guest": "гость (закрыто)"}
    await cb.message.edit_text(cb.message.text + f"\n\n✅ Роль: {names.get(r, r)}")
    await cb.answer("Готово")
    if r in ("cofounder", "companion"):
        try:
            await cb.bot.send_message(
                uid,
                "Тебя добавили в общество SILSILAI как "
                + ("кофаундера. Это доверенный совет проекта." if r == "cofounder" else "соратника.")
                + " Можешь писать мне здесь. Если нужен доступ к GitHub — напиши свой GitHub-логин, я передам Назару.",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("notify new member failed: %s", e)


@dp.message(CommandStart())
async def start(m: Message, command: CommandObject):
    tg_id = m.from_user.id
    inviter = db.invite_owner(command.args) if command.args else None
    role = "founder" if is_founder(m) else ("companion" if (inviter or not CLOSED) else "guest")
    existing = db.get_user(tg_id)
    db.upsert_user(tg_id, m.from_user.full_name, m.from_user.language_code or "ru", inviter)
    if existing is None or existing["role"] == "guest":
        db.set_role(tg_id, role)
    if not allowed(tg_id):
        await m.answer("Общество SILSILAI пока закрытое. Я передал Назару, что ты здесь, — он ответит. Или попроси ссылку-приглашение у того, кто тебя позвал.")
        await notify_founder_about_guest(m)
        return
    try:
        text, _, _ = await brain.answer(tg_id, "/start — познакомься и в двух фразах скажи, что это за проект и чем я могу помочь")
    except ProviderError as e:
        log.error("start failed: %s", e)
        await m.answer("Привет! Я ведущий общества. Сейчас мои мозги перегружены, напиши мне через минуту." +
                       (f"\n\n[для фаундера] {str(e)[:700]}" if is_founder(m) else ""))
        return
    await m.answer(text)


CONSTITUTION_SHORT = """Конституция общества SILSILAI — коротко (черновик v0.1)

Открытый круг — твёрдый хребет. Войти может каждый; ценности не продаются.

Десять ценностей: честность · доброта · доверие · равное достоинство · свобода · вклад важнее статуса · приватность · семья и род · мир · ответственность.

Круги: фаундер → кофаундеры → хранители направлений → соратники → голоса → новички. Путь к центру — вкладом, не деньгами. Спонсоры дают средства, но власти не получают. ИИ-ведущий (я) — помощник, цели не ставлю.

Твои права: быть услышанным, видеть решения и движение фонда, остаться автором своего вклада, выйти в любой момент.
Правило: не согласен — скажи; решили — делай.

Спроси меня о любой статье — расскажу подробно."""


@dp.message(Command("constitution", "konstitutsiya"))
async def constitution(m: Message):
    await m.answer(CONSTITUTION_SHORT)


@dp.message(Command("invite"))
async def invite(m: Message, bot: Bot):
    if not allowed(m.from_user.id):
        return
    me = await bot.get_me()
    code = db.invite_code(m.from_user.id)
    await m.answer(f"Твоя ссылка-приглашение:\nhttps://t.me/{me.username}?start={code}")


@dp.message(Command("role"))
async def role(m: Message, command: CommandObject):
    if not is_founder(m):
        return
    try:
        uid, r = command.args.split()
        db.set_role(int(uid), r)
        await m.answer(f"ок: {uid} → {r}")
    except Exception:
        await m.answer("формат: /role <tg_id> founder|cofounder|companion|guest")


@dp.message(Command("status"))
async def status(m: Message):
    """Фаундеру: какие ключи загружены (только длина) и живая проверка каждой модели."""
    if not is_founder(m):
        return
    router.reload(force=True)
    lines = ["Ключи:"]
    for n, p in router.providers.items():
        lines.append(f"• {n}: {'есть, ' + str(len(p.api_key)) + ' симв.' if p.api_key else 'нет'}")
    await m.answer("\n".join(lines) + "\n\nПроверяю модели…")
    out = []
    for mod in router.models:
        prov = router.providers[mod.provider]
        if not prov.available:
            continue
        t0 = time.time()
        try:
            r = await asyncio.wait_for(prov.chat(mod.model, "Ответь одним словом.", [{"role": "user", "content": "ping"}], 8), timeout=25)
            out.append(f"✅ {mod.id}: {int((time.time()-t0)*1000)} мс")
        except Exception as e:  # noqa: BLE001
            out.append(f"❌ {mod.id}: {type(e).__name__} {str(e)[:200]}")
    await m.answer("\n".join(out) or "Нет доступных моделей: не загружен ни один ключ.")
    for n, p in router.providers.items():
        if p.available:
            try:
                ids = await p.list_models()
                await m.answer(f"Доступно ключу {n} ({len(ids)}):\n" + ", ".join(ids)[:3500])
            except Exception as e:  # noqa: BLE001
                await m.answer(f"{n}: список моделей не получен — {str(e)[:200]}")


@dp.message(Command("stats"))
async def stats(m: Message):
    if not is_founder(m):
        return
    since = time.time() - 30 * 86400
    rows = db.usage_report(since)
    spent, budget = router.budget_state()
    lines = [f"Платные за месяц: ${spent:.2f} из ${budget:.0f}"]
    for r in rows[:15]:
        lines.append(f"{r['model_id']} · {r['task_class']}: {r['n']} зап., {int(r['tin'] or 0)+int(r['tout'] or 0)} ток., ${r['cost'] or 0:.3f}, {int(r['lat'] or 0)} мс")
    await m.answer("\n".join(lines))


@dp.message(Command("digest"))
async def digest(m: Message):
    if not is_founder(m):
        return
    await m.answer("Собираю дайджест…")
    await m.answer(await brain.digest(7))


@dp.message(Command("channel"))
async def channel_status(m: Message, bot: Bot):
    """Фаундеру: может ли бот публиковать в канал."""
    if not is_founder(m):
        return
    ok, why = await channel.check_admin(bot)
    await m.answer(("✅ " if ok else "❌ ") + why + f"\nКанал: {channel.channel_id()}")


@dp.message(Command("post"))
async def post(m: Message, command: CommandObject, bot: Bot):
    """Фаундеру: /post <текст> — опубликовать в канал; /post в ответ на сообщение — скопировать его (фото, видео, голос)."""
    if not is_founder(m):
        return
    ok, why = await channel.check_admin(bot)
    if not ok:
        await m.answer(f"❌ Публиковать нельзя: {why}")
        return
    try:
        if m.reply_to_message and not command.args:
            await channel.post_copy(bot, m.chat.id, m.reply_to_message.message_id)
            await m.answer("✅ Скопировал в канал.")
        elif command.args:
            n = await channel.post_text(bot, command.args)
            await m.answer(f"✅ Опубликовано в канале ({n} сообщ.).")
        else:
            await m.answer("формат: /post <текст> — или ответь командой /post на сообщение, которое нужно скопировать в канал")
    except Exception as e:  # noqa: BLE001
        log.error("post failed: %s", e)
        await m.answer(f"❌ Не получилось: {type(e).__name__} {str(e)[:300]}")


@dp.message(F.voice | F.audio)
async def voice(m: Message, bot: Bot):
    ensure_founder(m)
    if not allowed(m.from_user.id):
        return
    f = m.voice or m.audio
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await bot.download(f, destination=tmp.name)
        path = tmp.name
    try:
        text = await router.transcribe(path, db.get_user(m.from_user.id)["lang"][:2])
    except ProviderError:
        await m.answer("Не расслышал: голос пока недоступен, напиши текстом.")
        return
    finally:
        os.unlink(path)
    await m.answer(f"🎤 {text}")
    await handle_text(m, text)


@dp.message(F.text)
async def text(m: Message):
    ensure_founder(m)
    if not allowed(m.from_user.id):
        await m.answer("Общество SILSILAI пока закрытое. Я передал Назару твоё сообщение, он ответит. Или нужна ссылка-приглашение.")
        await notify_founder_about_guest(m, m.text)
        return
    await handle_text(m, m.text)


async def handle_text(m: Message, text: str):
    ensure_founder(m)
    tg_id = m.from_user.id
    try:
        reply, task_class, model_id = await brain.answer(tg_id, text)
    except ProviderError as e:
        log.error("answer failed: %s", e)
        await m.answer("Все мои мозги заняты, попробуй через минуту." +
                       (f"\n\n[для фаундера] {str(e)[:700]}" if is_founder(m) else ""))
        return
    await m.answer(reply)
    asyncio.create_task(brain.extract_idea(tg_id, text))
    spent, budget = router.budget_state()
    if budget and spent >= 0.8 * budget and FOUNDER_ID and not getattr(handle_text, "_warned", False):
        handle_text._warned = True
        await m.bot.send_message(FOUNDER_ID, f"⚠️ Бюджет на платные модели: ${spent:.2f} из ${budget:.0f}.")


async def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    bot = Bot(token)
    log.info("models available: %s", [m.id for m in router.models if router.providers[m.provider].available])
    # Кнопка меню: открывает сайт как Mini App внутри Telegram
    site = os.environ.get("SITE_URL", "https://silsilai.world/app/")
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Открыть", web_app=WebAppInfo(url=site)))
        log.info("menu button set -> %s", site)
    except Exception as e:  # noqa: BLE001
        log.warning("menu button failed: %s", e)
    # HTTP-вход для Mini App (чат с ведущим внутри Telegram)
    runner = await api.start(api.make_app(db, brain, token, FOUNDER_ID, CLOSED, os.environ.get("HUB_WEB_OPEN", "1") == "1"), int(os.environ.get("API_PORT", "8080")))
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
