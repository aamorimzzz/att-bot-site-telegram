"""Interface do Telegram: comandos, botões e envio dos avisos."""

from __future__ import annotations

import asyncio
import functools
import io
import json
import logging
import platform
import re
import time
from typing import Any
from urllib.parse import urlparse

from telegram import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, Defaults,
    MessageHandler, filters,
)

from . import __version__
from .config import Config
from .monitor import (
    MODES, NOTIFY_MODES, Diff, Event, Monitor, ago, cut, esc, fmt_interval, parse_interval,
    preview, render_change, render_diff_file, validate_regex,
)
from .storage import EXPORT_FIELDS, Storage

log = logging.getLogger("attsite.bot")
Ctx = ContextTypes.DEFAULT_TYPE

FIELD_ALIASES = {
    "nome": "name", "name": "name", "titulo": "name",
    "intervalo": "interval", "interval": "interval", "tempo": "interval",
    "seletor": "selector", "selector": "selector", "css": "selector",
    "modo": "mode", "mode": "mode",
    "ignorar": "ignore_regex", "ignore": "ignore_regex", "ignora": "ignore_regex",
    "palavras": "keywords", "palavra": "keywords", "keywords": "keywords", "keyword": "keywords",
    "avisar": "notify", "notify": "notify", "aviso": "notify",
    "ua": "user_agent", "useragent": "user_agent", "user-agent": "user_agent",
    "url": "url", "link": "url",
}
CLEAR_WORDS = {"limpar", "nenhum", "nenhuma", "none", "-", "off", "clear", "remover", "padrao", "padrão"}
NOTIFY_ALIASES = {
    "tudo": "tudo", "all": "tudo", "todas": "tudo", "sempre": "tudo",
    "add": "add", "adicionadas": "add", "adicionado": "add", "added": "add", "novas": "add",
    "rem": "rem", "removidas": "rem", "removido": "rem", "removed": "rem",
}
MODE_ALIASES = {"texto": "texto", "text": "texto", "html": "html", "raw": "html", "codigo": "html",
                "json": "json", "api": "json"}
ALL_WORDS = {"tudo", "todos", "all", "*"}
NOTIFY_LABEL = {"tudo": "tudo", "add": "só adições", "rem": "só remoções"}

HELP = """🤖 <b>AttSite Bot</b> — monitor de mudanças em sites

<b>Básico</b>
/add <code>&lt;url&gt; [intervalo] [seletor]</code> — monitora um site
   ex: <code>/add https://site.com 30s</code>
   ex: <code>/add https://site.com 5m div.noticias</code>
   (ou só mande o link aqui no chat)
/list — seus monitores (toque em /i1, /i2… para ver opções)
/check <code>[id]</code> — checa agora (sem id = todos)
/pause · /resume · /remove <code>&lt;id&gt;</code>

<b>Ajustes</b> — /set <code>&lt;id&gt; &lt;campo&gt; &lt;valor&gt;</code>
• <code>intervalo</code> — 30s, 5m, 1h
• <code>seletor</code> — CSS de uma parte da página (ou <code>limpar</code>)
• <code>ignorar</code> — regex de trechos a ignorar, ex: <code>\\d{2}:\\d{2}</code>
• <code>palavras</code> — só avisa se aparecer: <code>ipo, s-1</code>
• <code>avisar</code> — <code>tudo</code> | <code>add</code> (só adições) | <code>rem</code>
• <code>modo</code> — <code>texto</code> | <code>html</code> | <code>json</code>
• <code>nome</code>, <code>url</code>, <code>ua</code> (user-agent)
ex: <code>/set 1 palavras S-1, IPO</code>

<b>Outros</b>
/diff <code>&lt;id&gt;</code> — último diff · /snapshot <code>&lt;id&gt;</code> — conteúdo atual
/test <code>&lt;url&gt; [seletor]</code> — testa sem salvar
/export · /import — backup dos monitores
/status — status do bot"""

ADMIN_HELP = """
<b>Admin</b>
/allow <code>&lt;id&gt;</code> · /deny <code>&lt;id&gt;</code> · /users — quem pode usar o bot"""

BOT_COMMANDS = [
    ("add", "Monitorar site: /add <url> [intervalo] [seletor]"),
    ("list", "Listar monitores"),
    ("check", "Checar agora: /check [id]"),
    ("set", "Configurar: /set <id> <campo> <valor>"),
    ("pause", "Pausar: /pause <id|tudo>"),
    ("resume", "Retomar: /resume <id|tudo>"),
    ("remove", "Remover: /remove <id>"),
    ("diff", "Último diff: /diff <id>"),
    ("snapshot", "Conteúdo atual: /snapshot <id>"),
    ("test", "Testar URL/seletor sem salvar"),
    ("export", "Exportar monitores (.json)"),
    ("import", "Importar monitores (.json)"),
    ("status", "Status do bot"),
    ("help", "Ajuda"),
]


