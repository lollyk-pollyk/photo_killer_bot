import asyncio
import random
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardButton, \
    InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import config
import data as db
from scheduler_setup import setup_scheduler, scheduler


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# Состояния FSM
class RegisterState(StatesGroup):
    waiting_name = State()
    waiting_photo = State()


class KillState(StatesGroup):
    waiting_proof = State()
    waiting_target_choice = State()


class AdminApproveState(StatesGroup):
    waiting_approve_registration = State()
    waiting_approve_kill = State()


class AdminSupportState(StatesGroup):
    waiting_message = State()


class AdminReplyState(StatesGroup):
    waiting_reply = State()


class ExitState(StatesGroup):
    waiting_photo = State()

async def check_subscription(user_id: int) -> tuple:
    """Проверяет, подписан ли пользователь на все каналы.
    Возвращает (True/False, список каналов_на_которые_не_подписан)"""
    not_subscribed = []
    
    for channel in config.REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=f"@{channel}", user_id=user_id)
            if member.status not in ["member", "creator", "administrator"]:
                not_subscribed.append(channel)
        except Exception as e:
            print(f"Ошибка проверки канала {channel}: {e}")
            not_subscribed.append(channel)
    
    return len(not_subscribed) == 0, not_subscribed


@dp.message(Command("test_sub"))
async def test_subscription(message: Message):
    """Тест проверки подписки (только для админа)"""
    if message.from_user.id != config.ADMIN_ID:
        await message.answer("Только для админа")
        return
    
    user_id = message.from_user.id
    result = "🔍 РЕЗУЛЬТАТ ПРОВЕРКИ 🔍\n\n"
    
    for channel in config.REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=f"@{channel}", user_id=user_id)
            result += f"📢 @{channel}: {member.status}\n"
        except Exception as e:
            result += f"📢 @{channel}: ОШИБКА - {str(e)}\n"
    
    # Проверяем права бота
    bot_id = (await bot.get_me()).id
    result += f"\n🤖 Бот ID: {bot_id}\n"
    
    for channel in config.REQUIRED_CHANNELS:
        try:
            bot_member = await bot.get_chat_member(chat_id=f"@{channel}", user_id=bot_id)
            result += f"🔧 Бот в @{channel}: {bot_member.status}\n"
        except Exception as e:
            result += f"🔧 Бот в @{channel}: ОШИБКА - {str(e)}\n"
    
    await message.answer(result)

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        await state.clear()
        await message.answer("Действие отменено.")
    else:
        await message.answer(" Нет активного действия для отмены.")


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    
    is_subscribed, not_subscribed = await check_subscription(user_id)
    
    if not is_subscribed:
        channels_list = "\n".join([f"• https://t.me/{ch}" for ch in not_subscribed])
        await message.answer(
            f"❌ ДЛЯ УЧАСТИЯ В ИГРЕ НЕОБХОДИМО ПОДПИСАТЬСЯ НА КАНАЛЫ:\n\n"
            f"{channels_list}\n\n"
            f"✅ После подписки нажмите /start снова",
            disable_web_page_preview=True
        )
        return
        
    with db.get_db() as conn:
        cur = conn.execute("SELECT user_id, name, is_alive FROM players WHERE user_id = ?", (user_id,))
        player = cur.fetchone()

        if player:
            if player[2]:  # is_alive
                await message.answer(f" Вы уже зарегистрированы как {player[1]}!\n\n"
                                     f"Доступные команды:\n"
                                     f"/targets — мои цели\n"
                                     f"/kill — выстрелить фотопулей\n"
                                     f"/stats — моя статистика\n"
                                     f"/series — информация о серии\n"
                                     f"/exit — покинуть игровой мир\n")

            else:
                await message.answer("💀 Вы выбыли из игры. Спасибо за участие!")
            return

    await message.answer("Добро пожаловать в Фотокиллера!\n\n"
                         "Основные правила:\n"
                         "• В каждой серии вы получаете несколько целей\n"
                         "• У вас только одна фотопуля на серию\n"
                         "• Если не убили никого за серию → получаете предупреждение\n"
                         "• 2 предупреждения / попадание в вас фотопулей → выбывание\n\n"
                         "Введите ваше ИМЯ И ФАМИЛИЮ:")
    await state.set_state(RegisterState.waiting_name)
     


@dp.message(RegisterState.waiting_name)
async def process_name(message: Message, state: FSMContext):
    if len(message.text.strip()) > 50:
        await message.answer("Слишком длинное имя. Попробуйте снова (максимум 50 символов):")
        return
    await state.update_data(name=message.text.strip())
    await message.answer(" Теперь отправьте ваше ФОТО:\n"
                         "На фото обязательно должно быть хорошо видно лицо")
    await state.set_state(RegisterState.waiting_photo)


@dp.message(RegisterState.waiting_photo, F.photo | F.document)
async def process_photo(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    name = data["name"]

    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and message.document.mime_type.startswith("image/"):
        file_id = message.document.file_id
    else:
        await message.answer("Пожалуйста, отправьте именно фото (изображение).")
        return

    # Сохраняем в таблицу на модерацию
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO pending_registrations (user_id, name, photo_file_id) VALUES (?, ?, ?)",
            (user_id, name, file_id)
        )
        conn.commit()

    await state.clear()
    await message.answer(f" Ваши данные получены!\n\n"
                         f" Дождитесь проверки администратором.\n"
                         f"После одобрения вы получите уведомление.")

    # Отправляем админу на проверку
    admin_id = config.ADMIN_ID
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_reg_{user_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_reg_{user_id}")
        ]
    ])

    await bot.send_message(
        admin_id,
        f"🔔 НОВАЯ ЗАЯВКА НА РЕГИСТРАЦИЮ\n\n"
        f" Имя: {name}\n"
        f" ID: {user_id}\n"
        f" Фото:",
        reply_markup=keyboard
    )
    await bot.send_photo(admin_id, file_id)


@dp.callback_query(lambda c: c.data.startswith("show_photo_"))
async def show_target_photo(callback: types.CallbackQuery):
    target_id = int(callback.data.split("_")[-1])

    with db.get_db() as conn:
        cur = conn.execute("SELECT name, photo_file_id FROM players WHERE user_id = ?", (target_id,))
        player = cur.fetchone()

        if not player:
            await callback.answer("Цель не найдена", show_alert=True)
            return

        name, photo_file_id = player

        await callback.answer()
        await callback.message.answer_photo(
            photo_file_id,
            caption=f" {name}"
        )


