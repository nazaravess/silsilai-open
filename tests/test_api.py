import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

from hub import api
from hub.db import DB

TOKEN = "123456:TESTTOKEN_abcdefghijklmnopqrstuvwxyz012"


def sign(user_id=42, age=0, token=TOKEN, tamper=False):
    d = {"auth_date": str(int(time.time()) - age), "query_id": "q1",
         "user": json.dumps({"id": user_id, "first_name": "Али", "language_code": "uz"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    d["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if tamper:
        d["user"] = d["user"].replace("42", "43")
    return urlencode(d)


def test_verify():
    assert api.verify_init_data(sign(), TOKEN)["id"] == 42
    assert api.verify_init_data(sign(), "999:other") is None
    assert api.verify_init_data(sign(tamper=True), TOKEN) is None
    assert api.verify_init_data(sign(age=90000), TOKEN) is None
    assert api.verify_init_data("", TOKEN) is None


class FakeBrain:
    def __init__(self):
        self.calls = []

    async def answer(self, tg_id, text):
        self.calls.append((tg_id, text))
        return "ответ", "chat", "m"

    async def extract_idea(self, tg_id, text):
        return None


def call(tmp_path, payload, closed, founder=1, brain=None):
    brain = brain or FakeBrain()

    async def go():
        app = api.make_app(DB(tmp_path / "t.db"), brain, TOKEN, founder, closed)
        async with TestClient(TestServer(app)) as c:
            r = await c.post("/api/chat", json=payload)
            return r.status, await r.json()
    return asyncio.run(go()), brain


def test_open_mode_answers(tmp_path):
    (st, js), b = call(tmp_path, {"initData": sign(), "text": "привет"}, closed=False)
    assert st == 200 and js["reply"] == "ответ" and b.calls == [(42, "привет")]


def test_closed_mode_blocks_stranger_but_not_founder(tmp_path):
    (st, js), b = call(tmp_path, {"initData": sign(), "text": "привет"}, closed=True)
    assert js.get("closed") and b.calls == []
    (st, js), b = call(tmp_path, {"initData": sign(user_id=1), "text": "привет"}, closed=True, founder=1)
    assert js["reply"] == "ответ"


def test_rejects_bad_signature_and_empty(tmp_path):
    (st, _), _ = call(tmp_path, {"initData": sign(tamper=True), "text": "x"}, closed=False)
    assert st == 401
    (st, _), _ = call(tmp_path, {"initData": sign(), "text": "  "}, closed=False)
    assert st == 400


def test_web_guest_chats_even_when_closed(tmp_path):
    sid = "browser-random-id-123456"
    (st, js), b = call(tmp_path, {"web": sid, "text": "привет"}, closed=True)
    assert st == 200 and js["reply"] == "ответ" and b.calls[0][0] < 0
    (st, _), _ = call(tmp_path, {"web": "short", "text": "привет"}, closed=True)
    assert st == 401


def test_web_daily_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "WEB_PER_USER_DAY", 2)
    brain = FakeBrain()

    async def go():
        app = api.make_app(DB(tmp_path / "t.db"), brain, TOKEN, 1, False)
        async with TestClient(TestServer(app)) as c:
            out = []
            for _ in range(3):
                r = await c.post("/api/chat", json={"web": "browser-random-id-123456", "text": "а"})
                out.append(await r.json())
            return out
    out = asyncio.run(go())
    assert out[0]["reply"] == "ответ" and out[2].get("limit") is True and len(brain.calls) == 2


def test_voice_message_is_transcribed(tmp_path):
    import base64

    class R:
        async def transcribe(self, path, lang):
            import os
            assert os.path.exists(path) and path.endswith(".webm")
            return "что такое силсилай"

    brain = FakeBrain()
    brain.router = R()
    (st, js), b = call(tmp_path, {"web": "browser-random-id-123456", "audio": base64.b64encode(b"x" * 100).decode(), "mime": "audio/webm;codecs=opus"},
                       closed=False, brain=brain)
    assert js["heard"] == "что такое силсилай" and js["reply"] == "ответ" and b.calls[0][1] == "что такое силсилай"


def test_founder_prompt_knows_founder():
    from hub.brain import Brain
    from hub.spine import Spine

    class S:
        summary, retrieve = "хребет", staticmethod(lambda q: "")

    p = Brain(None, None, S()).system_prompt("привет", {"name": "Назар", "role": "founder"})
    assert "ФАУНДЕР" in p and "никаких «передам фаундеру»" in p


def test_dictation_returns_text_without_asking_brain(tmp_path):
    import base64

    class R:
        async def transcribe(self, path, lang):
            return "надиктованный текст"

    brain = FakeBrain()
    brain.router = R()
    (st, js), b = call(tmp_path, {"web": "browser-random-id-123456", "audio": base64.b64encode(b"x" * 50).decode(),
                                  "mime": "audio/webm", "stt_only": True}, closed=False, brain=brain)
    assert js == {"ok": True, "heard": "надиктованный текст"} and b.calls == []
