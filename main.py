import os
import asyncio
import logging
from typing import Optional

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F, exceptions
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
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

_incomplete_statuses = ('incomplete', 'abort')

BUTTON_TEXTS = {
    'start bot': '➕ Запустить бота',
    'subscribe channel': '➕ Подписаться',
    'give boost': '➕ Голосовать',
    'follow link': '➕ Перейти',
    'perform action': '➕ Выполнить',
}


# ═══════════════════════════════════════════════
# FLYER: ОП (обязательная подписка)
# ═══════════════════════════════════════════════

async def flyer_op_check_message(user_id: int, language_code: str = "ru", message_id: Optional[int] = None) -> bool:
    """Проверяет ОП. Если не подписан — показывает кнопки с каналами.
    Возвращает True, если доступ разрешён."""
    if not flyer:
        logging.warning("Flyer не настроен")
        return True

    try:
        tasks = await flyer.get_tasks(user_id=user_id, language_code=language_code or "ru", limit=20)
    except FlyerAPIError as e:
        logging.error(f"Flyer get_tasks: {e}")
        return True

    logging.info(f"Flyer tasks for {user_id}: {len(tasks) if tasks else 0}")

    if not tasks:
        return True

    tasks_incomplete = [t for t in tasks if t.get('status') in _incomplete_statuses]
    logging.info(f"Flyer incomplete: {len(tasks_incomplete)}")

    if not tasks_incomplete:
        return True

    # Строим клавиатуру
    keyboard = {'inline_keyboard': [[]]}
    for task in tasks_incomplete:
        for index, link in enumerate(task.get('links', [])):
            button_text = BUTTON_TEXTS.get(task.get('task'), task.get('task', 'Подписаться'))
            if len(keyboard['inline_keyboard'][-1]) == 2:
                keyboard['inline_keyboard'].append([])
            keyboard['inline_keyboard'][-1].append({'text': button_text, 'url': link})

    keyboard['inline_keyboard'].append([{
        'text': '☑️ Проверить',
        'callback_data': 'flyer_check',
    }])

    text = (
        "🔒 <b>Для доступа подпишись на каналы:</b>\n\n"
        "После подписки нажми <b>«☑️ Проверить»</b>."
    )

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


async def flyer_op_check(user_id: int) -> bool:
    """Проверяет, все ли ОП-задачи выполнены."""
    if not flyer:
        return True

    try:
        tasks = await flyer.get_tasks(user_id=user_id)
    except FlyerAPIError as e:
        logging.error(f"Flyer check: {e}")
        return True

    if not tasks:
        return True

    tasks_incomplete = [t for t in tasks if t.get('status') in _incomplete_statuses]
    if not tasks_incomplete:
        return True

    results = await asyncio.gather(*[
        flyer.check_task(user_id=user_id, signature=task['signature'])
        for task in tasks_incomplete
    ], return_exceptions=True)

    for task, status in zip(tasks_incomplete, results):
        if isinstance(status, str):
            task['status'] = status

    return all(t.get('status') not in _incomplete_statuses for t in tasks_incomplete)


# ═══════════════════════════════════════════════
# МЕНЮ
# ═══════════════════════════════════════════════

def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Проверка пройдена", callback_data="done")
    kb.adjust(1)
    return kb.as_markup()


# ═══════════════════════════════════════════════
# ХЕНДЛЕРЫ
# ═══════════════════════════════════════════════

@dp.message(CommandStart())
async def cmd_start(msg: types.Message):
    uid = msg.from_user.id
    lang = msg.from_user.language_code or "ru"

    logging.info(f"=== /start from {uid} (@{msg.from_user.username}) ===")

    if not await flyer_op_check_message(uid, lang):
        return

    await msg.answer(
        "🎉 <b>Доступ открыт!</b>\n\n"
        "Ты подписан на все ресурсы.",
        reply_markup=main_menu()
    )


@dp.callback_query(F.data == "flyer_check")
async def cb_flyer_check(cq: types.CallbackQuery):
    uid = cq.from_user.id
    lang = cq.from_user.language_code or "ru"

    logging.info(f"=== flyer_check from {uid} ===")

    if not await flyer_op_check(uid):
        if not await flyer_op_check_message(uid, lang, cq.message.message_id):
            await cq.answer("⏳ Ещё не всё выполнено")
            return

    try:
        await cq.message.delete()
    except Exception:
        pass

    await cq.answer("✅ Готово!")
    await cq.message.answer(
        "🎉 <b>Доступ открыт!</b>\n\n"
        "Ты подписан на все ресурсы.",
        reply_markup=main_menu()
    )


@dp.callback_query(F.data == "done")
async def cb_done(cq: types.CallbackQuery):
    await cq.answer("Всё работает ✅")


# ═══════════════════════════════════════════════
# АДМИН
# ═══════════════════════════════════════════════

@dp.message(Command("admin"))
async def cmd_admin(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    await msg.answer(
        f"🔑 <b>Flyer ОП</b>\n\n"
        f"{'✅' if FLYER_API_KEY else '❌'} FLYER_API_KEY\n"
        f"{'✅' if FLYER_AVAILABLE else '❌'} flyerapi\n"
        f"{'✅' if flyer else '❌'} Flyer instance"
    )


@dp.message(Command("flyer_debug"))
async def cmd_flyer_debug(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    if not flyer:
        await msg.answer("❌ Flyer не настроен")
        return
    try:
        tasks = await flyer.get_tasks(user_id=msg.from_user.id, language_code="ru", limit=20)
        if not tasks:
            await msg.answer("📭 Flyer вернул ПУСТОЙ список.\n\nЗначит задач нет для тебя.")
            return
        text = f"📋 <b>Задач: {len(tasks)}</b>\n\n"
        for t in tasks:
            text += f"• <b>{t.get('task')}</b> · status=<code>{t.get('status')}</code>\n"
            for l in t.get('links', []):
                text += f"  {l[:80]}\n"
            text += "\n"
        await msg.answer(text)
    except Exception as e:
        await msg.answer(f"❌ Ошибка: <code>{e}</code>")


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