# =================================================================== acesso
class Access:
    """Quem pode usar o bot.

    - IDs em ALLOWED_USERS (.env) sempre podem e são admins.
    - Se ALLOWED_USERS estiver vazio, o primeiro usuário que falar com o bot vira o dono.
    - Admins liberam outras pessoas com /allow.
    """

    def __init__(self, storage: Storage, env_ids: frozenset[int]):
        self.storage = storage
        self.env_ids = set(env_ids)

    def owner(self) -> int | None:
        v = self.storage.get_setting("owner")
        return int(v) if v else None

    def extra(self) -> set[int]:
        return set(json.loads(self.storage.get_setting("allowed_users") or "[]"))

    def is_admin(self, uid: int) -> bool:
        return uid in self.env_ids or uid == self.owner()

    def is_allowed(self, uid: int) -> bool:
        return self.is_admin(uid) or uid in self.extra()

    def claim(self, uid: int) -> bool:
        if self.env_ids or self.owner() is not None:
            return False
        self.storage.set_setting("owner", str(uid))
        return True

    def allow(self, uid: int) -> None:
        self.storage.set_setting("allowed_users", json.dumps(sorted(self.extra() | {uid})))

    def deny(self, uid: int) -> bool:
        ids = self.extra()
        if uid not in ids:
            return False
        self.storage.set_setting("allowed_users", json.dumps(sorted(ids - {uid})))
        return True


# =================================================================== helpers
def S(ctx: Ctx) -> Storage:
    return ctx.bot_data["storage"]


def M(ctx: Ctx) -> Monitor:
    return ctx.bot_data["monitor"]


def C(ctx: Ctx) -> Config:
    return ctx.bot_data["cfg"]


def A(ctx: Ctx) -> Access:
    return ctx.bot_data["access"]


def guarded(admin: bool = False):
    def deco(func):
        @functools.wraps(func)
        async def wrapper(update: Update, ctx: Ctx):
            user = update.effective_user
            if user is None:
                return
            acc = A(ctx)
            if not acc.is_allowed(user.id):
                if acc.claim(user.id):
                    log.info("Usuário %s (%s) virou dono do bot", user.id, user.full_name)
                    await reply(update, "👑 Você foi o primeiro a falar comigo, então agora é o "
                                        "<b>dono</b> deste bot. Só você (e quem você liberar com "
                                        "/allow) pode usá-lo.")
                else:
                    if update.callback_query:
                        await update.callback_query.answer("🚫 Acesso negado", show_alert=True)
                    else:
                        await reply(update, f"🚫 Acesso negado.\nSeu ID: <code>{user.id}</code>\n"
                                            f"Peça pro dono do bot liberar com <code>/allow {user.id}</code>.")
                    return
            if admin and not acc.is_admin(user.id):
                await reply(update, "🚫 Só o dono do bot pode usar esse comando.")
                return
            return await func(update, ctx)
        return wrapper
    return deco


async def reply(update: Update, text: str, **kw: Any):
    msg = update.effective_message
    if msg is None:
        return None
    return await msg.reply_text(text, **kw)


def args_text(update: Update) -> str:
    """Texto depois do comando, preservando espaços (bom para regex/seletor)."""
    text = (update.effective_message.text or "") if update.effective_message else ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] in "\"'“" and s[-1] in "\"'”":
        return s[1:-1].strip()
    return s


def parse_id(raw: str | None) -> int | None:
    raw = (raw or "").strip().lstrip("#")
    return int(raw) if raw.isdigit() else None


def normalize_url(raw: str) -> str | None:
    raw = raw.strip().strip("<>")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.I):
        raw = "https://" + raw
    p = urlparse(raw)
    if p.scheme not in ("http", "https") or not p.netloc or "." not in p.netloc and "localhost" not in p.netloc:
        return None
    return raw


def title(w: dict[str, Any]) -> str:
    return w["name"] or w["url"]


def short_title(w: dict[str, Any], n: int = 45) -> str:
    if w["name"]:
        return cut(w["name"], n)
    p = urlparse(w["url"])
    return cut(p.netloc + (p.path if p.path != "/" else ""), n)


def status_icon(w: dict[str, Any], alert_after: int) -> str:
    if w["paused"]:
        return "⏸"
    if w["error_count"] >= alert_after:
        return "🔴"
    if w["error_count"]:
        return "🟡"
    if w["last_check"] is None:
        return "⏳"
    return "🟢"


async def get_watch(update: Update, ctx: Ctx, raw: str | None, usage: str) -> dict[str, Any] | None:
    wid = parse_id(raw)
    if wid is None:
        await reply(update, f"Informe o número do monitor. Ex: <code>{usage}</code>\nVeja os números em /list.")
        return None
    w = S(ctx).get(wid)
    if not w or w["chat_id"] != update.effective_chat.id:
        await reply(update, f"❓ Monitor #{wid} não encontrado neste chat. Veja /list.")
        return None
    return w


def secs(value: Any) -> float:
    return value.total_seconds() if hasattr(value, "total_seconds") else float(value)


# =================================================================== envio seguro
async def safe_send(bot, chat_id: int, text: str, markup=None, storage: Storage | None = None) -> bool:
    for _ in range(3):
        try:
            await bot.send_message(chat_id, text, reply_markup=markup)
            return True
        except RetryAfter as exc:
            await asyncio.sleep(secs(exc.retry_after) + 1)
        except BadRequest as exc:
            if markup is not None:
                markup = None
                continue
            log.warning("Telegram recusou mensagem para %s: %s", chat_id, exc)
            return False
        except Forbidden:
            if storage:
                n = storage.pause_chat(chat_id)
                log.warning("Chat %s bloqueou o bot; %d monitores pausados", chat_id, n)
            return False
        except TelegramError as exc:
            log.warning("Erro do Telegram (%s); tentando de novo", exc)
            await asyncio.sleep(3)
    return False


