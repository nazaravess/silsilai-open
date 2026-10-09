import asyncio
import os
from pathlib import Path

import pytest

from hub.db import DB
from hub.providers import ProviderError, Reply
from hub.router import Router

CFG = Path(__file__).resolve().parents[1] / "config"


class FakeProvider:
    """Подменный провайдер: отвечает названием модели; модели из FAIL падают."""
    FAIL: set[str] = set()
    SLOW: set[str] = set()
    KEYS = {"groq", "google", "anthropic"}

    def __init__(self, name, cfg, timeout=30.0):
        self.name = name
        self.kind = cfg["kind"]

    @property
    def available(self):
        return self.name in self.KEYS

    async def chat(self, model, system, messages, max_tokens, json_mode=False):
        if model in self.FAIL:
            raise ProviderError("rate_limit")
        if model in self.SLOW:
            await asyncio.sleep(10)
        if json_mode:
            return Reply('{"kind":"idea","topic":"test","text":"ok"}', 10, 5)
        return Reply(f"answer from {model}", 100, 50)


@pytest.fixture
def router(tmp_path):
    FakeProvider.FAIL = set()
    FakeProvider.SLOW = set()
    FakeProvider.KEYS = {"groq", "google", "anthropic"}
    return Router(DB(tmp_path / "t.db"), CFG, provider_factory=FakeProvider)


def run(coro):
    return asyncio.run(coro)


def test_free_first_for_chat(router):
    ch = run(router.run("chat", "sys", [{"role": "user", "content": "hi"}]))
    assert ch.model.tier == "free"
    assert ch.cost_usd == 0


def test_investor_always_paid(router):
    ch = run(router.run("investor", "sys", [{"role": "user", "content": "хочу вложить"}]))
    assert ch.model.tier == "paid"
    assert ch.model.quality >= 85


def test_fallback_on_failure(router):
    first = router.candidates("chat")[0]
    FakeProvider.FAIL = {first.model}
    ch = run(router.run("chat", "sys", [{"role": "user", "content": "hi"}]))
    assert ch.model.id != first.id
    assert len(ch.attempts) == 2


def test_fallback_on_timeout(router):
    first = router.candidates("chat")[0]
    FakeProvider.SLOW = {first.model}
    ch = run(router.run("chat", "sys", [{"role": "user", "content": "hi"}]))
    assert ch.model.id != first.id


def test_quota_exhaustion_moves_on(router):
    from hub.router import _today
    first = router.candidates("chat")[0]
    for _ in range(first.quota_per_day):
        router.db.quota_inc(first.id, _today())
    assert router.candidates("chat")[0].id != first.id


def test_no_key_excludes_provider(router):
    FakeProvider.KEYS = {"anthropic"}
    router.reload(force=True)
    assert all(m.tier == "paid" for m in router.candidates("chat"))


def test_budget_blocks_paid_but_not_forced(router):
    router.db.log_usage(0, "chat", "anthropic/claude-sonnet", 0, 0, 999.0, True, 1)
    assert all(m.tier == "free" for m in router.candidates("chat"))
    assert router.candidates("investor")  # force_tier: paid всё равно работает


def test_exam_score_overrides_quality(router):
    # digest требует quality>=80: по стартовому баллу (75) gemini-flash не проходит → идёт платная
    assert router.candidates("digest")[0].tier == "paid"
    router.db.set_score("google/gemini-2.5-flash", 95)   # экзамен поднял балл
    router.reload(force=True)
    assert router.candidates("digest")[0].id == "google/gemini-2.5-flash"


def test_fast_classify_rules(router):
    assert router.classify_fast("Я хочу вложить деньги в проект") == "investor"
    assert router.classify_fast("/start") == "welcome"
    assert router.classify_fast("просто болтаю") is None


def test_usage_logged(router):
    run(router.run("chat", "sys", [{"role": "user", "content": "hi"}], tg_id=7))
    rep = router.db.usage_report(0)
    assert rep and rep[0]["n"] == 1
