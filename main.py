import os
import time
import asyncio
import logging
import json
import hmac
import hashlib
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID  = int(os.getenv("ADMIN_ID", "0") or "0")
PORT      = int(os.getenv("PORT", "10000"))

AXIONNA_API_KEY            = os.getenv("AXIONNA_API_KEY", "")
AXIONNA_WEBHOOK_SECRET     = os.getenv("AXIONNA_WEBHOOK_SECRET", "")
DARKBOOST_API_KEY          = os.getenv("DARKBOOST_API_KEY", "")
DARKBOOST_WEBHOOK_SECRET   = os.getenv("DARKBOOST_WEBHOOK_SECRET", "")

MAX_SPONSORS = 20

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

_http = None
_user_tasks = {}


async def http():
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    return _http


# ═══════════════════════════════════════════════
# AXIONNA
# ═══════════════════════════════════════════════

async def fetch_axionna(user_id):
    if not AXIONNA_API_KEY: return []
    s = await http()
    try:
        async with s.post("https://axionna.org/api/sponsors",
            json={"user_id": user_id, "max_sponsors": MAX_SPONSORS},
            headers={"Authorization": f"Bearer {AXIONNA_API_KEY}",
                     "Content-Type": "application/json"}) as r:
            body = await r.text()
            logging.info(f"AXIONNA /sponsors: status={r.status}, body={body[:500]}")
            if r.status != 200: return []
            d = json.loads(body)
            if d.get("status") != "ok":
                logging.warning(f"Axionna: status={d.get('status')}")
                return []
            out = []
            for x in d.get("sponsors", []):
                link = x.get("link")
                task_id = x.get("id")
                if link and task_id:
                    out.append({
                        "service": "axionna",
                        "id": str(task_id),
                        "link": link,
                        "task": x.get("task", ""),
                        "reward": x.get("reward", "0"),
                    })
            logging.info(f"Axionna: получено {len(out)} заданий")
            return out
    except Exception as e:
        logging.error(f"Axionna fetch: {e}")
    return []


async def check_axionna(user_id, task_ids):
    """Засчитывает ТОЛЬКО по status == 'ok'. Поля 'credited' в API НЕТ."""
    if not AXIONNA_API_KEY or not task_ids: return {}
    s = await http()
    try:
        async with s.post("https://axionna.org/api/check",
            json={"user_id": user_id, "task_ids": task_ids},
            headers={"Authorization": f"Bearer {AXIONNA_API_KEY}",
                     "Content-Type": "application/json"}) as r:
            body = await r.text()
            logging.info(f"AXIONNA /check: status={r.status}, body={body[:500]}")
            if r.status != 200: return {}
            d = json.loads(body)
            if d.get("status") != "ok": return {}
            res = {}
            for x in d.get("results", []):
                tid = str(x.get("id"))
                st = x.get("status")
                ok = (st == "ok")
                res[tid] = {
                    "ok": ok,
                    "status": st,
                    "subscribed": x.get("subscribed"),
                    "reward": x.get("reward"),
                    "error": x.get("error"),
                }
            return res
    except Exception as e:
        logging.error(f"Axionna check: {e}")
    return {}


# ═══════════════════════════════════════════════
# DARKBOOST
# ═══════════════════════════════════════════════

async def fetch_darkboost(user_id, username="", first_name=""):
    if not DARKBOOST_API_KEY: return []
    s = await http()
    try:
        async with s.post("https://darkboosts.com/api/v1/sponsors",
            json={
                "user_id": user_id, "chat_id": user_id,
                "username": username, "first_name": first_name,
                "last_name": "", "language_code": "ru",
                "is_premium": False, "max_sponsors": MAX_SPONSORS,
            },
            headers={"Auth": DARKBOOST_API_KEY, "Content-Type": "application/json"}) as r:
            body = await r.text()
            logging.info(f"DARKBOOST /sponsors: status={r.status}, body={body[:500]}")
            if r.status != 200: return []
            d = json.loads(body)
            if not (d.get("ok") and d.get("status") == "ok"):
                logging.warning(f"DarkBoost: ok={d.get('ok')}, status={d.get('status')}")
                return []
            session_id = d.get("session_id")
            out = []
            for x in d.get("sponsors", []):
                link = x.get("link")
                task_id = x.get("id")
                if link:
                    out.append({
                        "service": "darkboost",
                        "id": str(task_id) if task_id else link,
                        "link": link,
                        "session_id": session_id,
                    })
            logging.info(f"DarkBoost: {len(out)} заданий, session_id={session_id}")
            return out
    except Exception as e:
        logging.error(f"DarkBoost fetch: {e}")
    return []


async def check_darkboost(user_id, session_id):
    if not DARKBOOST_API_KEY or not session_id: return {}
    s = await http()
    try:
        async with s.post("https://darkboosts.com/api/v1/check",
            json={"user_id": user_id, "chat_id": user_id, "session_id": int(session_id)},
            headers={"Auth": DARKBOOST_API_KEY, "Content-Type": "application/json"}) as r:
            body = await r.text()
            logging.info(f"DARKBOOST /check: status={r.status}, body={body[:500]}")
            if r.status != 200: return {}
            return json.loads(body)
    except Exception as e:
        logging.error(f"DarkBoost check: {e}")
    return {}


