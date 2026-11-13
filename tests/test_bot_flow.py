"""Teste ponta a ponta: simula o Telegram (sem rede) e conversa com o bot."""

import itertools
import json
import time

import pytest
from telegram import Update
from telegram.request import BaseRequest

from attsite.monitor import Monitor
from attsite.telegram_bot import build_app

URL = "https://loja.exemplo.com/produtos"
BOT_USER = {"id": 999, "is_bot": True, "first_name": "AttSite", "username": "attsite_test_bot"}


class FakeTelegram(BaseRequest):
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.files: dict[str, bytes] = {}
        self._ids = itertools.count(100)

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    @property
    def read_timeout(self):
        return 5.0

    def _msg(self, params, **extra):
        return {"message_id": next(self._ids), "date": int(time.time()),
                "chat": {"id": int(params.get("chat_id", 1)), "type": "private"}, "from": BOT_USER, **extra}

    async def do_request(self, url, method, request_data=None, **kw):
        if "/file/bot" in url:
            return 200, self.files[url.rsplit("/", 1)[-1]]
        name = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        if request_data and request_data.multipart_data:
            params["_files"] = dict(request_data.multipart_data)
        self.calls.append((name, params))
        if name == "getMe":
            result = BOT_USER
        elif name in ("sendMessage", "editMessageText", "editMessageReplyMarkup"):
            result = self._msg(params, text=params.get("text", ""))
        elif name == "sendDocument":
            result = self._msg(params, document={"file_id": "d1", "file_unique_id": "u1"})
        elif name == "getFile":
            result = {"file_id": params["file_id"], "file_unique_id": "u", "file_size": 10,
                      "file_path": f"documents/{params['file_id']}"}
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    # ---- helpers para os testes
    def texts(self):
        return [p.get("text", "") for n, p in self.calls if n in ("sendMessage", "editMessageText")]

    def last_text(self):
        return self.texts()[-1]

    def docs(self):
        return [p for n, p in self.calls if n == "sendDocument"]


def page(*items):
    return "<html><body><ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul></body></html>"


class Chat:
    def __init__(self, app, tg, uid=111):
        self.app, self.tg, self.uid = app, tg, uid
        self._ids = itertools.count(1)

    def _base(self, uid=None):
        uid = uid or self.uid
        return {"message_id": next(self._ids), "date": int(time.time()),
                "chat": {"id": uid, "type": "private"},
                "from": {"id": uid, "is_bot": False, "first_name": "Ana"}}

    async def send(self, text, uid=None):
        msg = self._base(uid)
        msg["text"] = text
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await self.app.process_update(Update.de_json({"update_id": next(self._ids), "message": msg}, self.app.bot))

    async def send_file(self, file_id, data, caption=None):
        self.tg.files[file_id] = data
        msg = self._base()
        msg["document"] = {"file_id": file_id, "file_unique_id": "x", "file_name": "m.json", "file_size": len(data)}
        if caption:
            msg["caption"] = caption
        await self.app.process_update(Update.de_json({"update_id": next(self._ids), "message": msg}, self.app.bot))

    async def click(self, data):
        cq = {"id": str(next(self._ids)), "chat_instance": "ci", "data": data,
              "from": {"id": self.uid, "is_bot": False, "first_name": "Ana"},
              "message": {**self._base(), "from": BOT_USER, "text": "card"}}
        await self.app.process_update(Update.de_json({"update_id": next(self._ids), "callback_query": cq}, self.app.bot))


@pytest.fixture
async def env(cfg, storage, site):
    tg = FakeTelegram()
    mon = Monitor(storage, cfg, client=site.client())
    app = build_app(cfg, monitor=mon, storage=storage, request=tg)
    await app.initialize()
    yield app, tg, site, storage
    await app.shutdown()
    await mon.close()