async def safe_doc(bot, chat_id: int, data: bytes, filename: str, caption: str | None = None) -> bool:
    for _ in range(3):
        try:
            await bot.send_document(chat_id, document=io.BytesIO(data), filename=filename, caption=caption)
            return True
        except RetryAfter as exc:
            await asyncio.sleep(secs(exc.retry_after) + 1)
        except (Forbidden, BadRequest) as exc:
            log.warning("Não consegui enviar arquivo para %s: %s", chat_id, exc)
            return False
        except TelegramError as exc:
            log.warning("Erro do Telegram (%s); tentando de novo", exc)
            await asyncio.sleep(3)
    return False


def change_keyboard(w: dict[str, Any]) -> InlineKeyboardMarkup:
    row = []
    if w["url"].startswith(("http://", "https://")):
        row.append(InlineKeyboardButton("🔗 Abrir site", url=w["url"]))
    row.append(InlineKeyboardButton("⚙️ Opções", callback_data=f"info:{w['id']}"))
    return InlineKeyboardMarkup([row])


async def send_diff(bot, monitor: Monitor, storage: Storage, w: dict[str, Any], diff: Diff, at: float,
                    hits: list[str] | None = None, header: str | None = None) -> None:
    when = monitor.fmt_time(at)
    kw = {"header": header} if header else {}
    text, truncated = render_change(title=title(w), url=w["url"], when=when, removed=diff.removed,
                                    added=diff.added, hits=hits, **kw)
    await safe_send(bot, w["chat_id"], text, change_keyboard(w), storage)
    if truncated:
        data = render_diff_file(title=title(w), url=w["url"], when=when, removed=diff.removed,
                                added=diff.added).encode("utf-8")
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(at))
        await safe_doc(bot, w["chat_id"], data, f"diff_{w['id']}_{stamp}.txt")


async def notify_event(bot, monitor: Monitor, storage: Storage, ev: Event, manual: bool = False) -> None:
    w = ev.watch
    chat, wid = w["chat_id"], w["id"]
    if ev.recovered:
        await safe_send(bot, chat, f"✅ <b>#{wid} voltou a responder</b> — {esc(short_title(w, 80))}",
                        storage=storage)
    if ev.kind == "changed":
        await send_diff(bot, monitor, storage, w, ev.diff, ev.at, ev.hits)
    elif ev.kind == "error":
        if ev.alert:
            await safe_send(
                bot, chat,
                f"⚠️ <b>Problema no monitor #{wid}</b>\n📋 {esc(short_title(w, 80))}\n❌ {esc(ev.error)}\n\n"
                f"Já são {w['error_count']} falhas seguidas. Continuo tentando (com intervalo maior) "
                f"e aviso quando voltar.", change_keyboard(w), storage)
        elif manual:
            await safe_send(bot, chat, f"❌ <b>#{wid}</b> erro: {esc(ev.error)}", storage=storage)
    elif manual:
        if ev.kind == "unchanged":
            await safe_send(bot, chat, f"✅ <b>#{wid}</b> sem mudanças — {esc(short_title(w))}", storage=storage)
        elif ev.kind == "baseline":
            await safe_send(bot, chat, f"📸 <b>#{wid}</b> linha de base capturada: {ev.lines} linhas", storage=storage)
        elif ev.kind == "filtered":
            await safe_send(
                bot, chat,
                f"🔕 <b>#{wid}</b> mudou (−{len(ev.diff.removed)} / +{len(ev.diff.added)} linhas), mas não "
                f"bateu com seus filtros (avisar/palavras). Ver: /diff {wid}", storage=storage)


# =================================================================== cartão do monitor
def info_card(w: dict[str, Any], monitor: Monitor, cfg: Config) -> tuple[str, InlineKeyboardMarkup]:
    wid = w["id"]
    state = "⏸ pausado" if w["paused"] else "▶️ ativo"
    lines = [
        f"<b>#{wid} — {esc(short_title(w, 60))}</b>  ({state})",
        f"🔗 {esc(cut(w['url'], 300))}",
        f"⏱ Intervalo: <b>{fmt_interval(w['interval'])}</b>",
        f"🧩 Seletor: {('<code>' + esc(w['selector']) + '</code>') if w['selector'] else '— (página toda)'}",
        f"🧪 Modo: {w['mode']}",
        f"🙈 Ignorar: {('<code>' + esc(w['ignore_regex']) + '</code>') if w['ignore_regex'] else '—'}",
        f"🔑 Palavras: {esc(w['keywords']) if w['keywords'] else '—'}",
        f"📣 Avisar: {NOTIFY_LABEL.get(w['notify'], w['notify'])}",
    ]
    if w["user_agent"]:
        lines.append(f"🕵️ User-Agent: <code>{esc(cut(w['user_agent'], 80))}</code>")
    status = "✅ ok" if not w["error_count"] else f"❌ {esc(cut(w['last_status'] or '', 200))} ({w['error_count']}x)"
    lines += [
        "",
        f"🕐 Última checagem: {monitor.fmt_time(w['last_check'])} ({ago(w['last_check'])})",
        f"📶 Status: {status}",
        f"🔔 Última mudança: {monitor.fmt_time(w['last_change'])}",
        f"📊 {w['checks']} checagens · {w['changes']} mudanças",
        f"📄 Snapshot: {len(w['content'].split(chr(10))) if w['content'] else 0} linhas",
    ]
    toggle = ("▶️ Retomar", f"res:{wid}") if w["paused"] else ("⏸ Pausar", f"pau:{wid}")
    kb = [
        [InlineKeyboardButton("🔄 Checar agora", callback_data=f"chk:{wid}"),
         InlineKeyboardButton(toggle[0], callback_data=toggle[1])],
        [InlineKeyboardButton("📄 Snapshot", callback_data=f"snap:{wid}"),
         InlineKeyboardButton("🧾 Último diff", callback_data=f"diff:{wid}")],
        [InlineKeyboardButton("🗑 Remover", callback_data=f"del:{wid}")],
    ]
    if w["url"].startswith(("http://", "https://")):
        kb[2].append(InlineKeyboardButton("🔗 Abrir", url=w["url"]))
    return "\n".join(lines), InlineKeyboardMarkup(kb)


