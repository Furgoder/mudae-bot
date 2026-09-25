"""Локальный веб-дашборд: Flask + SocketIO для логов, метрик и управления."""

import os
import re
import ast
import threading
import time
import types
from collections import deque
from pathlib import Path
from threading import Lock

import Vars
import Database
from flask import Flask, render_template
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = 'mudae-dashboard-local'
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

_stats_lock = Lock()
_log_lock = Lock()
_VARS_PATH = Path(__file__).resolve().parent / 'Vars.py'

# Порядок и группировка ключей при записи Vars.py / отрисовке формы
VAR_SECTIONS = [
    ('Discord', ['token', 'channelId', 'serverId', 'username']),
    ('Снайпинг', ['snipeEnabled', 'snipeChannelIds', 'snipe_roll_ignore_window', 'gateway_capabilities']),
    ('Миниигры', ['ouroMinigamesEnabled']),
    ('Роллы и клейм', [
        'rollCommand', 'desiredKakeras', 'desiredSeries', 'min_power_threshold',
        'pokeRoll', 'repeatMinute', 'max_rolls_per_session', 'kakera_max_clicks',
        'claim_ttl',
    ]),
    ('Жадность порога', [
        'hunger_minutes_mid', 'hunger_minutes_high',
        'hunger_multiplier', 'hunger_multiplier_high',
    ]),
    ('Джекпот', ['jackpot_multiplier', 'jackpot_threshold_factor']),
    ('Человечность', [
        'humanize_enabled',
        'roll_delay_min', 'roll_delay_max', 'roll_distract_chance',
        'action_delay_min', 'action_delay_max',
        'minigame_gap_min', 'minigame_gap_max',
        'schedule_jitter_max', 'minigame_cd_jitter_max',
        'sleep_mode_enabled', 'sleep_time_start', 'sleep_time_end',
    ]),
]

# Ключи, которые лучше хранить строками (snowflake ID)
_STRING_ID_KEYS = frozenset({'token', 'channelId', 'serverId', 'username', 'rollCommand', 'repeatMinute'})
_LIST_ID_KEYS = frozenset({'snipeChannelIds'})
_SECRET_KEYS = frozenset({'token'})

BOT_STATS = {
    'start_time': time.time(),
    'kakera_earned': 0,
    'ourospheres_earned': 0,
    'successful_claims': 0,
    'stolen_or_missed': 0,
}

# Тайминги «человечности» + следующий прокрут (для UI/логов)
TIMING_STATE = {
    'last_sleep_sec': None,
    'last_sleep_kind': None,
    'last_sleep_at': None,
    'next_roll_at': None,
    'rolling': False,
}

_SLEEP_KIND_LABELS = {
    'roll': 'между роллами',
    'distract': 'отвлёкся',
    'action': 'перед действием',
    'schedule': 'джиттер расписания',
    'minigame': 'между минииграми',
    'poll': 'ожидание ответа',
}

LOG_CACHE = deque(maxlen=200)


def _load_claim_history():
    """Подгружает накопленные метрики из SQLite при старте."""
    Database.init_db()
    stats = Database.load_session_stats()
    with _stats_lock:
        for key, value in stats.items():
            if key in BOT_STATS:
                BOT_STATS[key] = value


def _save_claim_history():
    """Сохраняет BOT_STATS в SQLite (история клеймов пишется отдельно)."""
    with _stats_lock:
        snapshot = dict(BOT_STATS)
    Database.save_session_stats(snapshot)


def get_claim_history_payload():
    """Последние 200 клеймов для UI; в БД хранится полная история."""
    return Database.get_recent_claims(200)


def record_claim(entry):
    """Добавляет успешный клейм в SQLite и пушит в UI последние 200 записей."""
    item = Database.insert_claim(entry)
    if item is None:
        return
    socketio.emit('claim_history_update', get_claim_history_payload())
    return item


def clear_claim_history():
    """Очищает историю клеймов в БД и сбрасывает счётчик сессии."""
    Database.clear_claims()
    update_stats(successful_claims=0)
    socketio.emit('claim_history_update', [])
    return True


