import os
import time
import asyncio
import logging
import json
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

AXIONNA_API_KEY   = os.getenv("AXIONNA_API_KEY", "")
DARKBOOST_API_KEY = os.getenv("DARKBOOST_API_KEY", "")

MAX_SPONSORS = 20

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

_http = None
_user_tasks = {}  # {user_id: [{"service","id","link","signature"}]}


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

            if r.status != 200:
                return []

            d = json.loads(body)
            if d.get("status") != "ok":
                logging.warning(f"Axionna: status={d.get('status')}")
                return []

            sponsors = d.get("sponsors", [])
            out = []
            for x in sponsors:
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
    if not AXIONNA_API_KEY or not task_ids: return {}
    s = await http()
    try:
        async with s.post("https://axionna.org/api/check",
            json={"user_id": user_id, "task_ids": task_ids},
            headers={"Authorization": f"Bearer {AXIONNA_API_KEY}",
                     "Content-Type": "application/json"}) as r:
            body = await r.text()
            logging.info(f"AXIONNA /check: status={r.status}, body={body[:500]}")

            if r.status != 200:
                return {}

            d = json.loads(body)
            if d.get("status") != "ok":
                return {}

            res = {}
            for x in d.get("results", []):
                tid = str(x.get("id"))
                # Засчитываем ТОЛЬКО если subscribed И credited
                ok = bool(x.get("subscribed")) and bool(x.get("credited"))
                res[tid] = {
                    "ok": ok,
                    "status": x.get("status"),
                    "subscribed": x.get("subscribed"),
                    "credited": x.get("credited"),
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

            if r.status != 200:
                return []

            d = json.loads(body)
            if not (d.get("ok") and d.get("status") == "ok"):
                logging.warning(f"DarkBoost: ok={d.get('ok')}, status={d.get('status')}")
                return []

            session_id = d.get("session_id")
            sponsors = d.get("sponsors", [])
            out = []
            for x in sponsors:
                link = x.get("link")
                task_id = x.get("id")
                if link:
                    out.append({
                        "service": "darkboost",
                        "id": str(task_id) if task_id else link,
                        "link": link,
                        "session_id": session_id,
                    })
            logging.info(f"DarkBoost: получено {len(out)} заданий, session_id={session_id}")
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

            if r.status != 200:
                return {}

            d = json.loads(body)
            if d.get("status") == "ok":
                return {"ok": True, "raw": d}
            else:
                return {"ok": False, "raw": d}
    except Exception as e:
        logging.error(f"DarkBoost check: {e}")
    return {}


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
    rows = [1] * len(tasks)  # по одному в ряд для теста (видно сервис)
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

    # Параллельно
    axionna_tasks, darkboost_tasks = await asyncio.gather(
        fetch_axionna(uid),
        fetch_darkboost(uid, un, fn),
        return_exceptions=False,
    )

    all_tasks = axionna_tasks + darkboost_tasks
    _user_tasks[uid] = all_tasks

    logging.info(f"Всего задач: {len(all_tasks)} | Axionna: {len(axionna_tasks)} | DarkBoost: {len(darkboost_tasks)}")

    if not all_tasks:
        await msg.answer(
            "😕 <b>Спонсоров нет.</b>\n\n"
            "Возможно:\n"
            "• Ключ Axionna / DarkBoost не задан\n"
            "• Нет активных офферов для тебя\n"
            "• API вернул ошибку\n\n"
            "Смотри <b>логи BotHost</b> — там видно ответ каждого сервиса."
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
        await cq.answer("Задачи устарели. Напиши /start заново.", show_alert=True)
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
                    f"sub={info.get('subscribed')}, cred={info.get('credited')}"
                    f"{', err=' + str(info.get('error')) if info.get('error') else ''}\n"
                )
        report += f"Итого: <b>{done}/{len(axionna_tasks)}</b>\n\n"

    # ─── DARKBOOST ───
    if darkboost_tasks:
        report += "<b>DARKBOOST</b>\n"
        # Берём session_id из первой задачи (у всех одинаковый)
        session_id = darkboost_tasks[0].get("session_id")
        res = await check_darkboost(uid, session_id)

        if res.get("ok"):
            report += f"• Все задачи: ✅ подтверждены\n"
            report += f"• Итого: <b>{len(darkboost_tasks)}/{len(darkboost_tasks)}</b>\n"
        else:
            raw = res.get("raw", {})
            report += f"• Все задачи: ❌ не подтверждены\n"
            report += f"• Ответ API: <code>{json.dumps(raw, ensure_ascii=False)[:200]}</code>\n"
            report += f"• Итого: <b>0/{len(darkboost_tasks)}</b>\n"

        report += f"• session_id: <code>{session_id}</code>\n\n"

    report += "<i>Детали смотри в логах BotHost.</i>"

    try:
        await cq.message.edit_text(report)
    except Exception:
        await cq.message.answer(report)


@dp.message(Command("admin"))
async def cmd_admin(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    text = "🔑 <b>Ключи сервисов:</b>\n\n"
    text += f"{'✅' if AXIONNA_API_KEY else '❌'} Axionna\n"
    text += f"{'✅' if DARKBOOST_API_KEY else '❌'} DarkBoost\n"
    text += "\n<i>Добавь ключ в BotHost → Переменные окружения → перезапусти бота.</i>"
    await msg.answer(text)


# ═══════════════════════════════════════════════
# ВЕБ + MAIN
# ═══════════════════════════════════════════════

async def health(_):
    return web.Response(text="ok")


async def main():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info(f"Web on :{PORT}")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