# =================================================================== comandos
@guarded()
async def cmd_start(update: Update, ctx: Ctx):
    name = update.effective_user.first_name or ""
    await reply(update, f"👋 Olá, {esc(name)}! Eu aviso aqui quando um site mudar, mostrando "
                        f"exatamente o que foi <b>removido</b> e <b>adicionado</b>.\n\n"
                        f"Para começar, mande:\n<code>/add https://site.com 1m</code>\n\n"
                        f"Veja tudo em /help.")


@guarded()
async def cmd_help(update: Update, ctx: Ctx):
    extra = ADMIN_HELP if A(ctx).is_admin(update.effective_user.id) else ""
    await reply(update, HELP + extra)


async def _add(update: Update, ctx: Ctx, raw: str) -> None:
    cfg, st, mon = C(ctx), S(ctx), M(ctx)
    parts = raw.split(maxsplit=1)
    if not parts:
        await reply(update, "Uso: <code>/add &lt;url&gt; [intervalo] [seletor]</code>\n"
                            "Ex: <code>/add https://site.com 30s</code>")
        return
    url = normalize_url(parts[0])
    if not url:
        await reply(update, f"❌ URL inválida: <code>{esc(parts[0])}</code>")
        return
    rest = parts[1] if len(parts) > 1 else ""
    interval = cfg.default_interval
    if rest:
        first, _, after = rest.partition(" ")
        try:
            interval = parse_interval(first)
            rest = after.strip()
        except ValueError:
            pass
    selector = unquote(rest) or None
    if interval < cfg.min_interval:
        await reply(update, f"⚠️ Intervalo mínimo é {fmt_interval(cfg.min_interval)} (MIN_INTERVAL no .env).")
        return
    chat_id = update.effective_chat.id
    if st.count_chat(chat_id) >= cfg.max_watches_per_chat:
        await reply(update, f"⚠️ Limite de {cfg.max_watches_per_chat} monitores por chat atingido.")
        return
    dup = st.find(chat_id, url, selector)
    if dup:
        await reply(update, f"ℹ️ Esse site já está sendo monitorado: #{dup['id']} → /i{dup['id']}")
        return

    wid = st.add_watch(chat_id, url, interval, selector=selector)
    msg = await reply(update, f"⏳ Capturando a página pela primeira vez… (#{wid})")
    ev = await mon.check(wid)
    w = st.get(wid)
    head = (f"<b>Monitorando #{wid}</b>\n📋 {esc(cut(url, 300))}\n⏱ a cada <b>{fmt_interval(interval)}</b>"
            + (f" · 🧩 <code>{esc(selector)}</code>" if selector else ""))
    if ev and ev.kind == "error":
        text = (f"⚠️ {head}\n\nMas a primeira leitura falhou:\n❌ {esc(ev.error)}\n\n"
                f"Vou continuar tentando. Ajuste com /set {wid} … ou remova com /remove {wid}.")
    else:
        lines = (w["content"] or "").split("\n") if w["content"] else []
        text = (f"✅ {head}\n\n📄 {len(lines)} linhas capturadas. Prévia:\n{preview(lines)}\n"
                f"Te aviso aqui quando mudar. Opções: /i{wid}")
    try:
        await msg.edit_text(text)
    except TelegramError:
        await reply(update, text)


@guarded()
async def cmd_add(update: Update, ctx: Ctx):
    await _add(update, ctx, args_text(update))


@guarded()
async def on_plain_url(update: Update, ctx: Ctx):
    await _add(update, ctx, (update.effective_message.text or "").strip())


@guarded()
async def cmd_list(update: Update, ctx: Ctx):
    cfg = C(ctx)
    watches = S(ctx).list_chat(update.effective_chat.id)
    if not watches:
        await reply(update, "Nenhum monitor ainda. Use <code>/add https://site.com 1m</code>")
        return
    blocks = []
    for w in watches:
        icon = status_icon(w, cfg.error_alert_after)
        extra = []
        if w["selector"]:
            extra.append("🧩")
        if w["keywords"]:
            extra.append("🔑")
        if w["ignore_regex"]:
            extra.append("🙈")
        blocks.append(
            f"{icon} <b>#{w['id']}</b> {esc(short_title(w))} {' '.join(extra)}\n"
            f"    ⏱ {fmt_interval(w['interval'])} · 🕐 {ago(w['last_check'])} · 🔔 {w['changes']} · /i{w['id']}"
        )
    header = f"📋 <b>Seus monitores ({len(watches)})</b>\n🟢 ok · 🟡 falhou · 🔴 fora do ar · ⏸ pausado · ⏳ aguardando\n"
    chunk = header
    for b in blocks:
        if len(chunk) + len(b) + 2 > 3800:
            await reply(update, chunk)
            chunk = ""
        chunk += "\n" + b
    await reply(update, chunk)


@guarded()
async def cmd_info(update: Update, ctx: Ctx):
    m = re.match(r"^/i(?:nfo)?(?:@\w+)?\s*#?(\d*)", update.effective_message.text or "")
    raw = m.group(1) if m else ""
    w = await get_watch(update, ctx, raw, "/info 1")
    if w:
        text, kb = info_card(w, M(ctx), C(ctx))
        await reply(update, text, reply_markup=kb)