# ========== ИГРОВЫЕ КОМАНДЫ ==========

@dp.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext):
    user_id = message.from_user.id

    # Проверяем, зарегистрирован ли игрок
    with db.get_db() as conn:
        cur = conn.execute("SELECT name FROM players WHERE user_id = ?", (user_id,))
        player = cur.fetchone()

        if not player:
            await message.answer("Вы не зарегистрированы. Используйте /start для регистрации.")
            return

        name = player[0]

    # Сохраняем состояние, что ждём сообщение в поддержку
    await state.update_data(support_user_id=user_id, support_name=name)
    await state.set_state(AdminSupportState.waiting_message)

    await message.answer(
        "НАПИСАТЬ АДМИНИСТРАТОРУ\n\n"
        "Напишите ваше сообщение.\n"
        "Для отмены отправьте /cancel"
    )


@dp.message(AdminSupportState.waiting_message)
async def process_support_message(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    name = data.get("support_name", "Игрок")
    text = message.text

    # Отправляем админу
    admin_id = config.ADMIN_ID

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Ответить", callback_data=f"reply_to_{user_id}"),
            InlineKeyboardButton(text="❌ Закрыть", callback_data="close_support")
        ]
    ])

    await bot.send_message(
        admin_id,
        f" СООБЩЕНИЕ В ПОДДЕРЖКУ\n\n"
        f" Игрок: {name}\n"
        f" ID: {user_id}\n\n"
        f" Сообщение:\n{text}",
        reply_markup=keyboard
    )

    await state.clear()
    await message.answer("Ваше сообщение отправлено администратору.\n")


@dp.callback_query(lambda c: c.data.startswith("reply_to_"))
async def start_reply(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != config.ADMIN_ID:
        await callback.answer("Только для админа", show_alert=True)
        return

    user_id = int(callback.data.split("_")[-1])

    await state.update_data(reply_user_id=user_id)
    await state.set_state(AdminReplyState.waiting_reply)

    await callback.message.edit_text(
        callback.message.text + "\n\n✏️ Напишите ваш ответ:",
        reply_markup=None
    )
    await callback.answer()


@dp.message(AdminReplyState.waiting_reply)
async def process_admin_reply(message: Message, state: FSMContext):
    if message.from_user.id != config.ADMIN_ID:
        await message.answer("Только для админа.")
        return

    data = await state.get_data()
    user_id = data.get("reply_user_id")
    reply_text = message.text

    if not user_id:
        await message.answer(" Ошибка: пользователь не найден.")
        await state.clear()
        return

    try:
        await bot.send_message(
            user_id,
            f"ОТВЕТ ОТ АДМИНИСТРАТОРА\n\n"
            f"{reply_text}\n\n"
            f"️Если хотите написать ещё — используйте /support"
        )
        await message.answer(f"Ответ отправлен игроку (ID: {user_id})")
    except Exception as e:
        await message.answer(f"Ошибка при отправке: {e}")

    await state.clear()


@dp.callback_query(lambda c: c.data == "close_support")
async def close_support(callback: types.CallbackQuery):
    if callback.from_user.id != config.ADMIN_ID:
        await callback.answer("Только для админа", show_alert=True)
        return

    await callback.message.edit_text(
        callback.message.text + "\n\n Закрыто администратором.",
        reply_markup=None
    )
    await callback.answer()


@dp.message(Command("targets"))
async def cmd_targets(message: Message):
    user_id = message.from_user.id

    # СНАЧАЛА проверяем, есть ли активная серия
    series = db.get_current_series()

    if not series:
        await message.answer("❌ Сейчас нет активной серии.\n\n"
                             "Дождитесь, когда администратор начнёт новую серию.\n"
                             "Используйте /series для проверки статуса.")
        return

    series_id, series_num, targets_per = series

    with db.get_db() as conn:
        # Проверяем, жив ли игрок
        cur = conn.execute("SELECT is_alive, name FROM players WHERE user_id = ?", (user_id,))
        player = cur.fetchone()
        if not player or not player[0]:
            await message.answer("💀 Вы выбыли из игры.")
            return

        # Получаем живые цели
        cur = conn.execute("""
            SELECT t.target_id, p.name, p.photo_file_id
            FROM targets t
            JOIN players p ON t.target_id = p.user_id
            LEFT JOIN kills k ON k.victim_id = t.target_id AND k.series_id = t.series_id
            WHERE t.killer_id = ? AND t.series_id = ? AND p.is_alive = 1
            AND t.killed_at IS NULL AND k.id IS NULL
        """, (user_id, series_id))

        targets = cur.fetchall()

        # Проверяем, убил ли уже игрок в этой серии
        cur = conn.execute("SELECT 1 FROM kills WHERE killer_id = ? AND series_id = ?", (user_id, series_id))
        already_killed = cur.fetchone() is not None

        if not targets:
            await message.answer("У вас нет живых целей в текущей серии.\n\n"
                                 f"{'Вы уже использовали свою фотопулю.' if already_killed else '⚠️ Напоминание: если вы никого не убьёте до конца серии, получите предупреждение!'}")
            return

        # Отправляем список целей
        if already_killed:
            await message.answer(f"ВАШИ ЦЕЛИ (серия {series_num}):\n\n"
                                 f" Вы уже использовали свою фотопулю в этой серии.\n"
                                 f"Убить больше никого нельзя.\n\n"
                                 f"Нажмите на кнопку, чтобы увидеть фото цели:")
        else:
            await message.answer(f"ВАШИ ЦЕЛИ (серия {series_num}):\n\n"
                                 f" Вы можете убить только ОДНУ цель из списка.\n\n"
                                 f" Нажмите на кнопку, чтобы увидеть фото цели:")

        for target_id, target_name, photo_file_id in targets:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"📸 Показать фото {target_name}", callback_data=f"show_photo_{target_id}")]
            ])
            await message.answer(f"🎯 {target_name}", reply_markup=keyboard)

        if not already_killed and len(targets) == 1:
            await message.answer(" У вас осталась всего одна цель! Не упустите шанс.")


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    user_id = message.from_user.id

    with db.get_db() as conn:
        cur = conn.execute("SELECT name, kills_total, warnings, is_alive FROM players WHERE user_id = ?", (user_id,))
        player = cur.fetchone()

        if not player:
            await message.answer("Вы не зарегистрированы. Используйте /start")
            return

        name, kills, warnings, is_alive = player

        await message.answer(f" ВАША СТАТИСТИКА \n\n"
                             f"Имя: {name}\n"
                             f"Статус: {'🟢 Жив' if is_alive else '💀 Мёртв'}\n"
                             f" Всего убийств: {kills}\n"
                             f" Текущие предупреждения: {warnings}/2\n"
                             f"(2 предупреждения → выбывание)")


