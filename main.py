import os
import asyncio
import logging
import json
from typing import Optional, List, Dict

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F, exceptions
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardMarkup, KeyboardButton
from aiogram.client.default import DefaultBotProperties

try:
    from flyerapi import Flyer, APIError as FlyerAPIError
    FLYER_AVAILABLE = True
except ImportError:
    FLYER_AVAILABLE = False
    FlyerAPIError = Exception

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID  = int(os.getenv("ADMIN_ID", "0") or "0")
PORT      = int(os.getenv("PORT", "10000"))

AXIONNA_API_KEY   = os.getenv("AXIONNA_API_KEY", "")
DARKBOOST_API_KEY = os.getenv("DARKBOOST_API_KEY", "")
FLYER_API_KEY     = os.getenv("FLYER_API_KEY", "")

flyer = Flyer(FLYER_API_KEY) if (FLYER_AVAILABLE and FLYER_API_KEY) else None

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

_http = None

# Задачи юзера: {user_id: [{"service": "axionna"/"darkboost"/"flyer", "id": ..., "link": ..., "signature": ...}]}
_user_tasks: Dict[int, List[dict]] = {}

_incomplete_statuses = ('incomplete', 'abort')


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
            json={"user_id": user_id, "max_sponsors": 20},
            headers={"Authorization": f"Bearer {AXIONNA_API_KEY}",
                     "Content-Type": "application/json"}) as r:
            body = await r.text()
            logging.info(f"AXIONNA /sponsors: status={r.status}, body={body[:300]}")
            if r.status != 200: return []
            d = json.loads(body)
            if d.get("status") == "ok":
                return [{
                    "service": "axionna",
                    "id": str(x.get("id")),
                    "link": x.get("link"),
                    "task": x.get("task", ""),
                    "reward_rub": x.get("reward", "0"),
                } for x in d.get("sponsors", []) if x.get("link") and x.get("id")]
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
            logging.info(f"AXIONNA /check: status={r.status}, body={body[:300]}")
            if r.status != 200: return {}
            d = json.loads(body)
            if d.get("status") == "ok":
                return {str(x.get("id")): (x.get("status") == "ok")
                        for x in d.get("results", [])}
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
            json={"user_id": user_id, "chat_id": user_id,
                  "username": username, "first_name": first_name,
                  "last_name": "", "language_code": "ru",
                  "is_premium": False, "max_sponsors": 20},
            headers={"Auth": DARKBOOST_API_KEY, "Content-Type": "application/json"}) as r:
            body = await r.text()
            logging.info(f"DARKBOOST /sponsors: status={r.status}, body={body[:300]}")
            if r.status != 200: return []
            d = json.loads(body)
            if d.get("ok") and d.get("status") == "ok":
                session_id = d.get("session_id")
                return [{
                    "service": "darkboost",
                    "id": str(x.get("id")) if x.get("id") else x.get("link"),
                    "link": x.get("link"),
                    "session_id": session_id,
                } for x in d.get("sponsors", []) if x.get("link")]
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
            logging.info(f"DARKBOOST /check: status={r.status}, body={body[:300]}")
            if r.status != 200: return {}
            return json.loads(body)
    except Exception as e:
        logging.error(f"DarkBoost check: {e}")
    return {}


# ═══════════════════════════════════════════════
# FLYER
# ═══════════════════════════════════════════════

async def fetch_flyer(user_id, lang="ru"):
    if not flyer: return []
    try:
        tasks = await flyer.get_tasks(user_id=user_id, language_code=lang or "ru", limit=20)
        logging.info(f"FLYER get_tasks: {len(tasks) if tasks else 0}")
        if not tasks: return []
        incomplete = [t for t in tasks if t.get('status') in _incomplete_statuses]
        out = []
        for t in incomplete:
            links = t.get('links', [])
            if not links: continue
            out.append({
                "service": "flyer",
                "id": t.get('signature'),
                "link": links[0],  # берём первую ссылку
                "signature": t.get('signature'),
                "task": t.get('task', ''),
            })
        return out
    except FlyerAPIError as e:
        logging.error(f"Flyer get_tasks APIError: {e}")
    except Exception as e:
        logging.error(f"Flyer get_tasks unknown: {e}")
    return []


async def check_flyer(signature):
    if not flyer or not signature: return None
    try:
        return await flyer.check_task(signature=signature)
    except FlyerAPIError as e:
        logging.error(f"Flyer check_task: {e}")
    except Exception as e:
        logging.error(f"Flyer check_task unknown: {e}")
    return None


# ═══════════════════════════════════════════════
# СБОР ВСЕХ ЗАДАЧ
# ═══════════════════════════════════════════════