async def _check_many(update: Update, ctx: Ctx, watches: list[dict[str, Any]]) -> None:
    mon, st = M(ctx), S(ctx)
    msg = await reply(update, f"🔄 Checando {len(watches)} monitores…")
    sem = asyncio.Semaphore(C(ctx).max_concurrency)

    async def one(w):
        async with sem:
            return await mon.check(w["id"])

    events = [e for e in await asyncio.gather(*(one(w) for w in watches)) if e]
    counts = {"changed": 0, "unchanged": 0, "error": 0, "baseline": 0, "filtered": 0}
    errors = []
    for ev in events:
        counts[ev.kind] = counts.get(ev.kind, 0) + 1
        if ev.kind == "changed" or ev.recovered or ev.alert:
            await notify_event(ctx.bot, mon, st, ev)
        if ev.kind == "error":
            errors.append(f"❌ #{ev.watch['id']}: {esc(cut(ev.error, 150))}")
    summary = (f"🔄 <b>{len(events)} checados</b>\n🔔 {counts['changed']} com mudança · "
               f"✅ {counts['unchanged']} iguais · 🔕 {counts['filtered']} filtrados · "
               f"📸 {counts['baseline']} base · ❌ {counts['error']} erros")
    if errors:
        summary += "\n\n" + "\n".join(errors[:20])
    try:
        await msg.edit_text(summary)
    except TelegramError:
        await reply(update, summary)


@guarded()
async def cmd_check(update: Update, ctx: Ctx):
    raw = args_text(update)
    if not raw or raw.lower() in ALL_WORDS:
        watches = S(ctx).list_chat(update.effective_chat.id)
        if not watches:
            await reply(update, "Nenhum monitor ainda. Use /add.")
            return
        await _check_many(update, ctx, watches)
        return
    w = await get_watch(update, ctx, raw, "/check 1")
    if w:
        ev = await M(ctx).check(w["id"])
        if ev:
            await notify_event(ctx.bot, M(ctx), S(ctx), ev, manual=True)


async def _set_paused(update: Update, ctx: Ctx, paused: bool) -> None:
    raw = args_text(update)
    st = S(ctx)
    word = "pausado" if paused else "retomado"
    if raw.lower() in ALL_WORDS:
        watches = st.list_chat(update.effective_chat.id)
        for w in watches:
            st.update(w["id"], paused=int(paused), **({} if paused else {"next_check": time.time()}))
        await reply(update, f"{'⏸' if paused else '▶️'} {len(watches)} monitores {word}s.")
        return
    w = await get_watch(update, ctx, raw, "/pause 1" if paused else "/resume 1")
    if w:
        st.update(w["id"], paused=int(paused), **({} if paused else {"next_check": time.time()}))
        await reply(update, f"{'⏸' if paused else '▶️'} #{w['id']} {word}.")


@guarded()
async def cmd_pause(update: Update, ctx: Ctx):
    await _set_paused(update, ctx, True)


@guarded()
async def cmd_resume(update: Update, ctx: Ctx):
    await _set_paused(update, ctx, False)


@guarded()
async def cmd_remove(update: Update, ctx: Ctx):
    w = await get_watch(update, ctx, args_text(update), "/remove 1")
    if w:
        S(ctx).delete(w["id"])
        await reply(update, f"🗑 Monitor #{w['id']} removido ({esc(short_title(w))}).")


SET_USAGE = """⚙️ <b>Uso:</b> /set <code>&lt;id&gt; &lt;campo&gt; &lt;valor&gt;</code>

• <code>/set 1 intervalo 30s</code>
• <code>/set 1 seletor table.tableFile2</code> · <code>/set 1 seletor limpar</code>
• <code>/set 1 ignorar \\d{2}:\\d{2}:\\d{2}</code>
• <code>/set 1 palavras S-1, IPO</code>
• <code>/set 1 avisar add</code> (tudo | add | rem)
• <code>/set 1 modo json</code> (texto | html | json)
• <code>/set 1 nome SEC S-1</code>
• <code>/set 1 url https://novo-link.com</code>
• <code>/set 1 ua Seu Nome seu@email.com</code>"""


