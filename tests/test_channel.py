import asyncio
from types import SimpleNamespace

from hub import channel


class FakeBot:
    def __init__(self, status="administrator", can_post=True):
        self.sent = []
        self.copied = []
        self._status, self._can_post = status, can_post

    async def get_me(self):
        return SimpleNamespace(id=1)

    async def get_chat_member(self, chat, uid):
        return SimpleNamespace(status=self._status, can_post_messages=self._can_post)

    async def send_message(self, chat, text, **kw):
        self.sent.append((chat, text))

    async def copy_message(self, chat, from_chat, mid):
        self.copied.append((chat, from_chat, mid))


def run(coro):
    return asyncio.run(coro)


def test_channel_id_default_and_env(monkeypatch):
    monkeypatch.delenv("CHANNEL_ID", raising=False)
    assert channel.channel_id() == "@silsilaiworld"
    monkeypatch.setenv("CHANNEL_ID", "silsilaiworld")
    assert channel.channel_id() == "@silsilaiworld"
    monkeypatch.setenv("CHANNEL_ID", "-1003472763463")
    assert channel.channel_id() == -1003472763463


def test_split_short_and_long():
    assert channel.split_text("привет") == ["привет"]
    long = ("абзац " * 300 + "\n\n") * 5
    parts = channel.split_text(long, 4096)
    assert len(parts) > 1 and all(len(p) <= 4096 for p in parts)
    assert "".join(p.replace(" ", "").replace("\n", "") for p in parts) == long.replace(" ", "").replace("\n", "")


def test_check_admin_variants():
    assert run(channel.check_admin(FakeBot()))[0] is True
    assert run(channel.check_admin(FakeBot(can_post=False)))[0] is False
    assert run(channel.check_admin(FakeBot(status="member")))[0] is False
    assert run(channel.check_admin(FakeBot(status="creator")))[0] is True


def test_post_text_and_copy(monkeypatch):
    monkeypatch.delenv("CHANNEL_ID", raising=False)
    bot = FakeBot()
    assert run(channel.post_text(bot, "первая запись")) == 1
    assert bot.sent == [("@silsilaiworld", "первая запись")]
    run(channel.post_copy(bot, 111111111, 77))
    assert bot.copied == [("@silsilaiworld", 111111111, 77)]