# ═══════════════════════════════════════════════
# ВЕБХУК: DARKBOOST (HMAC-SHA256)
# ═══════════════════════════════════════════════

async def darkboost_webhook(request):
    try:
        body = await request.read()
        sig = request.headers.get("X-DarkBoost-Signature", "")
        event_type = request.headers.get("X-DarkBoost-Event", "")
        event_id = request.headers.get("X-DarkBoost-Event-Id", "")

        logging.info(f"DarkBoost webhook: event={event_type}, id={event_id}")
        logging.info(f"DarkBoost webhook body: {body[:500]}")

        if DARKBOOST_WEBHOOK_SECRET:
            expected = "sha256=" + hmac.new(
                DARKBOOST_WEBHOOK_SECRET.encode(), body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig, expected):
                logging.error(f"DarkBoost webhook: bad signature")
                return web.Response(status=403, text="bad signature")
            logging.info("DarkBoost webhook: signature OK")
        else:
            logging.warning("DarkBoost: DARKBOOST_WEBHOOK_SECRET не задан")

        try:
            event = json.loads(body)
        except Exception:
            event = {}

        logging.info(f"DarkBoost payload: {event}")

        user_id = event.get("user_id") or event.get("tg_user_id")
        service = event.get("service", "darkboost")

        if event_type == "subscription" and user_id:
            logging.info(f"✅ DarkBoost SUBSCRIPTION: user={user_id}, service={service}")
        elif event_type == "unsubscription" and user_id:
            logging.info(f"❌ DarkBoost UNSUBSCRIPTION: user={user_id}, service={service}")

        return web.Response(status=200, text="ok")
    except Exception as e:
        logging.error(f"DarkBoost webhook error: {e}")
        return web.Response(status=200, text="ok")


async def darkboost_health(request):
    return web.Response(text="darkboost webhook alive")


# ═══════════════════════════════════════════════
# ВЕБХУК: AXIONNA (X-Axionna-Webhook-Secret)
# ═══════════════════════════════════════════════

async def axionna_webhook(request):
    try:
        body = await request.read()
        secret = request.headers.get("X-Axionna-Webhook-Secret", "")
        idem = request.headers.get("Idempotency-Key", "")

        logging.info(f"Axionna webhook: idem={idem}")
        logging.info(f"Axionna webhook body: {body[:500]}")

        if AXIONNA_WEBHOOK_SECRET and not hmac.compare_digest(secret, AXIONNA_WEBHOOK_SECRET):
            logging.error("Axionna webhook: bad secret")
            return web.Response(status=403, text="bad secret")

        try:
            event = json.loads(body)
        except Exception:
            event = {}

        logging.info(f"Axionna payload: {event}")

        ev = event.get("event")
        user_id = event.get("tg_user_id")
        chat_id = event.get("chat_id")

        if ev == "subscriber.unsubscribed" and user_id:
            logging.info(f"❌ Axionna UNSUBSCRIBE: user={user_id}, chat={chat_id}")

        return web.Response(status=200, text="ok")
    except Exception as e:
        logging.error(f"Axionna webhook error: {e}")
        return web.Response(status=200, text="ok")


async def axionna_health(request):
    return web.Response(text="axionna webhook alive")


# ═══════════════════════════════════════════════
# КЛАВИАТУРА
# ═══════════════════════════════════════════════

def sponsors_kb(tasks):
    kb = InlineKeyboardBuilder()
    for i, t in enumerate(tasks, 1):
        label = f"🔗 {t['service']} #{i}"
        if t.get("task"):
            label += f" · {t['task']}"
        kb.button(text=label, url=t["link"])
    kb.button(text="✅ Проверить подписки", callback_data="check")
    rows = [1] * len(tasks)
    rows.append(1)
    kb.adjust(*rows)
    return kb.as_markup()


# ═══════════════════════════════════════════════
# ХЕНДЛЕРЫ
# ═══════════════════════════════════════════════

@dp.message(CommandStart())
async def cmd_start(msg: types.Message):
    uid = msg.from_user.id
    un = msg.from_user.username or ""
    fn = msg.from_user.first_name or ""

    logging.info(f"=== /start from {uid} (@{un}) ===")

    axionna_tasks, darkboost_tasks = await asyncio.gather(
        fetch_axionna(uid),
        fetch_darkboost(uid, un, fn),
    )

    all_tasks = axionna_tasks + darkboost_tasks
    _user_tasks[uid] = all_tasks

    logging.info(f"Всего: {len(all_tasks)} | Axionna: {len(axionna_tasks)} | DarkBoost: {len(darkboost_tasks)}")

    if not all_tasks:
        await msg.answer(
            "😕 <b>Спонсоров нет.</b>\n\n"
            "• Ключ не задан\n"
            "• Нет офферов\n"
            "• API вернул ошибку\n\n"
            "Смотри логи BotHost."
        )
        return

    text = (
        f"🎯 <b>Заданий: {len(all_tasks)}</b>\n\n"
        f"• Axionna: <b>{len(axionna_tasks)}</b>\n"
        f"• DarkBoost: <b>{len(darkboost_tasks)}</b>\n\n"
        f"Подпишись и жми «Проверить»."
    )
    await msg.answer(text, reply_markup=sponsors_kb(all_tasks))