async def test_full_flow(env):
    app, tg, site, storage = env
    chat = Chat(app, tg)
    site.set(URL, page("Camiseta R$ 50", "Boné R$ 30"))

    await chat.send("/start")
    assert any("dono" in t for t in tg.texts())

    # outro usuário é bloqueado
    await chat.send("/list", uid=222)
    assert "Acesso negado" in tg.last_text() and "222" in tg.last_text()

    await chat.send(f"/add {URL} 30s")
    assert "Monitorando #1" in tg.last_text() and "2 linhas capturadas" in tg.last_text()
    w = storage.get(1)
    assert w["interval"] == 30 and w["content"] == "Camiseta R$ 50\nBoné R$ 30"

    await chat.send(f"/add {URL}")
    assert "já está sendo monitorado" in tg.last_text()

    await chat.send("/list")
    assert "#1" in tg.last_text() and "/i1" in tg.last_text()

    await chat.send("/i1")
    card = tg.calls[-1][1]
    assert "Intervalo: <b>30s</b>" in card["text"] and "inline_keyboard" in str(card["reply_markup"])

    await chat.send("/check 1")
    assert "sem mudanças" in tg.last_text()

    site.set(URL, page("Camiseta R$ 45", "Boné R$ 30", "Tênis R$ 200"))
    await chat.send("/check 1")
    alert = tg.last_text()
    assert "ATUALIZAÇÃO DETECTADA" in alert
    assert "- Camiseta R$ 50" in alert and "+ Camiseta R$ 45" in alert and "+ Tênis R$ 200" in alert
    assert "🔴 1 linhas removidas" in alert and "🟢 2 linhas adicionadas" in alert

    await chat.send("/diff 1")
    assert "ÚLTIMA MUDANÇA" in tg.last_text()

    await chat.send("/snapshot 1")
    assert tg.docs()[-1]["_files"]

    await chat.send("/set 1 intervalo 5m")
    assert storage.get(1)["interval"] == 300
    await chat.send("/set 1 palavras promoção, tênis")
    assert storage.get(1)["keywords"] == "promoção, tênis"
    await chat.send("/set 1 ignorar R\\$ \\d+")
    assert storage.get(1)["ignore_regex"] == "R\\$ \\d+"
    await chat.send("/set 1 ignorar [abc")
    assert "regex inválida" in tg.last_text()
    await chat.send("/set 1 seletor .nao-existe")
    assert "Não apliquei" in tg.last_text() and storage.get(1)["selector"] is None
    await chat.send("/set 1 seletor ul")
    assert "Nova linha de base: 3 linhas" in tg.last_text()
    await chat.send("/set 1 avisar add")
    assert storage.get(1)["notify"] == "add"
    await chat.send("/set 1 banana 2")
    assert "Campo desconhecido" in tg.last_text()

    # botões
    await chat.click("pau:1")
    assert storage.get(1)["paused"] == 1
    await chat.click("res:1")
    assert storage.get(1)["paused"] == 0
    await chat.click("chk:1")
    assert any(n == "answerCallbackQuery" for n, _ in tg.calls)

    # export -> remove -> import
    await chat.send("/export")
    exported = tg.docs()[-1]["_files"]
    raw = next(iter(exported.values()))
    raw = raw[1] if isinstance(raw, tuple) else raw
    data = json.loads(raw)
    assert data["watches"][0]["url"] == URL and data["watches"][0]["selector"] == "ul"

    await chat.click("del:1")
    await chat.click("delok:1")
    assert storage.get(1) is None

    await chat.send("/import")
    await chat.send_file("f1", json.dumps(data).encode())
    assert "Importados: <b>1</b>" in tg.last_text()
    new = storage.list_chat(111)[0]
    assert new["selector"] == "ul" and new["keywords"] == "promoção, tênis"

    await chat.send("/status")
    assert "Monitores: 1" in tg.last_text()

    # link puro no chat também adiciona
    site.set("https://www.exemplo.org/", page("x"))
    await chat.send("https://www.exemplo.org/")
    assert "Monitorando #" in tg.last_text()

    # admin
    await chat.send("/allow 222")
    await chat.send("/list", uid=222)
    assert "Nenhum monitor" in tg.last_text()


async def test_scheduled_alerts_via_notify(env):
    from attsite.telegram_bot import notify_event

    app, tg, site, storage = env
    mon = app.bot_data["monitor"]
    site.set(URL, page(*[f"item {i} " + "x" * 120 for i in range(80)]))
    wid = storage.add_watch(555, URL, 60)
    await mon.check(wid)
    site.set(URL, page(*[f"novo {i} " + "y" * 120 for i in range(80)]))
    ev = await mon.check(wid)
    await notify_event(app.bot, mon, storage, ev)
    sent = [p for n, p in tg.calls if n == "sendMessage"][-1]
    assert len(sent["text"]) <= 4096 and "Diff completo no arquivo" in sent["text"]
    assert tg.docs() and int(tg.docs()[-1]["chat_id"]) == 555
