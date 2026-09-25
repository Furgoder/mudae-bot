import threading
import time
import random
from datetime import datetime, timedelta

import schedule

import Vars
import Sniper  # noqa: F401 — регистрирует @bot.gateway.command
import WebDashboard
from Function import bot, simpleRoll, log_msg, human_sleep, set_next_roll_at
from Minigames import play_ouroharvest, play_ourochest, play_ouroquest


next_oh_attempt = None
next_oc_attempt = None
next_oq_attempt = None

_gateway_thread = None
_gateway_lock = threading.Lock()
_gateway_wanted = False


def _repeat_minute():
    try:
        return max(0, min(59, int(str(Vars.repeatMinute).lstrip('0') or '0')))
    except (TypeError, ValueError):
        return 0


def compute_next_roll_datetime(after=None):
    """Ближайший час с минутой repeatMinute (без джиттера)."""
    now = after or datetime.now()
    minute = _repeat_minute()
    candidate = now.replace(second=0, microsecond=0, minute=minute)
    if candidate <= now + timedelta(seconds=1):
        candidate += timedelta(hours=1)
    return candidate


def publish_next_roll(when=None, *, note=None):
    """Публикует next_roll_at в UI/метрики и пишет в лог."""
    target = when or compute_next_roll_datetime()
    set_next_roll_at(target)
    stamp = target.strftime('%H:%M:%S') if hasattr(target, 'strftime') else str(target)
    ts = target.timestamp() if hasattr(target, 'timestamp') else float(target)
    left = max(0, int(ts - time.time()))
    extra = f" ({note})" if note else ''
    log_msg(
        'SYS',
        f"Следующий прокрут: {stamp} (через {left // 60}м {left % 60}с){extra}.",
        dim=True,
    )
    return target


def _parse_hhmm(value, fallback='00:00'):
    """Разбирает 'HH:MM' → (hour, minute)."""
    text = str(value if value is not None else fallback).strip()
    parts = text.split(':')
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f'invalid HH:MM: {text!r}')
    return hour, minute


def is_sleep_time(now=None):
    """True, если режим сна включён и сейчас внутри [start, end) (через полночь тоже)."""
    if not getattr(Vars, 'sleep_mode_enabled', False):
        return False
    now = now or datetime.now()
    try:
        start_h, start_m = _parse_hhmm(getattr(Vars, 'sleep_time_start', '02:00'), '02:00')
        end_h, end_m = _parse_hhmm(getattr(Vars, 'sleep_time_end', '09:00'), '09:00')
    except (TypeError, ValueError):
        return False
    start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


def _sleep_wake_at(now=None):
    """Ближайший datetime пробуждения (sleep_time_end)."""
    now = now or datetime.now()
    try:
        end_h, end_m = _parse_hhmm(getattr(Vars, 'sleep_time_end', '09:00'), '09:00')
    except (TypeError, ValueError):
        end_h, end_m = 9, 0
    wake = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if wake <= now:
        wake += timedelta(days=1)
    return wake


def _minigame_retry_delay(retry_after):
    """Кулдаун миниигры + случайный джиттер, чтобы не бить ровно в таймер."""
    base = float(retry_after or 300)
    if not getattr(Vars, 'humanize_enabled', True):
        return base
    jitter = float(getattr(Vars, 'minigame_cd_jitter_max', 120))
    if jitter <= 0:
        return base
    return base + random.uniform(0, jitter)