_load_claim_history()

LAST_PROFILE = {}
LAST_MINIGAMES = {
    'oh': {'ready_at': None, 'seconds_left': 0, 'ready': True},
    'oc': {'ready_at': None, 'seconds_left': 0, 'ready': True},
    'oq': {'ready_at': None, 'seconds_left': 0, 'ready': True},
}


def _timing_snapshot():
    """Копия TIMING_STATE для JSON (без удержания внешнего лока)."""
    return {
        'last_sleep_sec': TIMING_STATE.get('last_sleep_sec'),
        'last_sleep_kind': TIMING_STATE.get('last_sleep_kind'),
        'last_sleep_label': _SLEEP_KIND_LABELS.get(
            TIMING_STATE.get('last_sleep_kind'), TIMING_STATE.get('last_sleep_kind')
        ),
        'last_sleep_at': TIMING_STATE.get('last_sleep_at'),
        'next_roll_at': TIMING_STATE.get('next_roll_at'),
        'rolling': bool(TIMING_STATE.get('rolling')),
    }


def get_stats_payload():
    """Снимок метрик для фронтенда (включая аптайм и тайминги)."""
    with _stats_lock:
        return {
            'start_time': BOT_STATS['start_time'],
            'uptime': time.time() - BOT_STATS['start_time'],
            'kakera_earned': BOT_STATS['kakera_earned'],
            'ourospheres_earned': BOT_STATS['ourospheres_earned'],
            'successful_claims': BOT_STATS['successful_claims'],
            'stolen_or_missed': BOT_STATS['stolen_or_missed'],
            **_timing_snapshot(),
        }


def update_stats(**kwargs):
    """Обновляет BOT_STATS и рассылает stats_update всем клиентам."""
    with _stats_lock:
        if 'inc_kakera' in kwargs:
            BOT_STATS['kakera_earned'] += int(kwargs.pop('inc_kakera'))
        if 'inc_ourospheres' in kwargs:
            BOT_STATS['ourospheres_earned'] += int(kwargs.pop('inc_ourospheres'))
        if 'inc_claims' in kwargs:
            BOT_STATS['successful_claims'] += int(kwargs.pop('inc_claims'))
        if 'inc_stolen' in kwargs:
            BOT_STATS['stolen_or_missed'] += int(kwargs.pop('inc_stolen'))

        for key, value in kwargs.items():
            if key in BOT_STATS:
                BOT_STATS[key] = value

        payload = {
            'start_time': BOT_STATS['start_time'],
            'uptime': time.time() - BOT_STATS['start_time'],
            'kakera_earned': BOT_STATS['kakera_earned'],
            'ourospheres_earned': BOT_STATS['ourospheres_earned'],
            'successful_claims': BOT_STATS['successful_claims'],
            'stolen_or_missed': BOT_STATS['stolen_or_missed'],
            **_timing_snapshot(),
        }
        stats_snapshot = {
            'kakera_earned': BOT_STATS['kakera_earned'],
            'ourospheres_earned': BOT_STATS['ourospheres_earned'],
            'successful_claims': BOT_STATS['successful_claims'],
            'stolen_or_missed': BOT_STATS['stolen_or_missed'],
        }

    Database.save_session_stats(stats_snapshot)
    socketio.emit('stats_update', payload)
    return payload


def emit_timing(**kwargs):
    """Обновляет TIMING_STATE и пушит stats_update (метрики + sleep / next roll)."""
    with _stats_lock:
        for key in (
            'last_sleep_sec',
            'last_sleep_kind',
            'last_sleep_at',
            'next_roll_at',
            'rolling',
        ):
            if key in kwargs:
                TIMING_STATE[key] = kwargs[key]
        payload = {
            'start_time': BOT_STATS['start_time'],
            'uptime': time.time() - BOT_STATS['start_time'],
            'kakera_earned': BOT_STATS['kakera_earned'],
            'ourospheres_earned': BOT_STATS['ourospheres_earned'],
            'successful_claims': BOT_STATS['successful_claims'],
            'stolen_or_missed': BOT_STATS['stolen_or_missed'],
            **_timing_snapshot(),
        }
    socketio.emit('stats_update', payload)
    return payload


