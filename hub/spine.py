"""Хребет проекта: config/spine.md (копия Notion-страницы) режется на куски по заголовкам и пунктам.
В запрос уходят только куски, похожие на вопрос, — так расход режется в 5–10 раз.
Без эмбеддингов: пересечение слов, этого хватает для хребта в 2–3 тысячи слов."""
from __future__ import annotations

import re
from pathlib import Path

_WORD = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9]{3,}")


def _words(t: str) -> set[str]:
    return {w.lower()[:6] for w in _WORD.findall(t)}  # обрезка до 6 букв ≈ грубая лемматизация


class Spine:
    def __init__(self, path: str | Path, extra: list[str | Path] | None = None):
        self.path = Path(path)
        # доп. файлы (конституция и т.п.) — режутся так же, но в «выжимку» не попадают
        self.extra = [Path(x) for x in (extra or [])]
        self.chunks: list[tuple[str, str]] = []   # (заголовок, текст)
        self.summary = ""
        self._mtime = 0.0
        self.reload()

    def reload(self):
        if not self.path.exists():
            return
        files = [self.path] + [x for x in self.extra if x.exists()]
        mt = sum(f.stat().st_mtime for f in files)
        if mt == self._mtime:
            return
        self._mtime = mt
        self.chunks = []
        self._parse(self.path.read_text(encoding="utf-8"))
        # краткая выжимка — первые два блока основного хребта всегда идут в контекст
        self.summary = "\n".join(t for _, t in self.chunks[:2])[:1500]
        for f in files[1:]:
            self._parse(f.read_text(encoding="utf-8"))

    def _parse(self, text: str):
        head = "Общее"
        buf: list[str] = []
        for line in text.splitlines():
            if line.startswith("#"):
                if buf:
                    self._push(head, buf)
                head = line.lstrip("# ").strip()
                buf = []
            elif line.startswith("- ") and buf and sum(len(x) for x in buf) > 400:
                self._push(head, buf)
                buf = [line]
            else:
                buf.append(line)
        if buf:
            self._push(head, buf)

    def _push(self, head, buf):
        t = "\n".join(buf).strip()
        if t:
            self.chunks.append((head, t))

    def retrieve(self, query: str, k: int = 4, max_chars: int = 2500) -> str:
        self.reload()
        q = _words(query)
        scored = []
        for head, t in self.chunks:
            s = len(q & _words(head + " " + t))
            if s:
                scored.append((s, head, t))
        scored.sort(reverse=True)
        out, n = [], 0
        for s, head, t in scored[:k]:
            piece = f"## {head}\n{t}"
            if n + len(piece) > max_chars:
                piece = piece[: max_chars - n]
            out.append(piece)
            n += len(piece)
            if n >= max_chars:
                break
        return "\n\n".join(out)
