"""SQLite-хранилище истории клеймов и накопленных метрик сессии."""

import json
import sqlite3
import time
from pathlib import Path
from threading import Lock

DB_PATH = Path(__file__).resolve().parent / 'mudae_history.db'
_JSON_HISTORY_PATH = Path(__file__).resolve().parent / 'claim_history.json'
_lock = Lock()

_SESSION_KEYS = (
    'kakera_earned',
    'ourospheres_earned',
    'successful_claims',
    'stolen_or_missed',
)

_initialized = False


def _connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _claim_row_to_item(row):
    return {
        'id': row['id'],
        'name': row['name'],
        'series': row['series'] or '',
        'power': int(row['power'] or 0),
        'kakera': int(row['kakera'] or 0),
        'ourospheres': int(row['ourospheres'] or 0),
        'reason': row['reason'] or '',
        'ts': float(row['timestamp'] or 0),
    }


def init_db():
    """Создаёт mudae_history.db, таблицы и один раз мигрирует claim_history.json."""
    global _initialized
    with _lock:
        if _initialized:
            return
        conn = _connect()
        try:
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS claims (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    series TEXT,
                    power INTEGER,
                    kakera INTEGER,
                    ourospheres INTEGER,
                    reason TEXT,
                    timestamp REAL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS session_stats (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    kakera_earned INTEGER NOT NULL DEFAULT 0,
                    ourospheres_earned INTEGER NOT NULL DEFAULT 0,
                    successful_claims INTEGER NOT NULL DEFAULT 0,
                    stolen_or_missed INTEGER NOT NULL DEFAULT 0
                )
                '''
            )
            conn.execute(
                'INSERT OR IGNORE INTO session_stats (id) VALUES (1)'
            )
            _migrate_json_history(conn)
            _bootstrap_stats_from_claims(conn)
        finally:
            conn.close()
        _initialized = True


def _migrate_json_history(conn):
    count = conn.execute('SELECT COUNT(*) FROM claims').fetchone()[0]
    if count or not _JSON_HISTORY_PATH.exists():
        return
    try:
        with open(_JSON_HISTORY_PATH, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except Exception:
        return
    if not isinstance(data, list):
        return
    for item in data:
        if not isinstance(item, dict):
            continue
        conn.execute(
            '''
            INSERT INTO claims (name, series, power, kakera, ourospheres, reason, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                str(item.get('name') or '???'),
                str(item.get('series') or ''),
                int(item.get('power') or 0),
                int(item.get('kakera') or 0),
                int(item.get('ourospheres') or 0),
                str(item.get('reason') or ''),
                float(item.get('ts') or time.time()),
            ),
        )


def _bootstrap_stats_from_claims(conn):
    """Если метрики ещё нулевые, заполняет их суммами из уже лежащих клеймов."""
    row = conn.execute(
        '''
        SELECT kakera_earned, ourospheres_earned, successful_claims, stolen_or_missed
        FROM session_stats WHERE id = 1
        '''
    ).fetchone()
    if not row:
        return
    if any(int(row[k] or 0) for k in _SESSION_KEYS):
        return
    totals = conn.execute(
        '''
        SELECT
            COALESCE(SUM(kakera), 0),
            COALESCE(SUM(ourospheres), 0),
            COUNT(*)
        FROM claims
        '''
    ).fetchone()
    if not totals or int(totals[2] or 0) == 0:
        return
    conn.execute(
        '''
        UPDATE session_stats
        SET kakera_earned = ?, ourospheres_earned = ?, successful_claims = ?
        WHERE id = 1
        ''',
        (int(totals[0]), int(totals[1]), int(totals[2])),
    )


def insert_claim(entry):
    """Добавляет клейм в БД (история без лимита) и возвращает нормализованную запись."""
    init_db()
    if not isinstance(entry, dict):
        return None
    item = {
        'name': str(entry.get('name') or '???'),
        'series': str(entry.get('series') or ''),
        'power': int(entry.get('power') or 0),
        'kakera': int(entry.get('kakera') or 0),
        'ourospheres': int(entry.get('ourospheres') or 0),
        'reason': str(entry.get('reason') or ''),
        'ts': float(entry.get('ts') or time.time()),
    }
    with _lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                '''
                INSERT INTO claims (name, series, power, kakera, ourospheres, reason, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    item['name'],
                    item['series'],
                    item['power'],
                    item['kakera'],
                    item['ourospheres'],
                    item['reason'],
                    item['ts'],
                ),
            )
            item['id'] = cursor.lastrowid
        finally:
            conn.close()
    return item


def get_recent_claims(limit=200):
    """Последние N клеймов, новые сверху (для SocketIO / UI)."""
    init_db()
    limit = max(0, int(limit))
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                '''
                SELECT id, name, series, power, kakera, ourospheres, reason, timestamp
                FROM claims
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                ''',
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    return [_claim_row_to_item(row) for row in rows]


def clear_claims():
    """Удаляет все клеймы (явная команда UI). Сама БД лимит не накладывает."""
    init_db()
    with _lock:
        conn = _connect()
        try:
            conn.execute('DELETE FROM claims')
        finally:
            conn.close()


def load_session_stats():
    """Читает накопленные BOT_STATS (без start_time / uptime)."""
    init_db()
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                '''
                SELECT kakera_earned, ourospheres_earned, successful_claims, stolen_or_missed
                FROM session_stats WHERE id = 1
                '''
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return {key: 0 for key in _SESSION_KEYS}
    return {key: int(row[key] or 0) for key in _SESSION_KEYS}


def save_session_stats(stats):
    """Сохраняет метрики, чтобы kakera_earned и остальные не сбрасывались при рестарте."""
    init_db()
    payload = {key: int(stats.get(key) or 0) for key in _SESSION_KEYS}
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                '''
                UPDATE session_stats
                SET kakera_earned = ?,
                    ourospheres_earned = ?,
                    successful_claims = ?,
                    stolen_or_missed = ?
                WHERE id = 1
                ''',
                (
                    payload['kakera_earned'],
                    payload['ourospheres_earned'],
                    payload['successful_claims'],
                    payload['stolen_or_missed'],
                ),
            )
        finally:
            conn.close()