def sleep_kind_label(kind):
    """Человекочитаемая метка вида sleep для логов."""
    if not kind:
        return 'пауза'
    return _SLEEP_KIND_LABELS.get(kind, str(kind))


def get_settings_payload():
    """Обратная совместимость: булевы тумблеры."""
    return {
        'pokeRoll': bool(getattr(Vars, 'pokeRoll', False)),
        'snipeEnabled': bool(getattr(Vars, 'snipeEnabled', False)),
        'ouroMinigamesEnabled': bool(getattr(Vars, 'ouroMinigamesEnabled', False)),
    }


def get_config_payload():
    """Конфиг для UI (TTL буфера и базовый порог)."""
    try:
        claim_ttl = max(1, int(getattr(Vars, 'claim_ttl', 90)))
    except (TypeError, ValueError):
        claim_ttl = 90
    return {
        'claim_ttl': claim_ttl,
        'min_power_threshold': getattr(Vars, 'min_power_threshold', 80),
    }


def _json_safe_value(key, value):
    """Готовит значение Vars для JSON (snowflake ID → строки)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, list):
        if key in _LIST_ID_KEYS:
            return [str(item) for item in value]
        return [
            str(item) if isinstance(item, int) and abs(item) > 10**15 else item
            for item in value
        ]
    if isinstance(value, int) and not isinstance(value, bool):
        if key in _STRING_ID_KEYS or abs(value) > 10**15:
            return str(value)
        return value
    if isinstance(value, float):
        return value
    if value is None:
        return ''
    return str(value)


def get_vars_payload():
    """Все публичные переменные Vars + метаданные типов для формы."""
    values = {}
    type_map = {}
    for key, value in vars(Vars).items():
        if key.startswith('_') or callable(value):
            continue
        if isinstance(value, types.ModuleType):
            continue
        values[key] = _json_safe_value(key, value)
        if isinstance(value, bool):
            type_map[key] = 'bool'
        elif isinstance(value, list):
            type_map[key] = 'list'
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            type_map[key] = 'number'
        else:
            type_map[key] = 'string'

    ordered = []
    seen = set()
    for section, keys in VAR_SECTIONS:
        for key in keys:
            if key in values:
                ordered.append(key)
                seen.add(key)
    for key in values:
        if key not in seen:
            ordered.append(key)

    return {
        'values': values,
        'types': type_map,
        'order': ordered,
        'sections': [
            {'title': title, 'keys': [k for k in keys if k in values]}
            for title, keys in VAR_SECTIONS
        ],
        'secrets': sorted(_SECRET_KEYS & set(values)),
    }


def _format_py_value(key, value):
    """Сериализация Python-литерала для Vars.py."""
    if isinstance(value, bool):
        return 'True' if value else 'False'
    if isinstance(value, list):
        items = []
        for item in value:
            if key in _LIST_ID_KEYS:
                try:
                    items.append(str(int(str(item).strip())))
                except (TypeError, ValueError):
                    items.append(repr(str(item)))
            elif isinstance(item, bool):
                items.append('True' if item else 'False')
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                items.append(repr(item))
            else:
                items.append(repr(str(item)))
        return '[' + ', '.join(items) + ']'
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    # строки и ID
    return repr(str(value))


def _coerce_incoming_value(key, value, current):
    """Приводит значение из JSON формы к типу текущей переменной Vars."""
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ('1', 'true', 'yes', 'on')
        return bool(value)

    if isinstance(current, list):
        if isinstance(value, str):
            text = value.strip()
            if not text:
                raw_items = []
            elif text.startswith('['):
                raw_items = list(ast.literal_eval(text))
            else:
                raw_items = [part.strip() for part in text.split(',') if part.strip()]
        elif isinstance(value, list):
            raw_items = value
        else:
            raw_items = [value]

        result = []
        for item in raw_items:
            if key in _LIST_ID_KEYS:
                result.append(int(str(item).strip()))
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                result.append(item)
            else:
                result.append(str(item).strip())
        return result

    if isinstance(current, float) and not isinstance(current, bool):
        return float(value)
    if isinstance(current, int) and not isinstance(current, bool):
        # snowflake как int в старых конфигах — сохраняем точность через str→int
        return int(str(value).strip())
    return str(value)


def write_vars_file(settings):
    """Полностью перезаписывает Vars.py и применяет значения в память."""
    if not isinstance(settings, dict):
        raise ValueError('settings must be an object')

    coerced = {}
    # Сначала известные ключи из текущего модуля
    for key, current in vars(Vars).items():
        if key.startswith('_') or callable(current):
            continue
        if key not in settings:
            coerced[key] = current
            continue
        coerced[key] = _coerce_incoming_value(key, settings[key], current)

    # Новые ключи из формы (если появятся)
    for key, value in settings.items():
        if key.startswith('_') or key in coerced:
            continue
        if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', key):
            continue
        coerced[key] = value

    lines = [
        '# Конфиг бота. Редактируйте вручную или через вкладку «Настройки» в Web UI.',
        '# После сохранения через UI файл перезаписывается и бот перезапускается.',
        '',
    ]
    written = set()
    for title, keys in VAR_SECTIONS:
        section_keys = [k for k in keys if k in coerced]
        if not section_keys:
            continue
        lines.append(f'# --- {title} ---')
        for key in section_keys:
            lines.append(f'{key} = {_format_py_value(key, coerced[key])}')
            written.add(key)
        lines.append('')

    extras = [k for k in coerced if k not in written]
    if extras:
        lines.append('# --- Прочее ---')
        for key in extras:
            lines.append(f'{key} = {_format_py_value(key, coerced[key])}')
        lines.append('')

    _VARS_PATH.write_text('\n'.join(lines), encoding='utf-8')

    for key, value in coerced.items():
        setattr(Vars, key, value)
    return coerced


def persist_var_to_file(key, value):
    """Точечное обновление булева флага (legacy)."""
    payload = get_vars_payload()['values']
    payload[key] = value
    write_vars_file(payload)
    return True


def emit_log(payload):
    """Кладёт лог в кэш и рассылает клиентам."""
    with _log_lock:
        LOG_CACHE.append(payload)
    socketio.emit('log_event', payload)


def emit_buffer_card(card):
    socketio.emit('buffer_card', card)


def emit_buffer_clear():
    socketio.emit('buffer_clear', {})


def emit_profile_update(state):
    """Рассылает состояние профиля из $tu (profile_update)."""
    global LAST_PROFILE

    def _ready(val):
        if val is True:
            return True
        if val is False:
            return False
        return None

    payload = {
        'rolls_reset_minutes': int(state.get('rolls_reset_minutes', 0) or 0),
        'claim_minutes_left': int(state.get('claim_minutes_left', 0) or 0),
        'rolls_left': int(state.get('rolls_left', 0) or 0),
        'rolls_resets': int(state.get('rolls_resets', 0) or 0),
        'can_claim': bool(state.get('can_claim', False)),
        'rt_ready': _ready(state.get('rt_ready')),
        'rt_minutes_left': int(state.get('rt_minutes_left', 0) or 0),
        'daily_ready': _ready(state.get('daily_ready')),
        'daily_minutes_left': int(state.get('daily_minutes_left', 0) or 0),
        'dk_ready': _ready(state.get('dk_ready')),
        'dk_minutes_left': int(state.get('dk_minutes_left', 0) or 0),
        'current_threshold': float(state.get('current_threshold', 0) or 0),
        'claim_ttl': int(state.get('claim_ttl') or get_config_payload()['claim_ttl']),
        'updated_at': time.time(),
    }
    LAST_PROFILE = payload
    socketio.emit('profile_update', payload)
    socketio.emit('config_update', get_config_payload())
    return payload


def emit_minigame_cooldowns(payload=None):
    """Рассылает кулдауны $oh/$oc/$oq. Без аргумента — повторно шлёт кэш."""
    global LAST_MINIGAMES
    if payload is not None:
        LAST_MINIGAMES = payload
    socketio.emit('minigames_update', LAST_MINIGAMES)
    return LAST_MINIGAMES


def _cooldown_entry(ready_at_dt):
    """Готовит JSON-запись кулдауна из datetime или None."""
    now_ts = time.time()
    if ready_at_dt is None:
        return {'ready_at': None, 'seconds_left': 0, 'ready': True}
    ready_at = ready_at_dt.timestamp() if hasattr(ready_at_dt, 'timestamp') else float(ready_at_dt)
    seconds_left = max(0, int(ready_at - now_ts))
    return {
        'ready_at': ready_at,
        'seconds_left': seconds_left,
        'ready': seconds_left <= 0,
    }


@app.route('/')
def index():
    return render_template('index.html')


@socketio.on('connect')
def on_connect():
    emit('stats_update', get_stats_payload())
    emit('settings_update', get_settings_payload())
    emit('vars_update', get_vars_payload())
    emit('config_update', get_config_payload())
    emit('claim_history_update', get_claim_history_payload())
    if LAST_PROFILE:
        emit('profile_update', LAST_PROFILE)
    emit('minigames_update', LAST_MINIGAMES)
    with _log_lock:
        history = list(LOG_CACHE)
    if history:
        emit('log_history', history)


@socketio.on('save_vars')
def on_save_vars(data):
    """Принимает полный JSON настроек, пишет Vars.py и перезапускает процесс."""
    data = data or {}
    settings = data.get('settings')
    if not isinstance(settings, dict) or not settings:
        emit('action_result', {'ok': False, 'action': 'save_vars', 'error': 'empty settings'})
        return

    try:
        write_vars_file(settings)
    except Exception as error:
        emit('action_result', {'ok': False, 'action': 'save_vars', 'error': str(error)})
        emit_log({
            'tag': 'WARN',
            'message': f'Не удалось сохранить Vars.py: {error}',
            'time': time.strftime('%H:%M:%S', time.localtime()),
            'dim': False,
        })
        return

    socketio.emit('vars_update', get_vars_payload())
    socketio.emit('config_update', get_config_payload())
    emit('action_result', {'ok': True, 'action': 'save_vars', 'restarting': True})
    emit_log({
        'tag': 'SYS',
        'message': 'Vars.py сохранён. Перезапуск бота…',
        'time': time.strftime('%H:%M:%S', time.localtime()),
        'dim': False,
    })

    def _exit():
        time.sleep(0.6)
        os._exit(0)

    threading.Thread(target=_exit, daemon=True, name='save_vars_restart').start()


@socketio.on('update_setting')
def on_update_setting(data):
    """Legacy / живой тумблер: один булев флаг (для snipeEnabled ещё и Gateway)."""
    data = data or {}
    key = data.get('setting')
    value = bool(data.get('value'))
    if key not in ('pokeRoll', 'snipeEnabled', 'ouroMinigamesEnabled'):
        emit('action_result', {'ok': False, 'action': 'update_setting', 'error': f'unknown setting: {key}'})
        return

    setattr(Vars, key, value)
    try:
        payload = get_vars_payload()['values']
        payload[key] = value
        write_vars_file(payload)
        saved = True
    except Exception:
        saved = False

    if key == 'snipeEnabled':
        try:
            import Bot
            Bot.apply_snipe_enabled(value)
        except Exception as error:
            emit_log({
                'tag': 'WARN',
                'message': f'Не удалось переключить Gateway: {error}',
                'time': time.strftime('%H:%M:%S', time.localtime()),
                'dim': False,
            })

    socketio.emit('settings_update', get_settings_payload())
    socketio.emit('vars_update', get_vars_payload())
    emit('action_result', {
        'ok': True,
        'action': 'update_setting',
        'setting': key,
        'value': value,
        'persisted': saved,
    })
    emit_log({
        'tag': 'SYS',
        'message': f"Настройка {key} = {value}" + (' (сохранено в Vars.py)' if saved else ''),
        'time': time.strftime('%H:%M:%S', time.localtime()),
        'dim': True,
    })


@socketio.on('clear_claim_history')
def on_clear_claim_history(_data=None):
    """Очищает историю клеймов и сбрасывает счётчик successful_claims."""
    clear_claim_history()
    emit('action_result', {'ok': True, 'action': 'clear_claim_history'})
    emit_log({
        'tag': 'SYS',
        'message': 'История клеймов очищена.',
        'time': time.strftime('%H:%M:%S', time.localtime()),
        'dim': True,
    })


@socketio.on('force_tu_update')
def on_force_tu_update(_data=None):
    """Принудительный $tu без побочных $daily/$dk/$rolls — только свежий JSON."""
    def _run():
        try:
            from Function import updateProfileState, log_msg
            log_msg('SYS', 'Обновление данных ($tu) из веб-панели...', dim=True)
            state = updateProfileState(skip_actions=True)
            if state is None:
                emit_log({
                    'tag': 'WARN',
                    'message': 'Не удалось обновить профиль через $tu.',
                    'time': time.strftime('%H:%M:%S', time.localtime()),
                    'dim': False,
                })
        except Exception as error:
            emit_log({
                'tag': 'WARN',
                'message': f'Ошибка force_tu_update: {error}',
                'time': time.strftime('%H:%M:%S', time.localtime()),
                'dim': False,
            })

    threading.Thread(target=_run, daemon=True, name='force_tu_update').start()
    emit('action_result', {'ok': True, 'action': 'force_tu_update'})


@socketio.on('force_roll')
def on_force_roll(_data=None):
    def _run():
        try:
            import Bot
            from Function import simpleRoll, log_msg
            log_msg('ROLL', 'Принудительный прокрут из веб-панели...')
            simpleRoll()
            Bot.publish_next_roll(note='после ручного прокрута')
        except Exception as error:
            emit_log({
                'tag': 'WARN',
                'message': f'Ошибка принудительного прокрута: {error}',
                'time': time.strftime('%H:%M:%S', time.localtime()),
                'dim': False,
            })

    threading.Thread(target=_run, daemon=True, name='force_roll').start()
    emit('action_result', {'ok': True, 'action': 'force_roll'})


@socketio.on('force_minigames')
def on_force_minigames(_data=None):
    def _run():
        try:
            import Bot
            from Function import log_msg
            log_msg('ROLL', 'Принудительный запуск миниигр из веб-панели...')
            Bot.next_oh_attempt = None
            Bot.next_oc_attempt = None
            Bot.next_oq_attempt = None
            previous = getattr(Vars, 'ouroMinigamesEnabled', True)
            Vars.ouroMinigamesEnabled = True
            try:
                Bot.run_all_minigames()
            finally:
                Vars.ouroMinigamesEnabled = previous
        except Exception as error:
            emit_log({
                'tag': 'WARN',
                'message': f'Ошибка запуска миниигр: {error}',
                'time': time.strftime('%H:%M:%S', time.localtime()),
                'dim': False,
            })

    threading.Thread(target=_run, daemon=True, name='force_minigames').start()
    emit('action_result', {'ok': True, 'action': 'force_minigames'})


@socketio.on('restart_bot')
def on_restart_bot(_data=None):
    emit('action_result', {'ok': True, 'action': 'restart_bot'})
    emit_log({
        'tag': 'SYS',
        'message': 'Перезапуск бота (os._exit) по команде из панели...',
        'time': time.strftime('%H:%M:%S', time.localtime()),
        'dim': False,
    })

    def _exit():
        time.sleep(0.4)
        os._exit(0)

    threading.Thread(target=_exit, daemon=True).start()


def start_server(host='0.0.0.0', port=5000):
    """Блокирующий запуск SocketIO-сервера (вызывать в отдельном потоке)."""
    socketio.run(
        app,
        host=host,
        port=port,
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
        log_output=False,
    )