async def collect_all_tasks(user):
    """Собирает задания со всех сервисов."""
    uid = user.id
    un = user.username or ""
    fn = user.first_name or ""
    lang = user.language_code or "ru"

    ax, db, fl = await asyncio.gather(
        fetch_axionna(uid),
        fetch_darkboost(uid, un, fn),
        fetch_flyer(uid, lang),
        return_exceptions=False,
    )

    all_tasks = ax + db + fl
    logging.info(f"Всего задач: {len(all_tasks)} | Axionna: {len(ax)} | DarkBoost: {len(db)} | Flyer: {len(fl)}")
    return all_tasks# ═══════════════════════════════════════════════
# КЛАВИАТУРА
# ═══════════════════════════════════════════════

BUTTON_TEXTS = {
    'start bot': '➕ Запустить бота',
    'subscribe channel': '➕ Подписаться',
    'give boost': '➕ Голосовать',
    'follow link': '➕ Перейти',
    'perform action': '➕ Выполнить',
}


def tasks_keyboard(tasks):
    keyboard = {'inline_keyboard': [[]]}
    for i, t in enumerate(tasks, 1):
        label = f"🔗 {t['service']} #{i}"
        if t.get('task'):
            label = BUTTON_TEXTS.get(t['task'], f"🔗 {t['service']} #{i}")
        if len(keyboard['inline_keyboard'][-1]) == 2:
            keyboard['inline_keyboard'].append([])
        keyboard['inline_keyboard'][-1].append({'text': label, 'url': t['link']})

    keyboard['inline_keyboard'].append([{
        'text': '☑️ Проверить подписки',
        'callback_data': 'check',
    }])
    return keyboard


def main_menu():
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎯 Задания"), KeyboardButton(text="📊 Статистика")],
        ],
        resize_keyboard=True, is_persistent=True
    )
    return kb


# ═══════════════════════════════════════════════
# ПОКАЗ ЗАДАНИЙ
# ═══════════════════════════════════════════════

async def show_tasks(user_id, lang, message_id=None, user_obj=None):
    if user_obj is None:
        user_obj = types.User(id=user_id, is_bot=False, first_name="", language_code=lang)

    tasks = await collect_all_tasks(user_obj)

    if not tasks:
        if message_id is None:
            await bot.send_message(user_id, "🎉 <b>Нет новых заданий!</b>\n\nВозвращайся позже.")
        return True

    _user_tasks[user_id] = tasks

    by_service = {}
    for t in tasks:
        by_service[t['service']] = by_service.get(t['service'], 0) + 1

    text = (
        f"🎯 <b>Заданий: {len(tasks)}</b>\n\n"
        + "\n".join(f"• {s}: <b>{c}</b>" for s, c in by_service.items())
        + f"\n\nПодпишись на все каналы и жми <b>«☑️ Проверить подписки»</b>."
    )

    keyboard = tasks_keyboard(tasks)

    if message_id is None:
        await bot.send_message(chat_id=user_id, text=text, parse_mode='HTML',
                               reply_markup=keyboard)
    else:
        try:
            await bot.edit_message_text(chat_id=user_id, message_id=message_id,
                                        text=text, parse_mode='HTML',
                                        reply_markup=keyboard)
        except exceptions.MessageNotModified:
            pass
    return False


# ═══════════════════════════════════════════════
# ПРОВЕРКА ПОДПИСОК
# ═══════════════════════════════════════════════

async def check_all(user_id):
    tasks = _user_tasks.get(user_id, [])
    if not tasks: return 0, 0

    ax, db, fl = [], [], []
    for t in tasks:
        if t['service'] == 'axionna': ax.append(t['id'])
        elif t['service'] == 'darkboost': db.append(t)
        elif t['service'] == 'flyer': fl.append(t['signature'])

    done = 0

    # Axionna
    if ax:
        res = await check_axionna(user_id, ax)
        for aid, ok in res.items():
            if ok:
                done += 1
                logging.info(f"Axionna OK: {aid}")

    # DarkBoost
    if db:
        session_id = db[0].get('session_id')
        res = await check_darkboost(user_id, session_id)
        if res.get('status') == 'ok':
            done += len(db)
            logging.info(f"DarkBoost OK: {len(db)} задач")
        else:
            logging.info(f"DarkBoost status: {res.get('status')}")

    # Flyer
    for sig in fl:
        status = await check_flyer(sig)
        logging.info(f"Flyer {sig[:12]}... → {status}")
        if status == 'complete':
            done += 1

    return done, len(tasks)