@guarded()
async def cmd_set(update: Update, ctx: Ctx):
    cfg, st, mon = C(ctx), S(ctx), M(ctx)
    raw = args_text(update)
    m = re.match(r"^#?(\d+)\s+(\S+)\s*(.*)$", raw, re.S)
    if not m:
        await reply(update, SET_USAGE)
        return
    w = await get_watch(update, ctx, m.group(1), "/set 1 intervalo 30s")
    if not w:
        return
    field = FIELD_ALIASES.get(m.group(2).lower())
    value = unquote(m.group(3))
    if not field:
        await reply(update, f"❓ Campo desconhecido: <code>{esc(m.group(2))}</code>\n\n{SET_USAGE}")
        return
    clear = value.lower() in CLEAR_WORDS
    wid = w["id"]

    if field == "interval":
        try:
            sec = parse_interval(value)
        except ValueError as exc:
            await reply(update, f"❌ {esc(exc)}")
            return
        if sec < cfg.min_interval:
            await reply(update, f"⚠️ Mínimo: {fmt_interval(cfg.min_interval)}")
            return
        st.update(wid, interval=sec, next_check=time.time() + sec)
        await reply(update, f"✅ #{wid} agora checa a cada <b>{fmt_interval(sec)}</b>.")
        return

    if field in ("name", "keywords", "ignore_regex", "notify"):
        if not value and field != "notify":
            await reply(update, f"Informe um valor (ou <code>limpar</code>).\n\n{SET_USAGE}")
            return
        if field == "notify":
            nv = NOTIFY_ALIASES.get(value.lower())
            if nv not in NOTIFY_MODES:
                await reply(update, "❌ Use: <code>tudo</code>, <code>add</code> ou <code>rem</code>")
                return
            new_value: Any = nv
        elif clear:
            new_value = None
        elif field == "ignore_regex":
            try:
                validate_regex(value)
            except ValueError as exc:
                await reply(update, f"❌ {esc(exc)}")
                return
            new_value = value
        else:
            new_value = value
        st.update(wid, **{field: new_value})
        label = {"name": "nome", "keywords": "palavras", "ignore_regex": "ignorar", "notify": "avisar"}[field]
        shown = "—" if new_value is None else f"<code>{esc(new_value)}</code>"
        await reply(update, f"✅ #{wid} {label}: {shown}")
        return

    # Campos que mudam a extração -> nova linha de base
    if field == "url":
        new_value = normalize_url(value) if value else None
        if not new_value:
            await reply(update, "❌ URL inválida.")
            return
    elif field == "mode":
        new_value = MODE_ALIASES.get(value.lower())
        if new_value not in MODES:
            await reply(update, "❌ Use: <code>texto</code>, <code>html</code> ou <code>json</code>")
            return
    else:  # selector / user_agent
        if not value:
            await reply(update, f"Informe um valor (ou <code>limpar</code>).\n\n{SET_USAGE}")
            return
        new_value = None if clear else value

    msg = await reply(update, "⏳ Testando a nova configuração…")
    ok, lines, err = await mon.rebaseline(wid, **{field: new_value})
    if not ok:
        text = f"❌ Não apliquei: {esc(err)}"
    elif lines is None:
        text = f"⚠️ Salvo, mas o site não respondeu agora ({esc(err)}). A linha de base será capturada na próxima checagem."
    else:
        text = f"✅ #{wid} atualizado. Nova linha de base: {len(lines)} linhas.\n{preview(lines)}"
    try:
        await msg.edit_text(text)
    except TelegramError:
        await reply(update, text)


async def _send_last_diff(bot, ctx: Ctx, w: dict[str, Any]) -> None:
    parsed = Diff.from_json(w["last_diff"])
    if not parsed:
        await safe_send(bot, w["chat_id"], f"ℹ️ #{w['id']} ainda não teve nenhuma mudança registrada.")
        return
    diff, at = parsed
    await send_diff(bot, M(ctx), S(ctx), w, diff, at, header="🧾 <b>ÚLTIMA MUDANÇA REGISTRADA</b>")


async def _send_snapshot(bot, ctx: Ctx, w: dict[str, Any]) -> None:
    if w["content"] is None:
        await safe_send(bot, w["chat_id"], f"ℹ️ #{w['id']} ainda não tem snapshot (aguardando primeira leitura).")
        return
    body = (f"# {title(w)}\n# {w['url']}\n# capturado em {M(ctx).fmt_time(w['last_check'])}\n\n"
            + w["content"] + "\n").encode("utf-8")
    n = len(w["content"].split("\n")) if w["content"] else 0
    await safe_doc(bot, w["chat_id"], body, f"snapshot_{w['id']}.txt", caption=f"📄 #{w['id']} — {n} linhas")


@guarded()
async def cmd_diff(update: Update, ctx: Ctx):
    w = await get_watch(update, ctx, args_text(update), "/diff 1")
    if w:
        await _send_last_diff(ctx.bot, ctx, w)


@guarded()
async def cmd_snapshot(update: Update, ctx: Ctx):
    w = await get_watch(update, ctx, args_text(update), "/snapshot 1")
    if w:
        await _send_snapshot(ctx.bot, ctx, w)


@guarded()
async def cmd_test(update: Update, ctx: Ctx):
    raw = args_text(update)
    parts = raw.split(maxsplit=1)
    if not parts:
        await reply(update, "Uso: <code>/test &lt;url&gt; [seletor]</code> — mostra o que eu enxergo na página, sem salvar.")
        return
    url = normalize_url(parts[0])
    if not url:
        await reply(update, "❌ URL inválida.")
        return
    selector = unquote(parts[1]) if len(parts) > 1 else None
    mon = M(ctx)
    msg = await reply(update, "⏳ Baixando…")
    try:
        res, lines = await mon.fetch_lines(url, "texto", selector,
                                           mon.user_agent_for({"url": url, "user_agent": None}))
        text = (f"🧪 <b>Teste</b> — HTTP {res.status} em {res.elapsed:.1f}s, {len(res.content) // 1024} KB\n"
                f"📄 {len(lines)} linhas de texto" + (f" (seletor <code>{esc(selector)}</code>)" if selector else "")
                + f"\n{preview(lines, n=25)}")
    except Exception as exc:  # FetchError / ExtractError
        text = f"❌ {esc(exc)}"
    try:
        await msg.edit_text(text)
    except TelegramError:
        await reply(update, text)


@guarded()
async def cmd_export(update: Update, ctx: Ctx):
    watches = S(ctx).list_chat(update.effective_chat.id)
    if not watches:
        await reply(update, "Nada para exportar ainda.")
        return
    data = {
        "app": "attsite-bot", "version": 1,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "watches": [{k: w[k] for k in EXPORT_FIELDS} for w in watches],
    }
    body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    await safe_doc(ctx.bot, update.effective_chat.id, body, "attsite-monitores.json",
                   caption=f"💾 {len(watches)} monitores. Para restaurar: /import e envie este arquivo.")


