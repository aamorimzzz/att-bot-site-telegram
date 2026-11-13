"""Núcleo: baixar páginas, extrair texto, calcular diff, montar mensagens e agendar checagens."""

from __future__ import annotations

import asyncio
import difflib
import html
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from bs4 import BeautifulSoup, NavigableString

from .config import Config
from .storage import Storage

log = logging.getLogger("attsite.monitor")

MODES = ("texto", "html", "json")
NOTIFY_MODES = ("tudo", "add", "rem")
MAX_BYTES = 15 * 1024 * 1024
TG_SAFE_LIMIT = 3900          # Telegram aceita 4096; deixamos folga
LINE_MAX = 300                # corta linhas gigantes na mensagem (o arquivo .txt tem tudo)
SEP = "=============================="


class FetchError(Exception):
    """Falha ao baixar a página (rede, timeout, HTTP 4xx/5xx)."""


class ExtractError(Exception):
    """Falha ao extrair o conteúdo (seletor não achou nada, JSON inválido...)."""


# ============================================================ utilidades de tempo
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_interval(text: str) -> int:
    """'30' -> 30 · '30s' -> 30 · '5m' -> 300 · '1h30m' -> 5400."""
    s = (text or "").strip().lower().replace(" ", "")
    s = s.replace("min", "m").replace("seg", "s")
    if s.isdigit() and int(s) > 0:
        return int(s)
    total, pos = 0, 0
    for m in re.finditer(r"(\d+)([smhd])", s):
        if m.start() != pos:
            break
        total += int(m.group(1)) * _UNITS[m.group(2)]
        pos = m.end()
    if not s or pos != len(s) or total <= 0:
        raise ValueError(f"intervalo inválido: {text!r} (use 30s, 5m, 1h...)")
    return total


def fmt_interval(seconds: int | float) -> str:
    seconds = int(seconds)
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            q, seconds = divmod(seconds, size)
            parts.append(f"{q}{unit}")
    return "".join(parts) or "0s"