@dp.message(Command("series"))
async def cmd_series(message: Message):
    series = db.get_current_series()

    if not series:
        await message.answer("Нет активной серии.")
        return

    series_id, series_num, targets_per = series

    with db.get_db() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM players WHERE is_alive = 1")
        alive = cur.fetchone()[0]

        cur = conn.execute("SELECT COUNT(DISTINCT killer_id) FROM kills WHERE series_id = ?", (series_id,))
        killers = cur.fetchone()[0]

        duration_days = int(db.get_setting("series_duration_days") or 3)
        cur = conn.execute("SELECT started_at FROM series WHERE id = ?", (series_id,))
        started = cur.fetchone()[0]
        started_date = datetime.fromisoformat(started)
        ends_at = started_date + timedelta(days=duration_days)
        time_left = ends_at - datetime.now()

        if time_left.total_seconds() > 0:
            time_str = f"{time_left.days}д {time_left.seconds // 3600}ч"
        else:
            time_str = "Завершается..."

        await message.answer(f"СЕРИЯ {series_num} 🎲\n\n"
                             f" Старт: {started_date.strftime('%d.%m.%Y %H:%M')}\n"
                             f" До конца: {time_str}\n"
                             f" Целей на игрока: {targets_per}\n"
                             f" Живых игроков: {alive}\n"
                             f" Совершено убийств: {killers}\n\n"
                             f"️ Не убил никого → предупреждение\n"
                             f" 2 предупреждения → выбывание")


@dp.message(Command("kill"))
async def cmd_kill(message: Message, state: FSMContext):
    user_id = message.from_user.id

    # СНАЧАЛА проверяем, есть ли активная серия
    series = db.get_current_series()

    is_subscribed, _ = await check_subscription(user_id)
    if not is_subscribed:
        await message.answer("❌ Вы не подписаны на наши каналы!\n"
                             "Подпишитесь и нажмите /start")
        return

    if not series:
        await message.answer("❌ Сейчас нет активной серии.\n\n"
                             "Дождитесь, когда администратор начнёт новую серию.\n"
                             "Обычно это объявляется в чате.")
        return

    series_id, series_num, _ = series

    with db.get_db() as conn:
        # Проверяем, жив ли игрок
        cur = conn.execute("SELECT is_alive FROM players WHERE user_id = ?", (user_id,))
        player = cur.fetchone()

        if not player or not player[0]:
            await message.answer("💀 Вы выбыли и не можете убивать.")
            return

        # Проверяем, не убил ли уже в этой серии
        cur = conn.execute("SELECT 1 FROM kills WHERE killer_id = ? AND series_id = ?", (user_id, series_id))
        if cur.fetchone():
            await message.answer(" Вы уже использовали свою фотопулю в этой серии!")
            return

        # Получаем живые цели
        cur = conn.execute("""
            SELECT t.target_id, p.name
            FROM targets t
            JOIN players p ON t.target_id = p.user_id
            LEFT JOIN kills k ON k.victim_id = t.target_id AND k.series_id = t.series_id
            WHERE t.killer_id = ? AND t.series_id = ? AND p.is_alive = 1 AND k.id IS NULL
        """, (user_id, series_id))

        targets = cur.fetchall()

        if not targets:
            await message.answer("У вас нет живых целей.\n\n"
                                 "Возможно, кто-то убил их раньше вас.")
            return

        if len(targets) == 1:
            await state.update_data(target_id=targets[0][0], target_name=targets[0][1])
            await message.answer(
                f" Ваша единственная цель: {targets[0][1]}\n\n"
                f"📸 Отправьте ФОТО доказательство убийства:")
            await state.set_state(KillState.waiting_proof)
        else:
            # Сохраняем список целей в состояние
            await state.update_data(targets_list=targets)
            kb = ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text=t[1])] for t in targets],
                resize_keyboard=True,
                one_time_keyboard=True
            )
            await message.answer(" Укажите, кого поразим фотопулей:", reply_markup=kb)
            await state.set_state(KillState.waiting_target_choice)


@dp.message(KillState.waiting_target_choice, F.text)
async def process_target_choice(message: Message, state: FSMContext):
    data = await state.get_data()
    targets = data.get("targets_list", [])

    chosen_name = message.text.strip()
    target = next((t for t in targets if t[1] == chosen_name), None)

    if not target:
        await message.answer("Выберите имя из списка")
        return

    await state.update_data(target_id=target[0], target_name=target[1])
    await message.answer(f" Цель: {target[1]}\n\n📸 Отправьте ФОТО доказательство :",
                         reply_markup=ReplyKeyboardRemove())
    await state.set_state(KillState.waiting_proof)


@dp.message(KillState.waiting_proof, F.photo | F.document)
async def process_kill_proof(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    target_id = data.get("target_id")
    target_name = data.get("target_name")

    if not target_id:
        await message.answer(" Ошибка. Попробуйте /kill заново.")
        await state.clear()
        return

    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and message.document.mime_type.startswith("image/"):
        file_id = message.document.file_id
    else:
        await message.answer("Пожалуйста, отправьте именно фото.")
        return

    series = db.get_current_series()
    if not series:
        await message.answer(" Серия уже завершена.")
        await state.clear()
        return

    series_id, series_num, _ = series

    with db.get_db() as conn:
        # Проверяем, что цель ещё жива
        cur = conn.execute("SELECT is_alive, name FROM players WHERE user_id = ?", (target_id,))
        victim = cur.fetchone()

        if not victim or not victim[0]:
            await message.answer(f" {target_name} уже поражен фотопулей! Кто-то опередил вас.")
            await state.clear()
            return

        # Проверяем, что убийца ещё не убивал в этой серии
        cur = conn.execute("SELECT 1 FROM kills WHERE killer_id = ? AND series_id = ?", (user_id, series_id))
        if cur.fetchone():
            await message.answer(" Вы уже использовали свою фотопулю в этой серии!")
            await state.clear()
            return

        # Сохраняем в pending_kills
        conn.execute("""
            INSERT INTO pending_kills (series_id, killer_id, target_id, photo_proof, target_name)
            VALUES (?, ?, ?, ?, ?)
        """, (series_id, user_id, target_id, file_id, target_name))
        conn.commit()

    await state.clear()
    await message.answer(f" Фото отправлено на проверку!\n\n"
                         f"Дождитесь подтверждения.\n"
                         f"После одобрения убийство будет засчитано.")

    # Отправляем админу на проверку
    admin_id = config.ADMIN_ID
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve_kill_{user_id}_{target_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_kill_{user_id}_{target_id}")
        ]
    ])

    await bot.send_message(
        admin_id,
        f"🔔 НОВОЕ УБИЙСТВО НА ПРОВЕРКУ\n\n"
        f"🔫 Киллер: {message.from_user.first_name} (ID: {user_id})\n"
        f"🎯 Жертва: {target_name} (ID: {target_id})\n"
        f"📸 Фото доказательство:",
        reply_markup=keyboard
    )
    await bot.send_photo(admin_id, file_id)