@guarded()
async def cmd_import(update: Update, ctx: Ctx):
    ctx.user_data["awaiting_import"] = True
    await reply(update, "📥 Envie agora o arquivo <b>.json</b> gerado pelo /export.")


def import_items(storage: Storage, cfg: Config, chat_id: int, items: list[Any]) -> tuple[int, int, list[str]]:
    added, skipped, errors = 0, 0, []
    now = time.time()
    for i, it in enumerate(items, 1):
        try:
            if not isinstance(it, dict):
                raise ValueError("item não é um objeto")
            url = normalize_url(str(it.get("url") or ""))
            if not url:
                raise ValueError("url inválida")
            interval = int(it.get("interval") or cfg.default_interval)
            interval = max(interval, cfg.min_interval)
            mode = it.get("mode") or "texto"
            notify = it.get("notify") or "tudo"
            if mode not in MODES or notify not in NOTIFY_MODES:
                raise ValueError("modo/avisar inválido")
            if it.get("ignore_regex"):
                validate_regex(it["ignore_regex"])
            selector = it.get("selector") or None
            if storage.find(chat_id, url, selector):
                skipped += 1
                continue
            if storage.count_chat(chat_id) >= cfg.max_watches_per_chat:
                raise ValueError("limite de monitores atingido")
            storage.add_watch(chat_id, url, interval, selector=selector, name=it.get("name") or None,
                              mode=mode, ignore_regex=it.get("ignore_regex") or None,
                              keywords=it.get("keywords") or None, notify=notify,
                              user_agent=it.get("user_agent") or None, paused=bool(it.get("paused")),
                              next_check=now + 2 * added)
            added += 1
        except (ValueError, TypeError) as exc:
            errors.append(f"item {i}: {exc}")
    return added, skipped, errors


@guarded()
async def on_document(update: Update, ctx: Ctx):
    msg = update.effective_message
    doc = msg.document
    wants = ctx.user_data.pop("awaiting_import", False) or (msg.caption or "").strip().startswith("/import")
    if not wants or doc is None:
        return
    if (doc.file_size or 0) > 2_000_000:
        await reply(update, "❌ Arquivo grande demais.")
        return
    try:
        f = await doc.get_file()
        data = json.loads(bytes(await f.download_as_bytearray()).decode("utf-8-sig"))
        items = data["watches"] if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ValueError
    except Exception:
        await reply(update, "❌ Não consegui ler esse arquivo. Envie o .json gerado pelo /export.")
        return
    added, skipped, errors = import_items(S(ctx), C(ctx), update.effective_chat.id, items)
    text = f"📥 Importados: <b>{added}</b> · já existiam: {skipped} · com erro: {len(errors)}"
    if errors:
        text += "\n" + "\n".join(esc(e) for e in errors[:15])
    if added:
        text += "\n\nAs linhas de base serão capturadas nos próximos segundos. Veja /list."
    await reply(update, text)


@guarded()
async def cmd_status(update: Update, ctx: Ctx):
    st = S(ctx).stats()
    up = int(time.time() - ctx.bot_data.get("started", time.time()))
    mine = S(ctx).count_chat(update.effective_chat.id)
    await reply(update,
                f"📈 <b>Status</b>\n"
                f"🤖 AttSite Bot v{__version__} · Python {platform.python_version()}\n"
                f"⏳ Online há {fmt_interval(up) if up else '0s'}\n"
                f"📋 Monitores: {st['total']} ({st['active']} ativos) em {st['chats']} chat(s) · neste chat: {mine}\n"
                f"⚠️ Com erro agora: {st['with_errors']}\n"
                f"🔄 {st['checks']} checagens · 🔔 {st['changes']} mudanças detectadas")


@guarded(admin=True)
async def cmd_allow(update: Update, ctx: Ctx):
    uid = parse_id(args_text(update))
    target = update.effective_message.reply_to_message
    if uid is None and target and target.from_user:
        uid = target.from_user.id
    if uid is None:
        await reply(update, "Uso: <code>/allow &lt;id_do_usuario&gt;</code> (a pessoa vê o ID dela ao mandar /start)")
        return
    A(ctx).allow(uid)
    await reply(update, f"✅ Usuário <code>{uid}</code> liberado.")


@guarded(admin=True)
async def cmd_deny(update: Update, ctx: Ctx):
    uid = parse_id(args_text(update))
    if uid is None:
        await reply(update, "Uso: <code>/deny &lt;id_do_usuario&gt;</code>")
        return
    ok = A(ctx).deny(uid)
    await reply(update, f"🚫 Usuário <code>{uid}</code> removido." if ok else "Esse ID não estava na lista.")


@guarded(admin=True)
async def cmd_users(update: Update, ctx: Ctx):
    acc = A(ctx)
    lines = ["👥 <b>Acesso</b>"]
    if acc.env_ids:
        lines.append("Admins (.env): " + ", ".join(f"<code>{i}</code>" for i in sorted(acc.env_ids)))
    if acc.owner():
        lines.append(f"Dono: <code>{acc.owner()}</code>")
    extra = sorted(acc.extra())
    lines.append("Liberados: " + (", ".join(f"<code>{i}</code>" for i in extra) if extra else "—"))
    await reply(update, "\n".join(lines))


