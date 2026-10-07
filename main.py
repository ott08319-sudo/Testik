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

FLYER_API_KEY = os.getenv("FLYER_API_KEY", "")

flyer = Flyer(FLYER_API_KEY) if (FLYER_AVAILABLE and FLYER_API_KEY) else None

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

_user_tasks: Dict[int, List[dict]] = {}

_incomplete_statuses = ('incomplete', 'abort')
REWARD = 100.0

BUTTON_TEXTS = {
    'start bot': '➕ Запустить бота',
    'subscribe channel': '➕ Подписаться',
    'give boost': '➕ Голосовать',
    'follow link': '➕ Перейти',
    'perform action': '➕ Выполнить',
}


# ═══════════════════════════════════════════════
# FLYER: получение заданий
# ═══════════════════════════════════════════════

async def fetch_flyer_tasks(user_id: int, language_code: str = "ru") -> List[dict]:
    if not flyer:
        logging.warning("Flyer не настроен")
        return []

    try:
        tasks = await flyer.get_tasks(user_id=user_id, language_code=language_code or "ru", limit=20)
        logging.info(f"Flyer get_tasks for {user_id}: {len(tasks) if tasks else 0} задач")
    except FlyerAPIError as e:
        logging.error(f"Flyer APIError: {e}")
        return []
    except Exception as e:
        logging.error(f"Flyer unknown: {e}")
        return []

    if not tasks:
        return []

    incomplete = [t for t in tasks if t.get('status') in _incomplete_statuses]
    logging.info(f"Flyer incomplete: {len(incomplete)}")
    return incomplete


async def check_flyer_task(signature: str) -> Optional[str]:
    """Проверяет одно задание по signature. Возвращает status."""
    if not flyer or not signature:
        return None
    try:
        status = await flyer.check_task(signature=signature)
        return status
    except FlyerAPIError as e:
        logging.error(f"Flyer check_task: {e}")
    except Exception as e:
        logging.error(f"Flyer check_task unknown: {e}")
    return None


# ═══════════════════════════════════════════════
# КЛАВИАТУРА ЗАДАНИЙ
# ═══════════════════════════════════════════════

def tasks_keyboard(tasks: List[dict]):
    keyboard = {'inline_keyboard': [[]]}
    for task in tasks:
        for index, link in enumerate(task.get('links', [])):
            button_text = BUTTON_TEXTS.get(task.get('task'), task.get('task', 'Подписаться'))
            if len(keyboard['inline_keyboard'][-1]) == 2:
                keyboard['inline_keyboard'].append([])
            keyboard['inline_keyboard'][-1].append({'text': button_text, 'url': link})

    keyboard['inline_keyboard'].append([{
        'text': '☑️ Проверить подписки',
        'callback_data': 'flyer_check',
    }])
    return keyboard


async def show_tasks(user_id: int, language_code: str, message_id: Optional[int] = None) -> bool:
    """Показывает задания. True — если заданий нет."""
    tasks = await fetch_flyer_tasks(user_id, language_code)

    if not tasks:
        if message_id is None:
            await bot.send_message(
                user_id,
                "🎉 <b>Нет новых заданий!</b>\n\nВозвращайся позже.",
            )
        return True

    _user_tasks[user_id] = tasks

    text = (
        f"🎯 <b>Заданий: {len(tasks)}</b>\n\n"
        f"💵 За каждое: <b>{REWARD:.0f} монет</b>\n"
        f"⏳ Разблокировка: через <b>48ч</b>\n\n"
        f"Подпишись и жми <b>«☑️ Проверить подписки»</b>."
    )

    keyboard = tasks_keyboard(tasks)

    if message_id is None:
        await bot.send_message(chat_id=user_id, text=text, parse_mode='HTML', reply_markup=keyboard)
    else:
        try:
            await bot.edit_message_text(
                chat_id=user_id, message_id=message_id,
                text=text, parse_mode='HTML', reply_markup=keyboard
            )
        except exceptions.MessageNotModified:
            pass
    return False


