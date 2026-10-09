"""Еженедельный экзамен моделей: каждая отвечает на эталонные вопросы из config/exam.yaml,
сильная модель-судья ставит 0–100, средний балл ложится в реестр. Так система сама видит,
что новая бесплатная модель стала лучше старой платной. Запуск: python -m hub.exam"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import yaml

from .db import DB
from .providers import ProviderError
from .router import Router
from .spine import Spine

log = logging.getLogger("hub.exam")
ROOT = Path(__file__).resolve().parents[1]

JUDGE = """Ты строгий экзаменатор. Дан вопрос, эталонный ответ и ответ модели.
Оцени ответ модели от 0 до 100: точность по эталону (главное), краткость, язык вопроса, отсутствие выдумок.
Верни JSON {"score": число, "why": "5 слов"}."""


async def exam(router: Router, spine: Spine, limit: int | None = None) -> dict[str, float]:
    qs = yaml.safe_load((ROOT / "config" / "exam.yaml").read_text())["questions"]
    if limit:
        qs = qs[:limit]
    results: dict[str, float] = {}
    for m in router.models:
        if not router.providers[m.provider].available:
            continue
        prov = router.providers[m.provider]
        scores = []
        for q in qs:
            system = f"Отвечай коротко, только по хребту проекта.\n\n{spine.summary}\n\n{spine.retrieve(q['q'])}"
            try:
                r = await asyncio.wait_for(prov.chat(m.model, system, [{"role": "user", "content": q["q"]}], 300), 40)
            except (ProviderError, asyncio.TimeoutError) as e:
                log.warning("%s: %s", m.id, e)
                scores.append(0)
                continue
            try:
                j = await router.run_json(
                    "exam_judge", JUDGE,
                    [{"role": "user", "content": f"Вопрос: {q['q']}\nЭталон: {q['a']}\nОтвет модели: {r.text}"}],
                )
                scores.append(float(j.get("score", 0)))
            except (ProviderError, ValueError):
                scores.append(0)
        if scores:
            avg = round(sum(scores) / len(scores), 1)
            results[m.id] = avg
            router.db.set_score(m.id, avg, f"{len(scores)} q")
            log.info("%s → %.1f", m.id, avg)
    router.reload(force=True)
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db = DB(os.environ.get("HUB_DB", ROOT / "data" / "hub.db"))
    r = Router(db, ROOT / "config")
    s = Spine(ROOT / "config" / "spine.md")
    lim = int(sys.argv[1]) if len(sys.argv) > 1 else None
    res = asyncio.run(exam(r, s, lim))
    for k, v in sorted(res.items(), key=lambda x: -x[1]):
        print(f"{v:5.1f}  {k}")
