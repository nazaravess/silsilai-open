"""Публикация в Telegram-канал общества от имени бота."""
from __future__ import annotations

import os

# Канал по умолчанию — публичный t.me/silsilaiworld. Можно задать числовой id через CHANNEL_ID.
DEFAULT_CHANNEL = "@silsilaiworld"
MAX_LEN = 4096  # лимит Telegram на одно сообщение


def channel_id() -> str | int:
    raw = os.environ.get("CHANNEL_ID", "").strip() or DEFAULT_CHANNEL
    try:
        return int(raw)
    except ValueError:
        return raw if raw.startswith("@") else "@" + raw


def split_text(text: str, limit: int = MAX_LEN) -> list[str]:
    """Режем длинный текст по абзацам/строкам, чтобы не упереться в лимит."""
    text = text.strip()
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


async def check_admin(bot) -> tuple[bool, str]:
    """Бот админ канала и может ли публиковать? Возвращает (ок, пояснение)."""
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(channel_id(), me.id)
    except Exception as e:  # noqa: BLE001
        return False, f"не вижу канал: {type(e).__name__} {str(e)[:200]}"
    if member.status == "creator":
        return True, "бот — владелец канала"
    if member.status != "administrator":
        return False, f"статус бота в канале: {member.status} (нужен администратор)"
    if getattr(member, "can_post_messages", False):
        return True, "бот — админ, публиковать может"
    return False, "бот — админ, но без права «Публикация сообщений»"


async def post_text(bot, text: str) -> int:
    """Публикует текст (разбивает на части при необходимости). Возвращает число сообщений."""
    chunks = split_text(text)
    for chunk in chunks:
        await bot.send_message(channel_id(), chunk, disable_web_page_preview=False)
    return len(chunks)


async def post_copy(bot, from_chat_id: int, message_id: int) -> None:
    """Копирует любое сообщение (фото, видео, голос) в канал без пометки «переслано»."""
    await bot.copy_message(channel_id(), from_chat_id, message_id)