# ═══════════════════════════════════════════════
# ПРОВЕРКА ПОДПИСОК
# ═══════════════════════════════════════════════

async def check_all_flyer(user_id: int) -> int:
    tasks = _user_tasks.get(user_id, [])
    if not tasks:
        return 0

    done = 0
    for task in tasks:
        sig = task.get('signature')
        if not sig:
            continue
        status = await check_flyer_task(sig)
        logging.info(f"Task {task.get('task')} sig={sig[:12]}... status={status}")
        if status == 'complete':
            task['status'] = 'complete'
            done += 1

    return done


# ═══════════════════════════════════════════════
# МЕНЮ
# ═══════════════════════════════════════════════

def main_menu():
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎯 Задания"), KeyboardButton(text="💰 Баланс")],
            [KeyboardButton(text="📋 Мои подписки")],
        ],
        resize_keyboard=True, is_persistent=True
    )
    return kb


# ═══════════════════════════════════════════════
# ХЕНДЛЕРЫ
# ═══════════════════════════════════════════════

@dp.message(CommandStart())
async def cmd_start(msg: types.Message):
    uid = msg.from_user.id
    lang = msg.from_user.language_code or "ru"
    logging.info(f"=== /start from {uid} ===")

    ok = await show_tasks(uid, lang)
    if ok:
        await msg.answer("🏠 <b>Главное меню</b>", reply_markup=main_menu())


@dp.message(F.text == "🎯 Задания")
async def msg_tasks(msg: types.Message):
    await show_tasks(msg.from_user.id, msg.from_user.language_code or "ru")


@dp.message(F.text == "💰 Баланс")
async def msg_balance(msg: types.Message):
    await msg.answer("💰 <b>Баланс</b>\n\n<i>Заглушка</i>")


@dp.message(F.text == "📋 Мои подписки")
async def msg_completed(msg: types.Message):
    if not flyer:
        await msg.answer("Flyer не настроен"); return
    try:
        completed = await flyer.get_completed_tasks(user_id=msg.from_user.id)
        if not completed:
            await msg.answer("📭 Пока нет выполненных заданий"); return
        text = "📋 <b>Выполненные:</b>\n\n"
        for c in (completed if isinstance(completed, list) else []):
            text += f"• {c}\n"
        await msg.answer(text)
    except Exception as e:
        await msg.answer(f"❌ {e}")


@dp.callback_query(F.data == "flyer_check")
async def cb_flyer_check(cq: types.CallbackQuery):
    uid = cq.from_user.id
    lang = cq.from_user.language_code or "ru"

    logging.info(f"=== flyer_check from {uid} ===")
    await cq.answer("Проверяю…")

    done = await check_all_flyer(uid)
    if done > 0:
        await cq.answer(f"✅ Засчитано: {done}", show_alert=True)

    ok = await show_tasks(uid, lang, cq.message.message_id)
    if ok:
        try:
            await cq.message.delete()
        except Exception:
            pass
        await cq.message.answer(
            "🎉 <b>Все задания выполнены!</b>",
            reply_markup=main_menu()
        )


# ═══════════════════════════════════════════════
# АДМИН
# ═══════════════════════════════════════════════

@dp.message(Command("admin"))
async def cmd_admin(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    await msg.answer(
        f"🔑 <b>Flyer</b>\n\n"
        f"{'✅' if FLYER_API_KEY else '❌'} FLYER_API_KEY\n"
        f"{'✅' if FLYER_AVAILABLE else '❌'} flyerapi\n"
        f"{'✅' if flyer else '❌'} Flyer instance"
    )


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
            await msg.answer("📭 Пустой список. Задач нет."); return
        text = f"📋 <b>Задач: {len(tasks)}</b>\n\n<code>{json.dumps(tasks, ensure_ascii=False, indent=2)[:3000]}</code>"
        await msg.answer(text)
    except Exception as e:
        await msg.answer(f"❌ {e}")


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