def ago(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return "nunca"
    d = int((now or time.time()) - ts)
    if d < 5:
        return "agora"
    if d < 60:
        return f"há {d}s"
    if d < 3600:
        return f"há {d // 60} min"
    if d < 86400:
        return f"há {d // 3600} h"
    return f"há {d // 86400} d"


# ============================================================ extração de conteúdo
_DROP_TAGS = ["script", "style", "noscript", "template", "svg", "iframe", "head", "canvas", "object"]


def clean(s: str) -> str:
    return " ".join(s.split())


def _flatten_tables(root: Any) -> None:
    """Transforma cada linha de tabela em uma linha de texto 'célula | célula | ...'.

    Linhas que contêm outra tabela (tabelas de layout, comuns em sites antigos como o EDGAR
    da SEC) são mantidas, para não virar um linhão gigante.
    """
    for tr in root.find_all("tr"):
        if tr.find("table"):
            continue
        cells = [clean(c.get_text(" ")) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        tr.replace_with(NavigableString("\n" + " | ".join(cells) + "\n"))


def html_to_lines(raw: bytes | str, encoding: str | None = None, selector: str | None = None) -> list[str]:
    if isinstance(raw, bytes):
        soup = BeautifulSoup(raw, "lxml", from_encoding=encoding)
    else:
        soup = BeautifulSoup(raw, "lxml")
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    if selector:
        try:
            nodes = soup.select(selector)
        except Exception as exc:  # soupsieve.SelectorSyntaxError e afins
            raise ExtractError(f"seletor CSS inválido: {selector!r}") from exc
        if not nodes:
            raise ExtractError(f"o seletor {selector!r} não encontrou nada na página")
    else:
        nodes = [soup.body or soup]
    lines: list[str] = []
    for node in nodes:
        _flatten_tables(node)
        for ln in node.get_text("\n").splitlines():
            c = clean(ln)
            if c:
                lines.append(c)
    return lines


def _decode(raw: bytes, encoding: str | None) -> str:
    try:
        return raw.decode(encoding or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def json_to_lines(raw: bytes, encoding: str | None = None, path: str | None = None) -> list[str]:
    try:
        data = json.loads(_decode(raw, encoding))
    except json.JSONDecodeError as exc:
        raise ExtractError(f"a resposta não é um JSON válido ({exc.msg})") from exc
    if path:
        for part in path.strip().strip(".").split("."):
            if isinstance(data, list) and part.lstrip("-").isdigit():
                try:
                    data = data[int(part)]
                    continue
                except IndexError:
                    pass
            elif isinstance(data, dict) and part in data:
                data = data[part]
                continue
            raise ExtractError(f"caminho JSON {path!r} não encontrado (parou em {part!r})")
    text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
    return [ln.rstrip() for ln in text.splitlines() if ln.strip()]


def raw_to_lines(raw: bytes, encoding: str | None = None, selector: str | None = None) -> list[str]:
    if selector:
        soup = BeautifulSoup(raw, "lxml", from_encoding=encoding)
        try:
            nodes = soup.select(selector)
        except Exception as exc:
            raise ExtractError(f"seletor CSS inválido: {selector!r}") from exc
        if not nodes:
            raise ExtractError(f"o seletor {selector!r} não encontrou nada na página")
        text = "\n".join(n.prettify() for n in nodes)
    else:
        text = _decode(raw, encoding)
    return [ln.rstrip() for ln in text.splitlines() if ln.strip()]


def extract(raw: bytes, encoding: str | None, mode: str, selector: str | None) -> list[str]:
    if mode == "json":
        return json_to_lines(raw, encoding, selector)
    if mode == "html":
        return raw_to_lines(raw, encoding, selector)
    return html_to_lines(raw, encoding, selector)


# ============================================================ diff
@dataclass
class Diff:
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.removed and not self.added

    def to_json(self, at: float) -> str:
        return json.dumps({"at": at, "removed": self.removed, "added": self.added}, ensure_ascii=False)

    @staticmethod
    def from_json(raw: str | None) -> tuple["Diff", float] | None:
        if not raw:
            return None
        d = json.loads(raw)
        return Diff(d.get("removed", []), d.get("added", [])), float(d.get("at") or 0)


def validate_regex(pattern: str) -> None:
    try:
        re.compile(pattern, re.I)
    except re.error as exc:
        raise ValueError(f"regex inválida: {exc}") from exc


def compute_diff(old: list[str], new: list[str], ignore: str | None = None) -> Diff:
    """Diff linha a linha. `ignore` é uma regex: os trechos que batem são desconsiderados
    na comparação (a linha original continua aparecendo no aviso)."""
    rx = re.compile(ignore, re.I) if ignore else None

    def prep(lines: list[str]) -> list[tuple[str, str]]:
        out = []
        for line in lines:
            key = clean(rx.sub("", line)) if rx else line
            if key:
                out.append((key, line))
        return out

    a, b = prep(old), prep(new)
    sm = difflib.SequenceMatcher(None, [k for k, _ in a], [k for k, _ in b], autojunk=False)
    diff = Diff()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            diff.removed.extend(orig for _, orig in a[i1:i2])
        if tag in ("replace", "insert"):
            diff.added.extend(orig for _, orig in b[j1:j2])
    return diff


def parse_keywords(raw: str | None) -> list[str]:
    return [k.strip() for k in (raw or "").split(",") if k.strip()]


def keyword_hits(lines: Iterable[str], keywords: list[str]) -> list[str]:
    blob = "\n".join(lines).lower()
    return [k for k in keywords if k.lower() in blob]


# ============================================================ mensagens
def esc(s: Any) -> str:
    return html.escape(str(s), quote=False)


def cut(s: str, n: int = LINE_MAX) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _take(lines: list[str], budget: int) -> list[str]:
    out, used = [], 0
    for line in lines:
        if used + len(line) + 1 > budget:
            break
        out.append(line)
        used += len(line) + 1
    return out


def _section(title: str, shown: list[str], omitted: int) -> str:
    body = "\n".join(shown) if shown else "…"
    text = f"{title}\n<pre>{body}</pre>"
    if omitted:
        text += f"\n<i>… +{omitted} linhas (veja o arquivo)</i>"
    return text


def render_change(*, title: str, url: str, when: str, removed: list[str], added: list[str],
                  hits: list[str] | None = None, header: str = "🔔 <b>ATUALIZAÇÃO DETECTADA!</b>",
                  limit: int = TG_SAFE_LIMIT) -> tuple[str, bool]:
    """Monta o aviso no estilo do bot original. Retorna (texto_html, truncado?)."""
    head = [header, ""]
    if hits:
        head += [f"🚨 <b>PALAVRA-CHAVE:</b> {esc(cut(', '.join(hits), 200))}", ""]
    head.append(f"📋 <b>Site:</b> {esc(cut(title, 300))}")
    if title != url:
        head.append(f"🔗 {esc(cut(url, 300))}")
    head.append(f"🕐 {esc(when)}")
    head += [
        "",
        "📝 <b>DIFF TÉCNICO:</b>",
        SEP,
        "📊 <b>RESUMO DO DIFF</b>",
        f"🔴 {len(removed)} linhas removidas",
        f"🟢 {len(added)} linhas adicionadas",
        SEP,
    ]
    header_text = "\n".join(head)

    rem = [esc("- " + cut(x)) for x in removed]
    add = [esc("+ " + cut(x)) for x in added]
    budget = max(limit - len(header_text) - 200, 300)
    rem_len = sum(len(x) + 1 for x in rem)
    add_len = sum(len(x) + 1 for x in add)
    if rem_len + add_len <= budget:
        rem_b, add_b = rem_len, add_len
    else:
        half = budget // 2
        if rem_len <= half:
            rem_b, add_b = rem_len, budget - rem_len
        elif add_len <= half:
            rem_b, add_b = budget - add_len, add_len
        else:
            rem_b = add_b = half
    rem_shown, add_shown = _take(rem, rem_b), _take(add, add_b)
    truncated = (len(rem_shown) < len(rem) or len(add_shown) < len(add)
                 or any(len(x) > LINE_MAX for x in (*removed, *added)))

    parts = [header_text]
    if removed:
        parts.append(_section("🔴 <b>REMOVIDO:</b>", rem_shown, len(rem) - len(rem_shown)))
    if added:
        parts.append(_section("🟢 <b>ADICIONADO:</b>", add_shown, len(add) - len(add_shown)))
    if truncated:
        parts.append("📎 <i>Diff completo no arquivo abaixo.</i>")
    return "\n\n".join(parts), truncated


def render_diff_file(*, title: str, url: str, when: str, removed: list[str], added: list[str]) -> str:
    lines = [
        "ATUALIZAÇÃO DETECTADA",
        f"Site: {title}",
        f"URL: {url}",
        f"Quando: {when}",
        "",
        SEP,
        f"RESUMO: {len(removed)} linhas removidas / {len(added)} linhas adicionadas",
        SEP,
        "",
        "REMOVIDO:",
        *("- " + x for x in removed),
        "",
        "ADICIONADO:",
        *("+ " + x for x in added),
        "",
    ]
    return "\n".join(lines)


def preview(lines: list[str], n: int = 8, width: int = 120) -> str:
    if not lines:
        return "<i>(nenhum texto encontrado)</i>"
    body = "\n".join(cut(x, width) for x in lines[:n])
    more = f"\n… (+{len(lines) - n} linhas)" if len(lines) > n else ""
    return f"<pre>{esc(body + more)}</pre>"


# ============================================================ download
@dataclass
class FetchResult:
    status: int
    content: bytes
    encoding: str | None
    etag: str | None
    last_modified: str | None
    not_modified: bool
    final_url: str
    elapsed: float


def is_sec(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "sec.gov" or host.endswith(".sec.gov")


async def fetch(client: httpx.AsyncClient, url: str, *, user_agent: str, etag: str | None = None,
                last_modified: str | None = None, timeout: float = 30.0) -> FetchResult:
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    t0 = time.monotonic()
    try:
        r = await client.get(url, headers=headers, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise FetchError("o site demorou demais para responder (timeout)") from exc
    except httpx.HTTPError as exc:
        raise FetchError(cut(f"erro de conexão: {type(exc).__name__}: {exc}", 300)) from exc
    elapsed = time.monotonic() - t0
    if r.status_code == 304:
        return FetchResult(304, b"", None, etag, last_modified, True, str(r.url), elapsed)
    if r.status_code >= 400:
        hint = ""
        if r.status_code in (403, 429) and is_sec(url):
            hint = " — a SEC exige identificação: preencha SEC_USER_AGENT no .env (ex: 'Seu Nome seu@email.com')"
        elif r.status_code in (403, 503):
            hint = " — o site pode estar bloqueando robôs (Cloudflare/anti-bot)"
        raise FetchError(f"HTTP {r.status_code}{hint}")
    if len(r.content) > MAX_BYTES:
        raise FetchError(f"página grande demais ({len(r.content) // 1024 // 1024} MB)")
    return FetchResult(
        status=r.status_code,
        content=r.content,
        encoding=r.charset_encoding,
        etag=r.headers.get("etag"),
        last_modified=r.headers.get("last-modified"),
        not_modified=False,
        final_url=str(r.url),
        elapsed=elapsed,
    )


# ============================================================ motor
@dataclass
class Event:
    kind: str                         # baseline | unchanged | changed | filtered | error
    watch: dict[str, Any]
    diff: Diff | None = None
    hits: list[str] = field(default_factory=list)
    error: str | None = None
    alert: bool = False               # erro chegou no limite -> avisar o usuário
    recovered: bool = False           # voltou a funcionar depois de ter avisado erro
    lines: int = 0
    at: float = 0.0


def should_notify(watch: dict[str, Any], diff: Diff, hits: list[str]) -> bool:
    if parse_keywords(watch.get("keywords")) and not hits:
        return False
    mode = watch.get("notify") or "tudo"
    if mode == "add" and not diff.added:
        return False
    if mode == "rem" and not diff.removed:
        return False
    return True


class Monitor:
    def __init__(self, storage: Storage, cfg: Config, client: httpx.AsyncClient | None = None):
        self.storage = storage
        self.cfg = cfg
        self.client = client or httpx.AsyncClient(
            follow_redirects=True,
            limits=httpx.Limits(max_connections=max(10, cfg.max_concurrency * 2)),
        )
        try:
            self.tz = ZoneInfo(cfg.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("Fuso horário %r inválido; usando o horário local", cfg.timezone)
            self.tz = None
        self._locks: dict[int, asyncio.Lock] = {}
        self._tasks: dict[int, asyncio.Task] = {}

    async def close(self) -> None:
        for t in list(self._tasks.values()):
            t.cancel()
        await self.client.aclose()

    # -------------------------------------------------------------- helpers
    def fmt_time(self, ts: float | None = None, short: bool = False) -> str:
        if not ts:
            return "—"
        dt = datetime.fromtimestamp(ts, self.tz) if self.tz else datetime.fromtimestamp(ts)
        return dt.strftime("%H:%M:%S" if short else "%d/%m/%Y %H:%M:%S")

    def user_agent_for(self, watch: dict[str, Any]) -> str:
        if watch.get("user_agent"):
            return watch["user_agent"]
        if self.cfg.sec_user_agent and is_sec(watch["url"]):
            return self.cfg.sec_user_agent
        return self.cfg.user_agent

    def lock(self, watch_id: int) -> asyncio.Lock:
        return self._locks.setdefault(watch_id, asyncio.Lock())

    async def fetch_lines(self, url: str, mode: str = "texto", selector: str | None = None,
                          user_agent: str | None = None) -> tuple[FetchResult, list[str]]:
        res = await fetch(self.client, url, user_agent=user_agent or self.cfg.user_agent,
                          timeout=self.cfg.request_timeout)
        lines = await asyncio.to_thread(extract, res.content, res.encoding, mode, selector)
        return res, lines

    # -------------------------------------------------------------- checagem
    async def check(self, watch_id: int) -> Event | None:
        async with self.lock(watch_id):
            return await self._check(watch_id)

    async def _check(self, watch_id: int) -> Event | None:
        w = self.storage.get(watch_id)
        if not w:
            return None
        now = time.time()
        try:
            res = await fetch(
                self.client, w["url"], user_agent=self.user_agent_for(w),
                etag=w["etag"] if w["content"] is not None else None,
                last_modified=w["last_modified"] if w["content"] is not None else None,
                timeout=self.cfg.request_timeout,
            )
            lines = None if res.not_modified else await asyncio.to_thread(
                extract, res.content, res.encoding, w["mode"], w["selector"])
        except (FetchError, ExtractError) as exc:
            return self._on_error(w, str(exc), now)
        except Exception as exc:  # nunca deixar o loop morrer
            log.exception("Erro inesperado checando #%s", watch_id)
            return self._on_error(w, f"erro inesperado: {type(exc).__name__}: {exc}", now)

        recovered = w["error_count"] >= self.cfg.error_alert_after
        base: dict[str, Any] = dict(last_check=now, next_check=now + w["interval"], error_count=0,
                                    last_status="ok", checks=w["checks"] + 1)
        if lines is None:  # 304 Not Modified
            self.storage.update(watch_id, **base)
            return Event("unchanged", self.storage.get(watch_id), recovered=recovered, at=now)

        base.update(etag=res.etag, last_modified=res.last_modified, content="\n".join(lines))
        if w["content"] is None:
            self.storage.update(watch_id, **base)
            return Event("baseline", self.storage.get(watch_id), recovered=recovered,
                         lines=len(lines), at=now)

        old = w["content"].split("\n") if w["content"] else []
        diff = await asyncio.to_thread(compute_diff, old, lines, w["ignore_regex"])
        if diff.empty:
            self.storage.update(watch_id, **base)
            return Event("unchanged", self.storage.get(watch_id), recovered=recovered,
                         lines=len(lines), at=now)

        hits = keyword_hits(diff.added, parse_keywords(w["keywords"]))
        notify = should_notify(w, diff, hits)
        base.update(last_diff=diff.to_json(now), last_change=now, changes=w["changes"] + 1)
        self.storage.update(watch_id, **base)
        log.info("#%s mudou: -%d +%d linhas%s", watch_id, len(diff.removed), len(diff.added),
                 "" if notify else " (filtrado)")
        return Event("changed" if notify else "filtered", self.storage.get(watch_id), diff=diff,
                     hits=hits, recovered=recovered, lines=len(lines), at=now)

    def _on_error(self, w: dict[str, Any], msg: str, now: float) -> Event:
        errors = w["error_count"] + 1
        backoff = min(2 ** (errors - 1), 16)
        delay = min(w["interval"] * backoff, max(w["interval"], 1800))
        self.storage.update(w["id"], last_check=now, next_check=now + delay, error_count=errors,
                            last_status=cut(msg, 500), checks=w["checks"] + 1)
        log.warning("#%s erro (%dx): %s", w["id"], errors, msg)
        return Event("error", self.storage.get(w["id"]), error=msg,
                     alert=errors == self.cfg.error_alert_after, at=now)

    async def rebaseline(self, watch_id: int, **changes: Any) -> tuple[bool, list[str] | None, str | None]:
        """Aplica mudanças que alteram a extração (url/seletor/modo/ua) e captura nova linha de base.

        Retorna (aplicado?, linhas|None, erro|None). Erro de extração => não aplica.
        Erro de rede => aplica e deixa a linha de base para a próxima checagem.
        """
        async with self.lock(watch_id):
            w = self.storage.get(watch_id)
            if not w:
                return False, None, "monitor não encontrado"
            new = {**w, **changes}
            now = time.time()
            reset = dict(content=None, etag=None, last_modified=None, error_count=0)
            try:
                res, lines = await self.fetch_lines(new["url"], new["mode"], new["selector"],
                                                    self.user_agent_for(new))
            except ExtractError as exc:
                return False, None, str(exc)
            except FetchError as exc:
                self.storage.update(watch_id, **changes, **reset, next_check=now + new["interval"],
                                    last_status=cut(str(exc), 500))
                return True, None, str(exc)
            fresh = dict(reset, content="\n".join(lines), etag=res.etag,
                         last_modified=res.last_modified, last_check=now,
                         next_check=now + new["interval"], last_status="ok")
            self.storage.update(watch_id, **changes, **fresh)
            return True, lines, None

    # -------------------------------------------------------------- agendador
    async def run_forever(self, on_event: Callable[[Event], Awaitable[None]], tick: float = 2.0) -> None:
        sem = asyncio.Semaphore(self.cfg.max_concurrency)

        async def one(wid: int) -> None:
            try:
                async with sem:
                    ev = await self.check(wid)
                if ev:
                    await on_event(ev)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Falha processando #%s", wid)
            finally:
                self._tasks.pop(wid, None)

        log.info("Agendador iniciado (até %d checagens simultâneas)", self.cfg.max_concurrency)
        while True:
            try:
                for wid in self.storage.due(time.time()):
                    if wid not in self._tasks:
                        self._tasks[wid] = asyncio.create_task(one(wid))
            except Exception:
                log.exception("Erro no agendador")
            await asyncio.sleep(tick)