# =================================================================== botões
@guarded()
async def on_callback(update: Update, ctx: Ctx):
    q = update.callback_query
    action, _, sid = (q.data or "").partition(":")
    wid = parse_id(sid)
    st, mon, cfg = S(ctx), M(ctx), C(ctx)
    w = st.get(wid) if wid else None
    if not w or q.message is None or w["chat_id"] != q.message.chat.id:
        await q.answer("Monitor não encontrado (talvez já removido).", show_alert=True)
        return

    async def refresh_card():
        fresh = st.get(wid)
        if fresh:
            text, kb = info_card(fresh, mon, cfg)
            try:
                await q.edit_message_text(text, reply_markup=kb)
            except BadRequest:
                pass  # "message is not modified"

    if action == "info":
        await q.answer()
        text, kb = info_card(w, mon, cfg)
        await safe_send(ctx.bot, w["chat_id"], text, kb, st)
    elif action == "chk":
        await q.answer("🔄 Checando…")
        ev = await mon.check(wid)
        if ev:
            await notify_event(ctx.bot, mon, st, ev, manual=True)
        await refresh_card()
    elif action in ("pau", "res"):
        paused = action == "pau"
        st.update(wid, paused=int(paused), **({} if paused else {"next_check": time.time()}))
        await q.answer("⏸ Pausado" if paused else "▶️ Retomado")
        await refresh_card()
    elif action == "snap":
        await q.answer()
        await _send_snapshot(ctx.bot, ctx, w)
    elif action == "diff":
        await q.answer()
        await _send_last_diff(ctx.bot, ctx, w)
    elif action == "del":
        await q.answer()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Sim, remover", callback_data=f"delok:{wid}"),
                                    InlineKeyboardButton("↩️ Cancelar", callback_data=f"back:{wid}")]])
        try:
            await q.edit_message_reply_markup(reply_markup=kb)
        except BadRequest:
            pass
    elif action == "delok":
        st.delete(wid)
        await q.answer("🗑 Removido")
        try:
            await q.edit_message_text(f"🗑 Monitor #{wid} removido ({esc(short_title(w))}).")
        except BadRequest:
            pass
    elif action == "back":
        await q.answer()
        await refresh_card()
    else:
        await q.answer()


async def on_error(update: object, ctx: Ctx) -> None:
    log.error("Erro tratando update: %s", ctx.error, exc_info=ctx.error)


# =================================================================== montagem
async def _post_init(app: Application) -> None:
    app.bot_data["started"] = time.time()
    try:
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in BOT_COMMANDS])
    except TelegramError as exc:
        log.warning("Não consegui registrar o menu de comandos: %s", exc)
    monitor: Monitor = app.bot_data["monitor"]
    storage: Storage = app.bot_data["storage"]

    async def on_event(ev: Event) -> None:
        await notify_event(app.bot, monitor, storage, ev)

    app.bot_data["scheduler"] = asyncio.create_task(monitor.run_forever(on_event))
    stats = storage.stats()
    log.info("Bot @%s online — %d monitores (%d ativos)", app.bot.username, stats["total"], stats["active"])
    print(f"\n  ✅ Bot @{app.bot.username} rodando! Abra o Telegram e mande /start para ele.\n"
          f"     Monitores: {stats['total']} ({stats['active']} ativos). Ctrl+C para parar.\n", flush=True)


async def _post_shutdown(app: Application) -> None:
    task = app.bot_data.get("scheduler")
    if task:
        task.cancel()
    await app.bot_data["monitor"].close()
    app.bot_data["storage"].close()
    log.info("Bot desligado")


def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["help", "ajuda"], cmd_help))
    app.add_handler(CommandHandler(["add", "adicionar", "monitorar"], cmd_add))
    app.add_handler(CommandHandler(["list", "lista", "listar"], cmd_list))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler(["check", "checar"], cmd_check))
    app.add_handler(CommandHandler(["pause", "pausar"], cmd_pause))
    app.add_handler(CommandHandler(["resume", "retomar"], cmd_resume))
    app.add_handler(CommandHandler(["remove", "remover", "del", "delete"], cmd_remove))
    app.add_handler(CommandHandler(["set", "config"], cmd_set))
    app.add_handler(CommandHandler("diff", cmd_diff))
    app.add_handler(CommandHandler("snapshot", cmd_snapshot))
    app.add_handler(CommandHandler(["test", "teste"], cmd_test))
    app.add_handler(CommandHandler(["export", "exportar"], cmd_export))
    app.add_handler(CommandHandler(["import", "importar"], cmd_import))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler(["allow", "liberar"], cmd_allow))
    app.add_handler(CommandHandler(["deny", "bloquear"], cmd_deny))
    app.add_handler(CommandHandler(["users", "usuarios"], cmd_users))
    app.add_handler(MessageHandler(filters.Regex(r"^/i\d+(?:@\w+)?\s*$"), cmd_info))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(r"(?i)^\s*(https?://|www\.)\S+\s*$"),
        on_plain_url))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)


def build_app(cfg: Config, *, monitor: Monitor | None = None, storage: Storage | None = None,
              request=None) -> Application:
    storage = storage or Storage(cfg.db_path)
    monitor = monitor or Monitor(storage, cfg)
    defaults = Defaults(parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True))
    builder = (ApplicationBuilder().token(cfg.token).defaults(defaults).concurrent_updates(True)
               .post_init(_post_init).post_shutdown(_post_shutdown))
    if request is not None:
        builder = builder.request(request)
    app = builder.build()
    app.bot_data.update(cfg=cfg, storage=storage, monitor=monitor, access=Access(storage, cfg.allowed_users))
    register_handlers(app)
    return app


def run(cfg: Config) -> None:
    app = build_app(cfg)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
