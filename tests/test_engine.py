import asyncio
import time

import pytest

from attsite.monitor import Monitor

URL = "https://exemplo.com/pagina"


def page(*items: str) -> str:
    lis = "".join(f"<li>{i}</li>" for i in items)
    return f"<html><body><h1>Lista</h1><ul>{lis}</ul><footer>rodapé</footer></body></html>"


async def test_baseline_change_unchanged(storage, cfg, site):
    m = Monitor(storage, cfg, client=site.client())
    site.set(URL, page("a", "b"))
    wid = storage.add_watch(1, URL, 60)

    ev = await m.check(wid)
    assert ev.kind == "baseline" and ev.lines == 4

    ev = await m.check(wid)
    assert ev.kind == "unchanged"

    site.set(URL, page("novo", "a"))
    ev = await m.check(wid)
    assert ev.kind == "changed"
    assert ev.diff.added == ["novo"] and ev.diff.removed == ["b"]
    w = storage.get(wid)
    assert w["changes"] == 1 and w["checks"] == 3 and w["last_diff"]
    assert w["next_check"] > time.time() + 50
    await m.close()


async def test_etag_304(storage, cfg, site):
    m = Monitor(storage, cfg, client=site.client())
    site.set(URL, page("a"), headers={"content-type": "text/html", "etag": '"v1"'})
    wid = storage.add_watch(1, URL, 60)
    assert (await m.check(wid)).kind == "baseline"
    ev = await m.check(wid)
    assert ev.kind == "unchanged"
    assert site.requests[-1].headers["if-none-match"] == '"v1"'
    await m.close()


async def test_errors_backoff_alert_and_recovery(storage, cfg, site):
    m = Monitor(storage, cfg, client=site.client())
    wid = storage.add_watch(1, URL, 60)
    site.set(URL, "fora", status=500)
    kinds = []
    for _ in range(4):
        ev = await m.check(wid)
        kinds.append((ev.kind, ev.alert))
    assert kinds == [("error", False), ("error", False), ("error", True), ("error", False)]
    w = storage.get(wid)
    assert w["error_count"] == 4 and "HTTP 500" in w["last_status"]
    assert w["next_check"] - w["last_check"] == pytest.approx(60 * 8)

    site.set(URL, page("ok"))
    ev = await m.check(wid)
    assert ev.kind == "baseline" and ev.recovered
    assert storage.get(wid)["error_count"] == 0
    await m.close()


async def test_connection_error_is_caught(storage, cfg, site):
    m = Monitor(storage, cfg, client=site.client())
    wid = storage.add_watch(1, "https://nao-existe.example/", 60)
    ev = await m.check(wid)
    assert ev.kind == "error" and "conexão" in ev.error
    await m.close()


async def test_keywords_and_notify_filters(storage, cfg, site):
    m = Monitor(storage, cfg, client=site.client())
    site.set(URL, page("a"))
    wid = storage.add_watch(1, URL, 60, keywords="ipo, s-1")
    await m.check(wid)

    site.set(URL, page("a", "outra coisa"))
    ev = await m.check(wid)
    assert ev.kind == "filtered"

    site.set(URL, page("a", "outra coisa", "Novo IPO da ACME"))
    ev = await m.check(wid)
    assert ev.kind == "changed" and ev.hits == ["ipo"]

    storage.update(wid, keywords=None, notify="add")
    site.set(URL, page("a"))  # só remoções
    ev = await m.check(wid)
    assert ev.kind == "filtered" and ev.diff.removed and not ev.diff.added
    await m.close()


async def test_rebaseline_selector(storage, cfg, site):
    m = Monitor(storage, cfg, client=site.client())
    site.set(URL, page("a", "b"))
    wid = storage.add_watch(1, URL, 60)
    await m.check(wid)

    ok, lines, err = await m.rebaseline(wid, selector=".nao-existe")
    assert not ok and "não encontrou" in err
    assert storage.get(wid)["selector"] is None

    ok, lines, err = await m.rebaseline(wid, selector="ul")
    assert ok and lines == ["a", "b"] and err is None
    w = storage.get(wid)
    assert w["selector"] == "ul" and w["content"] == "a\nb"

    site.set(URL, page("a", "b").replace("rodapé", "rodapé mudou"))
    assert (await m.check(wid)).kind == "unchanged"  # mudança fora do seletor
    await m.close()


async def test_sec_user_agent(storage, cfg, site):
    m = Monitor(storage, cfg, client=site.client())
    sec = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=S-1"
    site.set(sec, page("x"))
    wid = storage.add_watch(1, sec, 60)
    await m.check(wid)
    assert site.requests[-1].headers["user-agent"] == "Fulano fulano@example.com"
    site.set(sec, "bloqueado", status=403)
    ev = await m.check(wid)
    assert "SEC_USER_AGENT" in ev.error
    await m.close()


async def test_scheduler_runs_due_watches(storage, cfg, site):
    m = Monitor(storage, cfg, client=site.client())
    site.set(URL, page("a"))
    wid = storage.add_watch(1, URL, 1, next_check=0)
    paused = storage.add_watch(1, URL + "?p", 1, next_check=0, paused=True)
    events = []

    async def on_event(ev):
        events.append(ev)

    task = asyncio.create_task(m.run_forever(on_event, tick=0.05))
    await asyncio.sleep(0.4)
    site.set(URL, page("a", "b"))
    await asyncio.sleep(1.4)
    task.cancel()
    kinds = [e.kind for e in events if e.watch["id"] == wid]
    assert kinds[0] == "baseline" and "changed" in kinds
    assert not any(e.watch["id"] == paused for e in events)
    await m.close()