@dp.callback_query(F.data == "check")
async def cb_check(cq: types.CallbackQuery):
    uid = cq.from_user.id
    tasks = _user_tasks.get(uid, [])
    if not tasks:
        await cq.answer("Задачи устарели. /start заново.", show_alert=True)
        return

    await cq.answer("Проверяю…")

    axionna_tasks = [t for t in tasks if t["service"] == "axionna"]
    darkboost_tasks = [t for t in tasks if t["service"] == "darkboost"]

    report = "🔍 <b>Результат проверки</b>\n\n"

    # ─── AXIONNA ───
    if axionna_tasks:
        report += "<b>AXIONNA</b>\n"
        ids = [t["id"] for t in axionna_tasks]
        res = await check_axionna(uid, ids)
        done = 0
        for t in axionna_tasks:
            info = res.get(t["id"])
            if not info:
                report += f"• <code>{t['id']}</code> — ❓ нет ответа\n"
                continue
            if info["ok"]:
                done += 1
                report += f"• <code>{t['id']}</code> — ✅ {info.get('reward')} ₽\n"
            else:
                report += (
                    f"• <code>{t['id']}</code> — ❌ "
                    f"status={info.get('status')}"
                    f"{', err=' + str(info.get('error')) if info.get('error') else ''}\n"
                )
        report += f"Итого: <b>{done}/{len(axionna_tasks)}</b>\n\n"

    # ─── DARKBOOST ───
    if darkboost_tasks:
        report += "<b>DARKBOOST</b>\n"
        session_id = darkboost_tasks[0].get("session_id")
        raw = await check_darkboost(uid, session_id)

        status = raw.get("status")
        missing = raw.get("missing", [])

        if status == "ok":
            report += f"• Все задачи: ✅ подтверждены\n"
            report += f"• Итого: <b>{len(darkboost_tasks)}/{len(darkboost_tasks)}</b>\n"
        elif status == "cooldown":
            report += f"• ⏳ cooldown, попробуй позже\n"
        elif status == "not_found":
            report += f"• ❌ сессия не найдена, /start заново\n"
        elif status == "no_offers":
            report += f"• ❌ нет офферов\n"
        else:
            report += f"• ❌ status={status}\n"
            report += f"• missing: <b>{len(missing)}</b>\n"

        report += f"• session_id: <code>{session_id}</code>\n"
        report += f"• raw: <code>{json.dumps(raw, ensure_ascii=False)[:200]}</code>\n\n"

    report += "<i>Детали — в логах BotHost.</i>"

    try:
        await cq.message.edit_text(report)
    except Exception:
        await cq.message.answer(report)


@dp.message(Command("admin"))
async def cmd_admin(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    text = "🔑 <b>Ключи:</b>\n\n"
    text += f"{'✅' if AXIONNA_API_KEY else '❌'} Axionna API\n"
    text += f"{'✅' if AXIONNA_WEBHOOK_SECRET else '❌'} Axionna Webhook Secret\n"
    text += f"{'✅' if DARKBOOST_API_KEY else '❌'} DarkBoost API\n"
    text += f"{'✅' if DARKBOOST_WEBHOOK_SECRET else '❌'} DarkBoost Webhook Secret\n"
    await msg.answer(text)


@dp.message(Command("webhook_test"))
async def cmd_webhook_test(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    base = os.getenv("SELF_URL", "https://bot-1791164325-1596-ott08319.bothost.tech")
    await msg.answer(
        f"🔗 <b>Эндпоинты вебхуков:</b>\n\n"
        f"<b>Axionna:</b>\n<code>{base}/webhook/axionna</code>\n\n"
        f"<b>DarkBoost:</b>\n<code>{base}/darkboost/webhook</code>\n\n"
        f"Открой в браузере — оба должны вернуть <b>... alive</b>."
    )


# ═══════════════════════════════════════════════
# WEB + MAIN
# ═══════════════════════════════════════════════

async def health(_):
    return web.Response(text="ok")


async def start_web():
    app = web.Application()
    app.router.add_get("/", health)
    # DarkBoost
    app.router.add_post("/darkboost/webhook", darkboost_webhook)
    app.router.add_get("/darkboost/webhook", darkboost_health)
    # Axionna
    app.router.add_post("/webhook/axionna", axionna_webhook)
    app.router.add_get("/webhook/axionna", axionna_health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info(f"Web on :{PORT}")
    logging.info(f"Endpoints: /webhook/axionna, /darkboost/webhook")


async def on_shutdown(*args, **kwargs):
    global _http
    if _http and not _http.closed:
        await _http.close()
        logging.info("HTTP session closed")


async def main():
    await start_web()
    dp.shutdown.register(on_shutdown)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