@dp.message(Command("exit"))
async def cmd_exit(message: Message, state: FSMContext):
    user_id = message.from_user.id

    # Проверяем, зарегистрирован ли игрок
    with db.get_db() as conn:
        cur = conn.execute("SELECT name, is_alive FROM players WHERE user_id = ?", (user_id,))
        player = cur.fetchone()

        if not player:
            await message.answer("❌ Вы не зарегистрированы. Используйте /start")
            return

        name, is_alive = player

        if not is_alive:
            await message.answer("💀 Вы уже выбыли из игры.")
            return

    await state.update_data(exit_user_id=user_id, exit_name=name)
    await state.set_state(ExitState.waiting_photo)

    # Создаём клавиатуру с кнопками
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💔 Выйти без фото", callback_data=f"exit_without_photo_{user_id}"),
            InlineKeyboardButton(text="❌ Остаться в игре", callback_data="cancel_exit")
        ]
    ])

    await message.answer(
        f"💔 ВЫ ХОТИТЕ ПОКИНУТЬ ИГРУ?\n\n"
        f"Вы уверены? Это действие необратимо.\n\n"
        f"📸 Это необязательно, но админ будет очень благодарен, "
        f"если вы порадуете его прощальной фотографией.\n\n"
        f"• Если хотите отправить фото — просто загрузите его\n"
        f"• Если не хотите — нажмите кнопку для выхода ниже",
        reply_markup=keyboard
    )


@dp.message(ExitState.waiting_photo, F.photo | F.document)
async def process_exit_photo(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    name = data.get("exit_name")

    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and message.document.mime_type.startswith("image/"):
        file_id = message.document.file_id
    else:
        await message.answer("Пожалуйста, отправьте фото или нажмите кнопку 'Выйти без фото'")
        return

    # Отправляем прощальное фото админу
    admin_id = config.ADMIN_ID

    await bot.send_photo(
        admin_id,
        file_id,
        caption=f"💔 ПРОЩАЛЬНОЕ ФОТО\n\n"
                f"👤 Игрок {name} покинул игру и прислал прощальное фото!\n"
                f"🆔 ID: {user_id}"
    )

    # Выбывание игрока
    with db.get_db() as conn:
        conn.execute("UPDATE players SET is_alive = 0 WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM targets WHERE killer_id = ?", (user_id,))
        conn.execute("UPDATE targets SET killed_at = CURRENT_TIMESTAMP WHERE target_id = ? AND killed_at IS NULL",
                     (user_id,))
        conn.commit()

    await state.clear()
    await message.answer(
        f"💔 ВЫ ПОКИНУЛИ ИГРУ\n\n"
        f"Спасибо за участие и прощальное фото!\n"
        f"Будем рады видеть вас снова в следующих играх."
    )


@dp.callback_query(lambda c: c.data.startswith("exit_without_photo_"))
async def exit_without_photo(callback: types.CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[-1])

    if callback.from_user.id != user_id:
        await callback.answer("❌ Это не ваша команда", show_alert=True)
        return

    with db.get_db() as conn:
        cur = conn.execute("SELECT name FROM players WHERE user_id = ?", (user_id,))
        player = cur.fetchone()

        if not player:
            await callback.answer("Игрок не найден", show_alert=True)
            return

        name = player[0]

        # Отправляем уведомление админу
        admin_id = config.ADMIN_ID
        await bot.send_message(
            admin_id,
            f"💔 ИГРОК ПОКИНУЛ ИГРУ\n\n"
            f"👤 {name} (ID: {user_id})\n"
            f"❌ Прощальное фото не отправил."
        )

        # Выбывание игрока
        conn.execute("UPDATE players SET is_alive = 0 WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM targets WHERE killer_id = ?", (user_id,))
        conn.execute("UPDATE targets SET killed_at = CURRENT_TIMESTAMP WHERE target_id = ? AND killed_at IS NULL",
                     (user_id,))
        conn.commit()

    await state.clear()
    await callback.message.edit_text(
        f"💔 ВЫ ПОКИНУЛИ ИГРУ\n\n"
        f"Спасибо за участие!\n"
        f"Жаль, что вы не захотели поделиться прощальным фото...\n"
        f"Будем рады видеть вас снова."
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "cancel_exit")
async def cancel_exit(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id

    current_state = await state.get_state()
    if current_state is None:
        await callback.answer("Нет активного действия", show_alert=True)
        return

    await state.clear()
    await callback.message.edit_text(
        f"✅ Вы остаётесь в игре!\n\n"
        f"Продолжайте охоту! /targets — ваши цели"
    )
    await callback.answer()


# ========== АДМИН-КОМАНДЫ ==========
def is_admin(message: Message) -> bool:
    return message.from_user.id == config.ADMIN_ID


@dp.message(Command("set_targets"))
async def cmd_set_targets(message: Message):
    if not is_admin(message):
        await message.answer(" Только для админа.")
        return

    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer(" Используйте: /set_targets <число от 1 до 5>")
        return

    count = int(parts[1])
    if count < 1 or count > 5:
        await message.answer(" Число должно быть от 1 до 5.")
        return

    db.set_setting("targets_per_series", count)
    await message.answer(f" Количество целей на игрока установлено: {count}\n(Применится к следующей серии)")


@dp.message(Command("set_duration"))
async def cmd_set_duration(message: Message):
    if not is_admin(message):
        await message.answer(" Только для админа.")
        return

    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer(" Используйте: /set_duration <дни>")
        return

    days = int(parts[1])
    if days < 1 or days > 30:
        await message.answer(" Дней должно быть от 1 до 30.")
        return

    db.set_setting("series_duration_days", days)
    await message.answer(f" Длительность серии установлена: {days} дн.\n(Для активной серии не изменится)")


@dp.message(Command("start_series"))
async def cmd_start_series(message: Message):
    if not is_admin(message):
        await message.answer("⛔ Только для админа.")
        return

    targets_per = int(db.get_setting("targets_per_series") or 3)
    series_id, series_num, targets_given = db.start_new_series(targets_per)

    # Выдаём цели всем живым игрокам
    with db.get_db() as conn:
        cur = conn.execute("SELECT user_id FROM players WHERE is_alive = 1")
        alive_players = [row[0] for row in cur.fetchall()]

        if len(alive_players) < 2:
            await message.answer("❌ Слишком мало живых игроков для новой серии (нужно хотя бы 2).")
            return

        # Выдаём цели
        for killer in alive_players:
            # Исключаем себя
            available = [p for p in alive_players if p != killer]
            if len(available) < targets_given:
                available = available * (targets_given // len(available) + 1)

            targets = random.sample(available, min(targets_given, len(available)))

            for target in targets:
                conn.execute("""
                    INSERT INTO targets (series_id, killer_id, target_id)
                    VALUES (?, ?, ?)
                """, (series_id, killer, target))

            # Собираем информацию о целях (имя, фото, ID)
            targets_info = []
            for t in targets:
                cur2 = conn.execute("SELECT name, photo_file_id FROM players WHERE user_id = ?", (t,))
                name, photo = cur2.fetchone()
                targets_info.append((name, photo, t))

            target_names = [name for name, _, _ in targets_info]

            # ========== ЛОГИКА ОТПРАВКИ ==========
            if len(targets_info) == 1:
                # ОДНА ЦЕЛЬ: отправляем фото сразу (без кнопки)
                name, photo_file_id, target_id = targets_info[0]

                await bot.send_message(
                    killer,
                    f" НОВАЯ СЕРИЯ #{series_num}!\n\n"
                    f"Ваша цель: {name}\n\n"
                    f" У вас есть одна фотопуля на серию.\n"
                    f" Не убьёте никого → получите предупреждение!"
                )
                await bot.send_photo(
                    killer,
                    photo_file_id,
                    caption=f"🎯 {name}\n\n📸 Это ваша цель. Используйте /kill чтобы воспользоваться фотопулей."
                )
            else:
                # НЕСКОЛЬКО ЦЕЛЕЙ: отправляем список и кнопки для просмотра фото
                await bot.send_message(
                    killer,
                    f"🎯 НОВАЯ СЕРИЯ #{series_num}!\n\n"
                    f"Ваши цели:\n" + "\n".join([f"• {name}" for name in target_names]) + "\n\n"
                                                                                          f" У вас есть одна фотопуля на эту серию.\n"
                                                                                          f" Не убьёте никого → получите предупреждение!\n\n"
                                                                                          f" Нажмите, чтобы увидеть фото цели:"
                )

                for name, photo_file_id, target_id in targets_info:
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=f"📸 Показать фото {name}", callback_data=f"show_photo_{target_id}")]
                    ])
                    await bot.send_message(
                        killer,
                        f"🎯 {name}",
                        reply_markup=keyboard
                    )

        conn.commit()

    await message.answer(f" СЕРИЯ #{series_num} НАЧАТА!\n\n"
                         f" Целей на игрока: {targets_given}\n"
                         f" Живых игроков: {len(alive_players)}\n"
                         f" Длительность: {db.get_setting('series_duration_days')} дн.\n\n"
                         f"Участникам разосланы цели.")


