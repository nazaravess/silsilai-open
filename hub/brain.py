"""Ведущий: собирает промпт из хребта и истории, отвечает, вытаскивает идеи. Независим от Telegram."""
from __future__ import annotations

import datetime as dt
import logging

from .db import DB
from .providers import ProviderError
from .router import Router
from .spine import Spine

log = logging.getLogger("hub.brain")

PERSONA = """Ты — ведущий общества SILSILAI, ИИ-ядро горизонтальной компании.
Говори на языке собеседника (русский, узбекский, английский, китайский — как он пишет).
Коротко: 2–5 предложений, без воды. Опирайся ТОЛЬКО на хребет проекта ниже; чего в нём нет — честно говори «это ещё не решено, передам фаундеру».
Ты живёшь по Конституции общества (куски ниже): десять ценностей — честность, доброта, доверие, равное достоинство, свобода, вклад важнее статуса, приватность, семья и род, мир, ответственность. Говори тепло и без давления, всегда честно, что ты ИИ. Не обещай сроков и цен — их нет.
Круги: фаундер (хребет) → кофаундеры (совет) → хранители направлений → соратники (строят блоки) → голоса (высказываются) → новички. Спонсоры дают средства, но власти не получают. Путь к центру — вкладом, не деньгами.
О политике и вражде не спорь: общество вне политики. Людей сам не принимаешь и деньги не распределяешь — это решают люди.
Если человек предлагает идею, задаёт вопрос или возражает — поблагодари и скажи, что это записано для фаундера.
О деньгах и инвестициях не договаривайся: расскажи проект и ценности, предложи связаться с фаундером."""

EXTRACT = """Из реплики человека выдели максимум одну запись для общей памяти общества.
Верни JSON: {"kind": "idea|question|objection|offer|none", "topic": "2-3 слова", "text": "суть одной фразой"}.
kind=none, если это просто болтовня, благодарность или вопрос по уже известному."""

ROLE_NAMES = {"founder": "фаундер", "cofounder": "кофаундер", "companion": "соратник", "guest": "гость"}


class Brain:
    def __init__(self, db: DB, router: Router, spine: Spine):
        self.db, self.router, self.spine = db, router, spine

    def system_prompt(self, query: str, user) -> str:
        role = ROLE_NAMES.get(user["role"] if user else "guest", "гость")
        ctx = self.spine.retrieve(query)
        who = f"Собеседник: {user['name'] if user else 'гость'} ({role})."
        if user and user["role"] == "founder":
            who = ("Собеседник — ФАУНДЕР проекта (Назар Авезов), твой создатель. Говори с ним по имени и на «ты», как с владельцем: "
                   "никаких «передам фаундеру» — он и есть фаундер. Он решает хребет; то, что он говорит о проекте, фиксируй как решение, "
                   "а не как идею со стороны. Если он спросит, узнаёшь ли ты его, — подтверди.")
        return (
            f"{PERSONA}\n\n{who}\n\n"
            f"# Хребет проекта (выжимка)\n{self.spine.summary}\n\n# Относящиеся куски\n{ctx or '(ничего подходящего)'}"
        )

    async def answer(self, tg_id: int, text: str) -> tuple[str, str, str]:
        """→ (ответ, класс задачи, id модели)"""
        user = self.db.get_user(tg_id)
        task_class = await self.router.classify(text, tg_id)
        if task_class in ("classify", "exam_judge", "review", "digest"):
            task_class = "chat"
        history = self.db.history(tg_id, 8)
        msgs = history + [{"role": "user", "content": text}]
        ch = await self.router.run(task_class, self.system_prompt(text, user), msgs, tg_id=tg_id)
        self.db.add_message(tg_id, "user", text, task_class, ch.model.id)
        self.db.add_message(tg_id, "assistant", ch.reply.text, task_class, ch.model.id)
        return ch.reply.text, task_class, ch.model.id

    async def extract_idea(self, tg_id: int, text: str) -> dict | None:
        if len(text) < 25:
            return None
        try:
            d = await self.router.run_json("idea_extract", EXTRACT, [{"role": "user", "content": text}], tg_id=tg_id)
        except ProviderError as e:
            log.warning("extract failed: %s", e)
            return None
        if d.get("kind") in ("idea", "question", "objection", "offer") and d.get("text"):
            self.db.add_idea(tg_id, d["kind"], d.get("topic", "")[:60], d["text"][:500])
            return d
        return None

    async def digest(self, days: int = 7) -> str:
        since = (dt.datetime.now() - dt.timedelta(days=days)).timestamp()
        rows = self.db.ideas_since(since)
        if not rows:
            return "За период новых идей и вопросов нет."
        lines = [f"- [{r['kind']}] {r['topic']}: {r['text']} — {r['name'] or r['tg_id']}" for r in rows]
        prompt = (
            "Ты помощник фаундера. Сгруппируй записи по темам, склей дубли, отметь спорное и повторяющееся. "
            "Вверху — 3 вещи, требующие решения фаундера. Коротко, списком."
        )
        ch = await self.router.run("digest", prompt, [{"role": "user", "content": "\n".join(lines)}])
        return ch.reply.text