def scheduled_simple_roll():
    """Роллы по расписанию со случайным сдвигом после :repeatMinute."""
    if is_sleep_time():
        wake = _sleep_wake_at()
        set_next_roll_at(wake)
        log_msg('SYS', f"Режим сна: бот отдыхает до {wake.strftime('%H:%M')}")
        return

    jitter_max = float(getattr(Vars, 'schedule_jitter_max', 90))
    # Следующий час после этой сессии — чтобы UI уже во время роллов знал «далее»
    upcoming = compute_next_roll_datetime(datetime.now() + timedelta(minutes=1))

    if getattr(Vars, 'humanize_enabled', True) and jitter_max > 0:
        wait = random.uniform(0, jitter_max)
        start_at = datetime.now() + timedelta(seconds=wait)
        publish_next_roll(start_at, note=f'джиттер +{wait:.0f}с')
        human_sleep(wait, wait, kind='schedule', log=False)  # уже залогировали выше
    else:
        publish_next_roll(datetime.now(), note='старт по расписанию')
    try:
        set_next_roll_at(upcoming)  # во время сессии UI уже знает «далее»
        simpleRoll()
    except Exception as error:
        log_msg('WARN', f"Сессия роллов оборвалась (сеть/Discord): {error}")
    finally:
        publish_next_roll(upcoming, note='после сессии')


def publish_minigame_cooldowns():
    """Собирает next_oh/oc/oq и отправляет на фронтенд через SocketIO."""
    payload = {
        'oh': WebDashboard._cooldown_entry(next_oh_attempt),
        'oc': WebDashboard._cooldown_entry(next_oc_attempt),
        'oq': WebDashboard._cooldown_entry(next_oq_attempt),
    }
    WebDashboard.emit_minigame_cooldowns(payload)
    return payload


def start_gateway():
    """Запускает bot.gateway.run() в daemon-потоке (идемпотентно)."""
    global _gateway_thread, _gateway_wanted
    with _gateway_lock:
        _gateway_wanted = True
        if _gateway_thread is not None and _gateway_thread.is_alive():
            log_msg('SYS', "Gateway уже запущен.", dim=True)
            return

        def _run():
            log_msg('SYS', "WebSockets Gateway стартует...")
            try:
                bot.gateway.run(auto_reconnect=True)
            except Exception as error:
                log_msg('WARN', f"Gateway завершился: {error}")
            finally:
                log_msg('SYS', "Gateway поток остановлен.", dim=True)

        _gateway_thread = threading.Thread(
            target=_run, daemon=True, name='DiscordGateway'
        )
        _gateway_thread.start()
        log_msg('SYS', "Снайпер Gateway: ВКЛ (поток запущен).")


def stop_gateway():
    """Жёстко закрывает Gateway (snipeEnabled=False)."""
    global _gateway_wanted
    with _gateway_lock:
        _gateway_wanted = False
        try:
            bot.gateway.close()
            log_msg('SYS', "Снайпер Gateway: ВЫКЛ (gateway.close).")
        except Exception as error:
            log_msg('WARN', f"gateway.close: {error}")


def apply_snipe_enabled(enabled):
    """Включает/выключает снайп. Gateway всегда нужен для парсинга какеры ($k/$ku)."""
    enabled = bool(enabled)
    Vars.snipeEnabled = enabled
    # Gateway держим всегда: без него не ловим +N ($k) и КД ($ku) между роллами
    start_gateway()
    if enabled:
        log_msg('SYS', "Снайпер: ВКЛ (клейм/какера в чужих каналах).")
    else:
        log_msg('SYS', "Снайпер: ВЫКЛ (Gateway остаётся для парсинга чата какеры).")


