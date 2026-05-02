import sqlite3

DB_NAME = "photo_killer.db"


DEFAULT_TARGETS_PER_PLAYER = 3
DEFAULT_SERIES_DURATION_DAYS = 3


def get_db():
    return sqlite3.connect(DB_NAME)


def init_db():
    with get_db() as conn:
        # Таблица игроков
        conn.execute("""
            CREATE TABLE IF NOT EXISTS players (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                photo_file_id TEXT NOT NULL,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_alive BOOLEAN DEFAULT 1,
                warnings INTEGER DEFAULT 0,
                kills_total INTEGER DEFAULT 0
            )
        """)

        # Таблица серий
        conn.execute("""
            CREATE TABLE IF NOT EXISTS series (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number INTEGER NOT NULL,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP,
                targets_per_player INTEGER DEFAULT 3,
                is_active BOOLEAN DEFAULT 1
            )
        """)

        # Таблица целей
        conn.execute("""
            CREATE TABLE IF NOT EXISTS targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER NOT NULL,
                killer_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                killed_at TIMESTAMP,
                photo_proof TEXT
            )
        """)

        # Таблица убийств
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER NOT NULL,
                killer_id INTEGER NOT NULL,
                victim_id INTEGER NOT NULL,
                photo_proof TEXT NOT NULL,
                killed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Настройки админа
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Установка значений по умолчанию
        conn.execute("INSERT OR IGNORE INTO admin_settings (key, value) VALUES ('targets_per_series', ?)",
                     (str(DEFAULT_TARGETS_PER_PLAYER),))
        conn.execute("INSERT OR IGNORE INTO admin_settings (key, value) VALUES ('series_duration_days', ?)",
                     (str(DEFAULT_SERIES_DURATION_DAYS),))

        conn.commit()

        # Таблица на модерацию регистраций
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_registrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                photo_file_id TEXT NOT NULL,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Таблица на модерацию убийств
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_kills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER NOT NULL,
                killer_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                photo_proof TEXT NOT NULL,
                target_name TEXT,
                submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


def get_setting(key):
    with get_db() as conn:
        cur = conn.execute("SELECT value FROM admin_settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else None


def set_setting(key, value):
    with get_db() as conn:
        conn.execute("REPLACE INTO admin_settings (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()


def get_current_series():
    with get_db() as conn:
        cur = conn.execute(
            "SELECT id, number, targets_per_player FROM series WHERE is_active = 1 ORDER BY id DESC LIMIT 1")
        return cur.fetchone()


def start_new_series(targets_per_player=None):
    if targets_per_player is None:
        targets_per_player = int(get_setting("targets_per_series") or DEFAULT_TARGETS_PER_PLAYER)

    with get_db() as conn:
        # Получаем номер новой серии
        cur = conn.execute("SELECT MAX(number) FROM series")
        max_num = cur.fetchone()[0]
        new_number = (max_num or 0) + 1

        # Завершаем активную серию, если есть
        conn.execute("UPDATE series SET is_active = 0, ended_at = CURRENT_TIMESTAMP WHERE is_active = 1")

        # Создаём новую
        conn.execute("""
            INSERT INTO series (number, targets_per_player, is_active)
            VALUES (?, ?, 1)
        """, (new_number, targets_per_player))

        series_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return series_id, new_number, targets_per_player


def end_current_series():
    with get_db() as conn:
        # Получаем текущую серию
        cur = conn.execute("SELECT id, number FROM series WHERE is_active = 1")
        series = cur.fetchone()
        if not series:
            return None

        series_id, series_num = series

        # Для каждого игрока проверяем, убил ли он в этой серии
        cur = conn.execute("""
            SELECT DISTINCT killer_id FROM kills WHERE series_id = ?
        """, (series_id,))
        killers = {row[0] for row in cur.fetchall()}

        # Получаем всех живых игроков
        cur = conn.execute("SELECT user_id, warnings FROM players WHERE is_alive = 1")
        players = cur.fetchall()

        warnings_given = []
        eliminated = []

        for user_id, current_warnings in players:
            if user_id in killers:
                # Убил в этой серии — сбрасываем предупреждения
                conn.execute("UPDATE players SET warnings = 0 WHERE user_id = ?", (user_id,))
            else:
                # Не убил — накручиваем предупреждение
                new_warnings = current_warnings + 1
                conn.execute("UPDATE players SET warnings = ? WHERE user_id = ?", (new_warnings, user_id))
                warnings_given.append((user_id, new_warnings))

                if new_warnings >= 2:
                    # Выбывание
                    conn.execute("UPDATE players SET is_alive = 0 WHERE user_id = ?", (user_id,))
                    eliminated.append(user_id)

        # Завершаем серию
        conn.execute("UPDATE series SET is_active = 0, ended_at = CURRENT_TIMESTAMP WHERE id = ?", (series_id,))
        conn.commit()

        return {
            "series_num": series_num,
            "killers_count": len(killers),
            "warnings_given": warnings_given,
            "eliminated": eliminated
        }