@dp.message(Command("remove_player"))
async def cmd_remove_player(message: Message):
    if not is_admin(message):
        await message.answer(" Только для админа.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(" Используйте: /remove_player <ID или имя>\n\n"
                             "Примеры:\n"
                             "/remove_player 123456789\n"
                             "/remove_player Иван Иванов")
        return

    search = parts[1].strip()

    with db.get_db() as conn:
        # Пробуем найти по ID или по имени
        if search.isdigit():
            cur = conn.execute("SELECT user_id, name, is_alive FROM players WHERE user_id = ?", (int(search),))
        else:
            cur = conn.execute("SELECT user_id, name, is_alive FROM players WHERE name LIKE ?", (f"%{search}%",))

        player = cur.fetchone()

        if not player:
            await message.answer(f" Игрок '{search}' не найден.")
            return

        user_id, name, is_alive = player

        # Удаляем
        conn.execute("UPDATE players SET is_alive = 0 WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM targets WHERE target_id = ?", (user_id,))
        conn.execute("DELETE FROM kills WHERE killer_id = ? OR victim_id = ?", (user_id, user_id))
        conn.execute("DELETE FROM pending_registrations WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM pending_kills WHERE killer_id = ? OR target_id = ?", (user_id, user_id))
        conn.commit()

    await message.answer(f"✅ ИГРОК УДАЛЁН\n\n"
                         f"👤 {name}\n"
                         f"🆔 {user_id}\n\n"
                         f"Игрок выбыл из игры.")

    # Уведомляем удалённого игрока
    try:
        await bot.send_message(
            user_id,
            f" АДМИН ВАС УДАЛИЛ ВАС ИЗ ИГРЫ  \n\n"
            f"Если считаете это ошибкой — свяжитесь с организатором."
        )
    except:
        pass


@dp.message(Command("end_series"))
async def cmd_end_series(message: Message):
    if not is_admin(message):
        await message.answer(" Только для создателя.")
        return

    result = db.end_current_series()

    if not result:
        await message.answer(" Нет активной серии.")
        return

    # Рассылаем результаты
    with db.get_db() as conn:
        # Информация для админа
        admin_msg = f" СЕРИЯ #{result['series_num']} ЗАВЕРШЕНА 🏁\n\n"
        admin_msg += f" Совершено убийств: {result['killers_count']}\n"
        admin_msg += f"⚠ Предупреждения получили: {len(result['warnings_given'])} игроков\n"
        admin_msg += f"💀 Выбыли: {len(result['eliminated'])} игроков\n\n"

        if result['eliminated']:
            eliminated_names = []
            for uid in result['eliminated']:
                cur = conn.execute("SELECT name FROM players WHERE user_id = ?", (uid,))
                name = cur.fetchone()
                if name:
                    eliminated_names.append(name[0])
            admin_msg += f"Выбывшие: {', '.join(eliminated_names)}"

        await message.answer(admin_msg)

        # Оповещаем игроков с предупреждениями
        for uid, warnings in result['warnings_given']:
            cur = conn.execute("SELECT name FROM players WHERE user_id = ?", (uid,))
            name = cur.fetchone()
            if name:
                try:
                    await bot.send_message(uid,
                                           f"️ СЕРИЯ #{result['series_num']} ЗАВЕРШЕНА ️\n\n"
                                           f"Вы никого не убили.\n"
                                           f"Предупреждение #{warnings}/2\n\n"
                                           f"{'💀 ВЫ ВЫБЫЛИ из игры!' if warnings >= 2 else 'В следующей серии постарайтесь быть активнее!'}")
                except:
                    pass


