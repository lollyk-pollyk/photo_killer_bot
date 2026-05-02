import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import data as db
from aiogram import Bot

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


async def auto_end_series(bot: Bot):
    """Автоматическое завершение серии при истечении времени"""
    series = db.get_current_series()
    if not series:
        return

    series_id, series_num, _ = series

    # Получаем дату начала
    with db.get_db() as conn:
        cur = conn.execute("SELECT started_at FROM series WHERE id = ?", (series_id,))
        started = cur.fetchone()[0]

    duration_days = int(db.get_setting("series_duration_days") or 3)
    started_date = datetime.fromisoformat(started)
    end_date = started_date + timedelta(days=duration_days)

    if datetime.now() >= end_date:
        logger.info(f"Автоматическое завершение серии #{series_num}")

        # Завершаем серию
        with db.get_db() as conn:
            # Получаем всех игроков, кто не убил
            cur = conn.execute("""
                SELECT p.user_id, p.warnings, p.name
                FROM players p
                WHERE p.is_alive = 1
            """)
            players = cur.fetchall()

            cur = conn.execute("SELECT DISTINCT killer_id FROM kills WHERE series_id = ?", (series_id,))
            killers = {row[0] for row in cur.fetchall()}

            warnings_given = []
            eliminated = []

            for user_id, current_warnings, name in players:
                if user_id in killers:
                    conn.execute("UPDATE players SET warnings = 0 WHERE user_id = ?", (user_id,))
                else:
                    new_warnings = current_warnings + 1
                    conn.execute("UPDATE players SET warnings = ? WHERE user_id = ?", (new_warnings, user_id))
                    warnings_given.append((user_id, new_warnings, name))

                    if new_warnings >= 2:
                        conn.execute("UPDATE players SET is_alive = 0 WHERE user_id = ?", (user_id,))
                        eliminated.append((user_id, name))

            conn.execute("UPDATE series SET is_active = 0, ended_at = CURRENT_TIMESTAMP WHERE id = ?", (series_id,))
            conn.commit()

            # Отправляем уведомления
            for user_id, warnings, name in warnings_given:
                try:
                    await bot.send_message(user_id,
                                           f"⚠️ СЕРИЯ #{series_num} ЗАВЕРШЕНА ⚠️\n\n"
                                           f"Вы никого не убили.\n"
                                           f"Предупреждение #{warnings}/2\n\n"
                                           f"{'💀 ВЫ ВЫБЫЛИ!' if warnings >= 2 else 'Будьте активнее в следующей серии!'}")
                except:
                    pass

            # Админу отчёт
            admin_id = db.get_setting("admin_id")
            if admin_id:
                report = f"🏁 Серия #{series_num} завершена автоматически\n"
                report += f"Убийств совершено: {len(killers)}\n"
                report += f"Предупреждений выдано: {len(warnings_given)}\n"
                report += f"Выбыло: {len(eliminated)}"
                try:
                    await bot.send_message(int(admin_id), report)
                except:
                    pass


def setup_scheduler(dp, bot):
    """Настройка планировщика"""
    # Сохраняем admin_id в БД
    from config import ADMIN_ID
    db.set_setting("admin_id", str(ADMIN_ID))

    # Проверяем активную серию каждые 6 часов
    scheduler.add_job(auto_end_series, 'interval', hours=6, args=[bot])
    scheduler.start()
    logger.info("Планировщик запущен")