# ═══════════════════════════════════════════════
# ХЕНДЛЕРЫ
# ═══════════════════════════════════════════════

@dp.message(CommandStart())
async def cmd_start(msg: types.Message):
    uid = msg.from_user.id
    lang = msg.from_user.language_code or "ru"
    logging.info(f"=== /start from {uid} (@{msg.from_user.username}) ===")

    ok = await show_tasks(uid, lang, user_obj=msg.from_user)
    if ok:
        await msg.answer("🏠 <b>Главное меню</b>", reply_markup=main_menu())


@dp.message(F.text == "🎯 Задания")
async def msg_tasks(msg: types.Message):
    await show_tasks(msg.from_user.id, msg.from_user.language_code or "ru",
                     user_obj=msg.from_user)


@dp.message(F.text == "📊 Статистика")
async def msg_stats(msg: types.Message):
    tasks = _user_tasks.get(msg.from_user.id, [])
    by_service = {}
    for t in tasks:
        by_service[t['service']] = by_service.get(t['service'], 0) + 1
    text = f"📊 <b>Твои задачи: {len(tasks)}</b>\n\n"
    for s, c in by_service.items():
        text += f"• {s}: <b>{c}</b>\n"
    await msg.answer(text, reply_markup=main_menu())


@dp.callback_query(F.data == "check")
async def cb_check(cq: types.CallbackQuery):
    uid = cq.from_user.id
    lang = cq.from_user.language_code or "ru"

    await cq.answer("Проверяю…")
    logging.info(f"=== check from {uid} ===")

    done, total = await check_all(uid)

    if done > 0:
        await cq.answer(f"✅ Засчитано: {done}/{total}", show_alert=True)
    else:
        await cq.answer(f"❌ Ничего не засчитано", show_alert=True)

    ok = await show_tasks(uid, lang, cq.message.message_id, user_obj=cq.from_user)
    if ok:
        try:
            await cq.message.delete()
        except Exception:
            pass
        await cq.message.answer("🎉 <b>Все задания выполнены!</b>",
                                reply_markup=main_menu())


# ═══════════════════════════════════════════════
# АДМИН
# ═══════════════════════════════════════════════

@dp.message(Command("admin"))
async def cmd_admin(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    text = "🔑 <b>Ключи сервисов:</b>\n\n"
    text += f"{'✅' if AXIONNA_API_KEY else '❌'} Axionna\n"
    text += f"{'✅' if DARKBOOST_API_KEY else '❌'} DarkBoost\n"
    text += f"{'✅' if FLYER_API_KEY else '❌'} Flyer API\n"
    text += f"{'✅' if FLYER_AVAILABLE else '❌'} flyerapi\n"
    text += f"{'✅' if flyer else '❌'} Flyer instance\n"
    await msg.answer(text)


@dp.message(Command("flyer_me"))
async def cmd_flyer_me(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    if not flyer: return
    try:
        info = await flyer.get_me()
        await msg.answer(f"<code>{json.dumps(info, ensure_ascii=False, indent=2)[:3500]}</code>")
    except Exception as e:
        await msg.answer(f"❌ {e}")


@dp.message(Command("flyer_debug"))
async def cmd_flyer_debug(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    if not flyer:
        await msg.answer("❌ Flyer не настроен"); return
    try:
        tasks = await flyer.get_tasks(user_id=msg.from_user.id, language_code="ru", limit=20)
        if not tasks:
            await msg.answer("📭 Flyer: пустой список."); return
        text = f"📋 <b>Flyer задач: {len(tasks)}</b>\n\n<code>{json.dumps(tasks, ensure_ascii=False, indent=2)[:3000]}</code>"
        await msg.answer(text)
    except Exception as e:
        await msg.answer(f"❌ {e}")


@dp.message(Command("axionna_debug"))
async def cmd_axionna_debug(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    tasks = await fetch_axionna(msg.from_user.id)
    if not tasks:
        await msg.answer("📭 Axionna: пусто"); return
    await msg.answer(f"📋 <b>Axionna: {len(tasks)}</b>\n\n<code>{json.dumps(tasks, ensure_ascii=False)[:2000]}</code>")


@dp.message(Command("darkboost_debug"))
async def cmd_db_debug(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    tasks = await fetch_darkboost(msg.from_user.id, msg.from_user.username or "", msg.from_user.first_name or "")
    if not tasks:
        await msg.answer("📭 DarkBoost: пусто"); return
    await msg.answer(f"📋 <b>DarkBoost: {len(tasks)}</b>\n\n<code>{json.dumps(tasks, ensure_ascii=False)[:2000]}</code>")


# ═══════════════════════════════════════════════
# WEB + MAIN
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
