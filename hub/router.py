"""Диспетчер моделей.
Правило: для класса задачи берём самую дешёвую модель, которая держит планку качества и умеет нужное.
Бесплатные с остатком квоты — первыми. Все кончились — платная, самая дешёвая. Сбой — через 3 с следующая."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .db import DB
from .providers import Provider, ProviderError, Reply

log = logging.getLogger("hub.router")

FALLBACK_TIMEOUT = 8.0   # секунды до перехода к следующей модели (первый запрос с сервера бывает медленным)
MAX_ATTEMPTS = 4


@dataclass
class Model:
    id: str
    provider: str
    model: str
    tier: str
    price_in: float
    price_out: float
    quality: float
    speed: str
    skills: list[str]
    quota_per_day: int | None = None


@dataclass
class Choice:
    model: Model
    reply: Reply
    cost_usd: float
    latency_ms: int
    attempts: list[str] = field(default_factory=list)


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def _month_start() -> float:
    now = dt.datetime.now(dt.timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


class Router:
    def __init__(self, db: DB, config_dir: str | Path, provider_factory=Provider):
        self.db = db
        self.config_dir = Path(config_dir)
        self.provider_factory = provider_factory
        self._loaded = 0.0
        self.models: list[Model] = []
        self.providers: dict[str, Provider] = {}
        self.classes: dict[str, dict] = {}
        self.rules: dict[str, list[str]] = {}
        self.stt: list[dict] = []
        self.budget = 0.0
        self.reload()

    # ---------- конфиг ----------
    def reload(self, force: bool = False):
        if not force and time.time() - self._loaded < 60:
            return
        reg = yaml.safe_load((self.config_dir / "registry.yaml").read_text())
        cls = yaml.safe_load((self.config_dir / "task_classes.yaml").read_text())
        self.providers = {n: self.provider_factory(n, c) for n, c in reg["providers"].items()}
        scores = self.db.scores()
        self.models = []
        for m in reg["models"]:
            mm = Model(
                id=m["id"], provider=m["provider"], model=m["model"], tier=m["tier"],
                price_in=float(m.get("price_in", 0)), price_out=float(m.get("price_out", 0)),
                quality=float(scores.get(m["id"], m.get("quality", 50))),
                speed=m.get("speed", "medium"), skills=list(m.get("skills", ["text"])),
                quota_per_day=m.get("quota_per_day"),
            )
            self.models.append(mm)
        self.stt = reg.get("stt", [])
        self.budget = float(reg.get("budget_usd_per_month", 0))
        self.classes = cls["classes"]
        self.rules = cls.get("rules", {})
        self._loaded = time.time()

    # ---------- классификация ----------
    def classify_fast(self, text: str) -> str | None:
        t = text.lower()
        for name, words in self.rules.items():
            if any(w in t for w in words):
                return name
        return None

    async def classify(self, text: str, tg_id: int = 0) -> str:
        fast = self.classify_fast(text)
        if fast:
            return fast
        names = [n for n in self.classes if n not in ("classify", "exam_judge", "review", "digest")]
        prompt = (
            "Определи класс запроса. Ответь одним словом из списка: " + ", ".join(names) + "."
        )
        try:
            ch = await self.run("classify", prompt, [{"role": "user", "content": text[:500]}], tg_id=tg_id)
            word = re.sub(r"[^a-z_]", "", ch.reply.text.lower().strip())
            if word in names:
                return word
        except ProviderError:
            pass
        return "chat"

    # ---------- выбор ----------
    def candidates(self, task_class: str) -> list[Model]:
        spec = self.classes[task_class]
        need = set(spec.get("skills", ["text"]))
        min_q = float(spec.get("min_quality", 0))
        max_price = spec.get("max_price_out")
        force = spec.get("force_tier")
        day = _today()
        over_budget = self.budget and self.db.spent_since(_month_start()) >= self.budget

        pool = []
        for m in self.models:
            if not self.providers[m.provider].available:
                continue
            if not need.issubset(m.skills):
                continue
            if m.quality < min_q:
                continue
            if force and m.tier != force:
                continue
            if m.tier == "paid":
                if over_budget and not force:
                    continue
                if max_price is not None and m.price_out > float(max_price):
                    continue
            if m.tier == "free" and m.quota_per_day and self.db.quota_used(m.id, day) >= m.quota_per_day:
                continue
            pool.append(m)
        # бесплатные первыми; внутри — дешевле, затем качественнее, затем быстрее
        order = {"fast": 0, "medium": 1, "slow": 2}
        pool.sort(key=lambda m: (m.tier != "free", m.price_out, -m.quality, order.get(m.speed, 1)))
        return pool

    # ---------- исполнение ----------
    async def run(self, task_class: str, system: str, messages: list[dict], tg_id: int = 0,
                  json_mode: bool = False, max_tokens: int | None = None) -> Choice:
        self.reload()
        spec = self.classes[task_class]
        max_tokens = max_tokens or int(spec.get("max_tokens", 500))
        attempts: list[str] = []
        last_err: Exception | None = None
        for m in self.candidates(task_class)[:MAX_ATTEMPTS]:
            prov = self.providers[m.provider]
            t0 = time.time()
            try:
                reply = await asyncio.wait_for(
                    prov.chat(m.model, system, messages, max_tokens, json_mode),
                    timeout=FALLBACK_TIMEOUT if m.speed == "fast" else FALLBACK_TIMEOUT * 3,
                )
            except (ProviderError, asyncio.TimeoutError, OSError) as e:
                last_err = e
                ms = int((time.time() - t0) * 1000)
                attempts.append(f"{m.id}: {type(e).__name__} {e}")
                self.db.log_usage(tg_id, task_class, m.id, 0, 0, 0.0, False, ms)
                log.warning("model %s failed (%s), falling back", m.id, e)
                continue
            ms = int((time.time() - t0) * 1000)
            cost = (reply.tokens_in * m.price_in + reply.tokens_out * m.price_out) / 1e6
            self.db.log_usage(tg_id, task_class, m.id, reply.tokens_in, reply.tokens_out, cost, True, ms)
            if m.tier == "free" and m.quota_per_day:
                self.db.quota_inc(m.id, _today())
            attempts.append(m.id)
            return Choice(m, reply, cost, ms, attempts)
        raise ProviderError(f"no model answered for {task_class}: {attempts or 'no candidates'} ({last_err})")

    async def run_json(self, task_class: str, system: str, messages: list[dict], tg_id: int = 0) -> dict:
        ch = await self.run(task_class, system, messages, tg_id=tg_id, json_mode=True)
        txt = ch.reply.text.strip()
        txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
        try:
            return json.loads(txt)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", txt, re.S)
            return json.loads(m.group(0)) if m else {}

    # ---------- голос ----------
    async def transcribe(self, audio_path: str, lang: str | None = None) -> str:
        for s in self.stt:
            if s["provider"] == "local":
                try:
                    from faster_whisper import WhisperModel  # необязательная зависимость
                except ImportError:
                    continue
                wm = WhisperModel(s["model"], device="cpu", compute_type="int8")
                segs, _ = wm.transcribe(audio_path, language=lang)
                return " ".join(x.text for x in segs).strip()
            prov = self.providers.get(s["provider"])
            if not prov or not prov.available:
                continue
            if s.get("quota_per_day") and self.db.quota_used(s["id"], _today()) >= s["quota_per_day"]:
                continue
            try:
                text = await prov.transcribe(s["model"], audio_path, lang)
                self.db.quota_inc(s["id"], _today())
                return text
            except ProviderError as e:
                log.warning("stt %s failed: %s", s["id"], e)
        raise ProviderError("no speech-to-text available")

    # ---------- бюджет ----------
    def budget_state(self) -> tuple[float, float]:
        return self.db.spent_since(_month_start()), self.budget