def run_all_minigames():
    """Запускает $oh/$oc/$oq с учётом индивидуального кулдауна каждой игры."""
    global next_oh_attempt, next_oc_attempt, next_oq_attempt

    if is_sleep_time():
        publish_minigame_cooldowns()
        return

    if not getattr(Vars, 'ouroMinigamesEnabled', True):
        publish_minigame_cooldowns()
        return

    now = datetime.now()
    launched = False

    if next_oh_attempt is None or now >= next_oh_attempt:
        log_msg('ROLL', "Запускаю Ouroharvest ($oh) по таймеру...", dim=True)
        result = play_ouroharvest()
        retry_after = _minigame_retry_delay(result.get('retry_after', 300))
        next_oh_attempt = now + timedelta(seconds=retry_after)
        log_msg(
            'SYS',
            f"Следующая проверка $oh: {next_oh_attempt.strftime('%d.%m %H:%M')} "
            f"({result.get('status', 'error')}, sleep+КД {retry_after:.0f}с).",
            dim=True,
        )
        launched = True

    if next_oc_attempt is None or now >= next_oc_attempt:
        if launched:
            human_sleep(
                float(getattr(Vars, 'minigame_gap_min', 1.5)),
                float(getattr(Vars, 'minigame_gap_max', 4.0)),
                kind='minigame',
            )
        log_msg('ROLL', "Запускаю Ourochest ($oc) по таймеру...", dim=True)
        result = play_ourochest()
        retry_after = _minigame_retry_delay(result.get('retry_after', 300))
        next_oc_attempt = now + timedelta(seconds=retry_after)
        log_msg(
            'SYS',
            f"Следующая проверка $oc: {next_oc_attempt.strftime('%d.%m %H:%M')} "
            f"({result.get('status', 'error')}, sleep+КД {retry_after:.0f}с).",
            dim=True,
        )
        launched = True

    if next_oq_attempt is None or now >= next_oq_attempt:
        if launched:
            human_sleep(
                float(getattr(Vars, 'minigame_gap_min', 1.5)),
                float(getattr(Vars, 'minigame_gap_max', 4.0)),
                kind='minigame',
            )
        log_msg('ROLL', "Запускаю Ouroquest ($oq) по таймеру...", dim=True)
        result = play_ouroquest()
        retry_after = _minigame_retry_delay(result.get('retry_after', 300))
        next_oq_attempt = now + timedelta(seconds=retry_after)
        log_msg(
            'SYS',
            f"Следующая проверка $oq: {next_oq_attempt.strftime('%d.%m %H:%M')} "
            f"({result.get('status', 'error')}, sleep+КД {retry_after:.0f}с).",
            dim=True,
        )

    publish_minigame_cooldowns()


def run_scheduler():
    log_msg('SYS', "Фоновый поток расписания запущен.", dim=True)
    while True:
        try:
            schedule.run_pending()
        except Exception as error:
            # Иначе ConnectionError убивает весь поток — расписание больше не тикает.
            log_msg('WARN', f"Ошибка в задаче расписания: {error}")
        time.sleep(1)


def start_bot():
    # 0. Веб-дашборд в отдельном потоке
    threading.Thread(
        target=WebDashboard.start_server,
        kwargs={'host': '0.0.0.0', 'port': 5000},
        daemon=True,
        name='WebDashboard',
    ).start()
    log_msg('SYS', "Веб-дашборд: http://127.0.0.1:5000")
    publish_minigame_cooldowns()

    # 1. Индикация запуска
    log_msg('SYS', "Бот успешно запущен!")
    jitter_max = float(getattr(Vars, 'schedule_jitter_max', 90))
    if getattr(Vars, 'humanize_enabled', True) and jitter_max > 0:
        log_msg(
            'SYS',
            f"Расписание: роллы каждый час в XX:{Vars.repeatMinute} "
            f"(+0…{int(jitter_max)}с джиттер)",
        )
    else:
        log_msg('SYS', f"Расписание: роллы каждый час в XX:{Vars.repeatMinute}")

    # 2. Gateway сразу — парсер какеры ($k / $ku) не зависит от снайпа
    start_gateway()

    # 3. Моментальный запуск для отладки
    log_msg('ROLL', "Выполняю тестовый стартовый прокрут...", dim=True)
    try:
        simpleRoll()
    finally:
        publish_next_roll(note='после старта')

    # 4. Настройка планировщика
    timeString = f":{Vars.repeatMinute}"
    schedule.every().hour.at(timeString).do(scheduled_simple_roll)
    schedule.every(1).minutes.do(run_all_minigames)

    # 5. Планировщик в фоне
    threading.Thread(target=run_scheduler, daemon=True).start()

    if getattr(Vars, 'snipeEnabled', False):
        log_msg('SYS', "Снайпер: ВКЛ.")
    else:
        log_msg('SYS', "Снайпер: ВЫКЛ (чат какеры всё равно слушаем через Gateway).")

    # Главный поток держит процесс живым (Gateway больше не блокирует main)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    start_bot()