@dp.message(Command("next_series"))
async def cmd_next_series(message: Message):
    if not is_admin(message):
        await message.answer("Только для админа.")
        return

    await cmd_end_series(message)
    await cmd_start_series(message)


@dp.message(Command("status"))
async def cmd_status(message: Message):
    if not is_admin(message):
        await message.answer("Только для админа.")
        return

    series = db.get_current_series()

    with db.get_db() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM players WHERE is_alive = 1")
        alive = cur.fetchone()[0]

        cur = conn.execute("SELECT COUNT(*) FROM players")
        total = cur.fetchone()[0]

        status_msg = f" СТАТУС ИГРЫ \n\n"
        status_msg += f" Всего игроков: {total}\n"
        status_msg += f"🟢 Живых: {alive}\n"
        status_msg += f"💀 Мёртвых: {total - alive}\n\n"

        if series:
            series_id, series_num, targets_per = series
            cur = conn.execute("SELECT COUNT(DISTINCT killer_id) FROM kills WHERE series_id = ?", (series_id,))
            killers = cur.fetchone()[0]
            status_msg += f" Активная серия: #{series_num}\n"
            status_msg += f" Целей на игрока: {targets_per}\n"
            status_msg += f" Убийств в серии: {killers}\n"
        else:
            status_msg += f" Активная серия: нет\n"

        await message.answer(status_msg)


@dp.message(Command("players"))
async def cmd_players(message: Message):
    if not is_admin(message):
        await message.answer(" Только для админа.")
        return

    with db.get_db() as conn:
        cur = conn.execute(
            "SELECT name, user_id, is_alive, kills_total, warnings FROM players ORDER BY is_alive DESC, kills_total DESC")
        players = cur.fetchall()

        if not players:
            await message.answer("Нет игроков.")
            return

        msg = "👥 СПИСОК ИГРОКОВ 👥\n\n"
        for name, uid, alive, kills, warnings in players:
            status = "🟢" if alive else "💀"
            msg += f"{status} {name} — убийств: {kills}, предупреждений: {warnings}\n"

        await message.answer(msg)


from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


@dp.callback_query(lambda c: c.data.startswith("approve_reg_"))
async def approve_registration(callback: types.CallbackQuery):
    if callback.from_user.id != config.ADMIN_ID:
        await callback.answer("Только для админа", show_alert=True)
        return

    user_id = int(callback.data.split("_")[-1])

    with db.get_db() as conn:
        # Берём данные из pending
        cur = conn.execute("SELECT name, photo_file_id FROM pending_registrations WHERE user_id = ?", (user_id,))
        reg = cur.fetchone()

        if not reg:
            await callback.message.edit_text(" Заявка уже обработана или не найдена.")
            return

        name, photo_file_id = reg

        # Переносим в players
        conn.execute(
            "INSERT INTO players (user_id, name, photo_file_id, is_alive, warnings, kills_total) VALUES (?, ?, ?, 1, 0, 0)",
            (user_id, name, photo_file_id)
        )
        # Удаляем из pending
        conn.execute("DELETE FROM pending_registrations WHERE user_id = ?", (user_id,))
        conn.commit()

    await callback.message.edit_text(f" Регистрация {name} ОДОБРЕНА!")

    # Уведомляем пользователя
    await bot.send_message(
        user_id,
        f"ВАША РЕГИСТРАЦИЯ ОДОБРЕНА!\n\n"
        f"Добро пожаловать в игру!\n"
        f"Дождитесь начала серии."
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("reject_reg_"))
async def reject_registration(callback: types.CallbackQuery):
    if callback.from_user.id != config.ADMIN_ID:
        await callback.answer("Только для админа", show_alert=True)
        return

    user_id = int(callback.data.split("_")[-1])

    with db.get_db() as conn:
        cur = conn.execute("SELECT name FROM pending_registrations WHERE user_id = ?", (user_id,))
        reg = cur.fetchone()
        name = reg[0] if reg else "пользователь"

        conn.execute("DELETE FROM pending_registrations WHERE user_id = ?", (user_id,))
        conn.commit()

    await callback.message.edit_text(f" Регистрация {name} ОТКЛОНЕНА.")

    await bot.send_message(
        user_id,
        f"К сожалению, ваша регистрация была отклонена администратором.\n"
        f"Вы можете попробовать снова через /start"
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("approve_kill_"))
async def approve_kill(callback: types.CallbackQuery):
    if callback.from_user.id != config.ADMIN_ID:
        await callback.answer("Только для админа", show_alert=True)
        return

    _, _, killer_id, target_id = callback.data.split("_")
    killer_id = int(killer_id)
    target_id = int(target_id)

    with db.get_db() as conn:
        # Берём данные из pending_kills
        cur = conn.execute("""
            SELECT id, series_id, photo_proof, target_name 
            FROM pending_kills 
            WHERE killer_id = ? AND target_id = ?
            ORDER BY submitted_at DESC LIMIT 1
        """, (killer_id, target_id))
        pending = cur.fetchone()

        if not pending:
            await callback.message.edit_text(" Заявка уже обработана.")
            return

        pending_id, series_id, photo_proof, target_name = pending

        # Регистрируем убийство
        conn.execute("""
            INSERT INTO kills (series_id, killer_id, victim_id, photo_proof)
            VALUES (?, ?, ?, ?)
        """, (series_id, killer_id, target_id, photo_proof))

        # Увеличиваем счёт убийств
        conn.execute("UPDATE players SET kills_total = kills_total + 1 WHERE user_id = ?", (killer_id,))

        # Убиваем жертву
        conn.execute("UPDATE players SET is_alive = 0 WHERE user_id = ?", (target_id,))

        # Отмечаем цель как убитую
        conn.execute("""
            UPDATE targets SET killed_at = CURRENT_TIMESTAMP, photo_proof = ?
            WHERE killer_id = ? AND target_id = ? AND series_id = ? AND killed_at IS NULL
        """, (photo_proof, killer_id, target_id, series_id))

        # Удаляем из pending
        conn.execute("DELETE FROM pending_kills WHERE id = ?", (pending_id,))
        conn.commit()

    await callback.message.edit_text(f" Убийство {target_name} ПОДТВЕРЖДЕНО!")

    # Получаем имя киллера
    with db.get_db() as conn:
        cur = conn.execute("SELECT name FROM players WHERE user_id = ?", (killer_id,))
        killer_name = cur.fetchone()[0]

    # Уведомляем убийцу
    await bot.send_message(
        killer_id,
        f" ВАШЕ УБИЙСТВО ПОДТВЕРЖДЕНО!\n\n"
        f"💀 Жертва: {target_name}\n"
        f"Остальные цели в этой серии больше неактивны."
    )

    # Уведомляем жертву с фото от киллера
    try:
        await bot.send_photo(
            target_id,
            photo_proof,
            caption=f" ВАС УБИЛИ!\n\n"
                    f" Киллер: {killer_name}\n"
                    f" Это фото стало доказательством вашего убийства.\n\n"
                    f"Вы выбываете из игры. Спасибо за участие!"
        )
    except:
        # Если не отправилось фото, отправляем текст
        await bot.send_message(
            target_id,
            f"💀 ВАС УБИЛИ!\n\n"
            f" Киллер: {killer_name}\n"
            f"Вы выбываете из игры. Спасибо за участие!"
        )

    # Уведомляем других, у кого эта цель была в списке
    with db.get_db() as conn:
        cur = conn.execute("""
            SELECT DISTINCT t.killer_id, p.name
            FROM targets t
            JOIN players p ON t.killer_id = p.user_id
            WHERE t.target_id = ? AND t.series_id = ? AND t.killer_id != ?
        """, (target_id, series_id, killer_id))

        for other_killer, other_name in cur.fetchall():
            try:
                await bot.send_message(
                    other_killer,
                    f" Ваша цель {target_name} была убита игроком {killer_name}!\n"
                    f"Она выбывает из вашего списка."
                )
            except:
                pass

    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("reject_kill_"))
