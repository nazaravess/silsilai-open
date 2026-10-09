import asyncio
from pathlib import Path

import pytest

from hub.brain import Brain
from hub.db import DB
from hub.router import Router
from hub.spine import Spine
from tests.test_router import FakeProvider

CFG = Path(__file__).resolve().parents[1] / "config"
FIX = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def brain(tmp_path):
    FakeProvider.FAIL = set()
    FakeProvider.SLOW = set()
    FakeProvider.KEYS = {"groq", "google", "anthropic"}
    db = DB(tmp_path / "t.db")
    db.upsert_user(1, "Назар")
    db.set_role(1, "founder")
    return Brain(db, Router(db, CFG, provider_factory=FakeProvider), Spine(FIX / "spine.md"))


def test_spine_retrieval_picks_relevant_chunks(brain):
    ctx = brain.spine.retrieve("что будет если потеряю телефон")
    assert "пропуск" in ctx and "пустое стекло" in ctx
    assert len(ctx) <= 2600


def test_system_prompt_is_compact(brain):
    sp = brain.system_prompt("где хранятся данные", brain.db.get_user(1))
    assert "фаундер" in sp and "Хребет" in sp
    assert len(sp) < 6000   # не весь хребет, только выжимка + куски


def test_answer_records_history_and_routes_investor_to_paid(brain):
    reply, cls, model = asyncio.run(brain.answer(1, "Хочу вложить деньги, какая доля?"))
    assert cls == "investor" and model.startswith("anthropic/")
    assert len(brain.db.history(1)) == 2


def test_idea_extracted_and_digest_built(brain):
    d = asyncio.run(brain.extract_idea(1, "Предлагаю сделать детский профиль с лимитом времени на игры по вечерам"))
    assert d and d["kind"] == "idea"
    assert len(brain.db.ideas_since(0)) == 1
    out = asyncio.run(brain.digest(7))
    assert out.startswith("answer from")


def test_short_messages_not_extracted(brain):
    assert asyncio.run(brain.extract_idea(1, "спасибо")) is None


def test_constitution_is_retrievable():
    s = Spine(FIX / "spine.md", extra=[FIX / "constitution.md"])
    ctx = s.retrieve("кто такие спонсоры и получают ли они власть")
    assert "Спонсор" in ctx
    ctx = s.retrieve("какие красные линии и что будет за нарушение")
    assert "Красные линии" in ctx
    # выжимка — только из основного хребта
    assert "Конституция" not in s.summary.split("\n")[0]