async def reject_kill(callback: types.CallbackQuery):
    if callback.from_user.id != config.ADMIN_ID:
        await callback.answer("Только для админа", show_alert=True)
        return

    _, _, killer_id, target_id = callback.data.split("_")
    killer_id = int(killer_id)
    target_id = int(target_id)

    with db.get_db() as conn:
        cur = conn.execute("""
            SELECT target_name, photo_proof FROM pending_kills 
            WHERE killer_id = ? AND target_id = ?
            ORDER BY submitted_at DESC LIMIT 1
        """, (killer_id, target_id))
        pending = cur.fetchone()

        if pending:
            target_name = pending[0]
            photo_proof = pending[1]
            conn.execute("DELETE FROM pending_kills WHERE killer_id = ? AND target_id = ?", (killer_id, target_id))
            conn.commit()
        else:
            target_name = "неизвестный"
            photo_proof = None

    await callback.message.edit_text(f"❌ Убийство {target_name} ОТКЛОНЕНО.")

    await bot.send_message(
        killer_id,
        f"❌ Ваше убийство НЕ ПОДТВЕРЖДЕНО администратором.\n"
        f"Попробуйте загрузить другое фото через /kill"
    )

    await callback.answer()


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if not is_admin(message):
        await message.answer("⛔ Только для админа.")
        return

    text = message.text.replace("/broadcast", "").strip()
    if not text:
        await message.answer("❌ Используйте: /broadcast <текст сообщения>\n\n"
                             "Пример: /broadcast Внимание! Завтра финал игры!")
        return

    with db.get_db() as conn:
        cur = conn.execute("SELECT user_id, name FROM players WHERE is_alive = 1")
        players = cur.fetchall()

        if not players:
            await message.answer("❌ Нет активных игроков для рассылки.")
            return

        sent = 0
        failed = 0

        for user_id, name in players:
            try:
                await bot.send_message(
                    user_id,
                    f" МАССОВАЯ РАССЫЛКА ОТ АДМИНА \n\n{text}"
                )
                sent += 1
            except:
                failed += 1

        await message.answer(f"✅ Рассылка завершена!\n\n"
                             f"Отправлено: {sent}\n"
                             f"Не доставлено: {failed}")


@dp.message(Command("broadcast_all"))
async def cmd_broadcast_all(message: Message):
    """Рассылка всем игрокам (включая мёртвых)"""
    if not is_admin(message):
        await message.answer("⛔ Только для админа.")
        return

    text = message.text.replace("/broadcast_all", "").strip()
    if not text:
        await message.answer("❌ Используйте: /broadcast_all <текст сообщения>")
        return

    with db.get_db() as conn:
        cur = conn.execute("SELECT user_id, name FROM players")
        players = cur.fetchall()

        if not players:
            await message.answer("❌ Нет игроков.")
            return

        sent = 0
        failed = 0

        for user_id, name in players:
            try:
                await bot.send_message(
                    user_id,
                    f" ВАЖНОЕ ОБЪЯВЛЕНИЕ ОТ АДМИНА \n\n{text}"
                )
                sent += 1
            except:
                failed += 1

        await message.answer(f"✅ Рассылка завершена!\n\n"
                             f"Всего: {len(players)}\n"
                             f"Отправлено: {sent}\n"
                             f"Не доставлено: {failed}")


@dp.message(Command("msg"))
async def cmd_send_message(message: Message):
    if not is_admin(message):
        await message.answer("⛔ Только для админа.")
        return

    # Разделяем: первый параметр (кто), остальное (текст сообщения)
    parts = message.text.split(maxsplit=2)

    if len(parts) < 3:
        await message.answer("❌ Используйте:\n"
                             "/msg <ID или имя> <текст сообщения>\n\n"
                             "Примеры:\n"
                             "/msg 123456789 Привет!\n"
                             "/msg Анна Смирнова Привет, это сообщение для тебя!")
        return

    search = parts[1]  # ID или имя
    text = parts[2]  # текст сообщения

    with db.get_db() as conn:
        # Поиск по ID (если ввели число)
        if search.isdigit():
            cur = conn.execute("SELECT user_id, name FROM players WHERE user_id = ?", (int(search),))
        else:
            # Поиск по имени (с пробелами)
            cur = conn.execute("SELECT user_id, name FROM players WHERE name = ?", (search,))

        player = cur.fetchone()

        # Если не нашли по полному совпадению, пробуем поиск по части имени
        if not player and not search.isdigit():
            cur = conn.execute("SELECT user_id, name FROM players WHERE name LIKE ?", (f"%{search}%",))
            player = cur.fetchone()

        if not player:
            await message.answer(f"❌ Игрок '{search}' не найден.")
            return

        user_id, name = player

        # Отправляем сообщение
        try:
            await bot.send_message(
                user_id,
                f"📢 СООБЩЕНИЕ ОТ АДМИНИСТРАТОРА 📢\n\n{text}"
            )
            await message.answer(f"✅ Сообщение отправлено игроку {name} (ID: {user_id})")
        except Exception as e:
            await message.answer(f"❌ Ошибка при отправке: {e}")


@dp.message(Command("restore_player"))
async def cmd_restore_player(message: Message):
    if not is_admin(message):
        await message.answer("⛔ Только для админа.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("❌ Используйте: /restore_player <ID или имя>\n\n"
                             "Примеры:\n"
                             "/restore_player 123456789\n"
                             "/restore_player Анна Смирнова")
        return

    search = parts[1].strip()

    with db.get_db() as conn:
        # Пробуем найти игрока (даже мёртвого)
        if search.isdigit():
            cur = conn.execute("SELECT user_id, name, is_alive FROM players WHERE user_id = ?", (int(search),))
        else:
            cur = conn.execute("SELECT user_id, name, is_alive FROM players WHERE name LIKE ?", (f"%{search}%",))

        player = cur.fetchone()

        if not player:
            await message.answer(f" Игрок '{search}' не найден.")
            return

        user_id, name, is_alive = player

        if is_alive == 1:
            await message.answer(f" Игрок {name} уже жив и в игре.\n\n"
                                 f"Если хотите сбросить ему предупреждения — используйте:\n"
                                 f"/reset_warnings {user_id}")
            return

        # Восстанавливаем игрока
        conn.execute("UPDATE players SET is_alive = 1, warnings = 0 WHERE user_id = ?", (user_id,))
        conn.commit()

    await message.answer(f"✅ ИГРОК ВОССТАНОВЛЕН\n\n"
                         f"👤 {name}\n"
                         f"🆔 {user_id}\n\n"
                         f"Игрок снова в игре! Предупреждения сброшены.")

    # Уведомляем восстановленного игрока
    try:
        await bot.send_message(
            user_id,
            f" ВАС ВОССТАНОВИЛИ В ИГРЕ \n\n"
            f"Вы снова можете участвовать.\n"
            f"Дождитесь начала следующей серии."
        )
    except:
        pass


@dp.message(Command("reset_warnings"))
async def cmd_reset_warnings(message: Message):
    """Сбросить предупреждения игроку (без восстановления, если жив)"""
    if not is_admin(message):
        await message.answer("⛔ Только для админа.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("❌ Используйте: /reset_warnings <ID или имя>\n\n"
                             "Пример:\n"
                             "/reset_warnings 123456789")
        return

    search = parts[1].strip()

    with db.get_db() as conn:
        if search.isdigit():
            cur = conn.execute("SELECT user_id, name, warnings FROM players WHERE user_id = ?", (int(search),))
        else:
            cur = conn.execute("SELECT user_id, name, warnings FROM players WHERE name LIKE ?", (f"%{search}%",))

        player = cur.fetchone()

        if not player:
            await message.answer(f"❌ Игрок '{search}' не найден.")
            return

        user_id, name, warnings = player

        conn.execute("UPDATE players SET warnings = 0 WHERE user_id = ?", (user_id,))
        conn.commit()

    await message.answer(f"✅ Предупреждения сброшены\n\n"
                         f"👤 {name}\n"
                         f"Было предупреждений: {warnings}\n"
                         f"Стало: 0")

    try:
        await bot.send_message(
            user_id,
            f"✅ Администратор сбросил ваши предупреждения!\n"
            f"Теперь у вас 0/2."
        )
    except:
        pass


@dp.message(Command("dead_players"))
async def cmd_dead_players(message: Message):
    """Показать всех выбывших игроков (для восстановления)"""
    if not is_admin(message):
        await message.answer("⛔ Только для админа.")
        return

    with db.get_db() as conn:
        cur = conn.execute(
            "SELECT user_id, name, kills_total, warnings FROM players WHERE is_alive = 0 ORDER BY kills_total DESC"
        )
        players = cur.fetchall()

        if not players:
            await message.answer("📭 Нет выбывших игроков.")
            return

        msg = "💀 ВЫБЫВШИЕ ИГРОКИ 💀\n\n"
        for user_id, name, kills, warnings in players:
            msg += f"• {name} (ID: {user_id}) — убийств: {kills}, предупреждений: {warnings}\n"

        msg += "\nЧтобы восстановить: /restore_player ID"

        await message.answer(msg)


@dp.message()
async def handle_any_message(message: Message):
    """Обработка любых других сообщений (не команд)"""
    user_id = message.from_user.id

    # Игнорируем команды (они уже обработаны выше)
    if message.text and message.text.startswith('/'):
        return

    # Проверяем, есть ли игрок в БД
    with db.get_db() as conn:
        cur = conn.execute("SELECT name FROM players WHERE user_id = ?", (user_id,))
        player = cur.fetchone()

    if player:
        await message.answer(
            " Чем могу помочь?\n\n"
            "📋 Доступные команды:\n"
            "/targets — мои цели\n"
            "/kill — загрузить фото убийства\n"
            "/stats — моя статистика\n"
            "/series — информация о серии\n"
            "/support — написать администратору\n"
            "/exit — покинуть игру\n\n"
            "Если хотите написать админу, используйте /support"
        )
    else:
        await message.answer(
            " Вы не зарегистрированы!\n\n"
            " Для участия в игре Фотокиллер:\n"
            "1. Подпишитесь на наши каналы\n"
            "2. Используйте /start для регистрации\n\n"
            "По вопросам: /support"
        )


# ========== ЗАПУСК ==========
async def main():
    db.init_db()
    logger.info("База данных инициализирована")

    # Настраиваем планировщик
    setup_scheduler(dp, bot)

    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
