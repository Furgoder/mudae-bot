import threading
import time
import random
from datetime import datetime, timedelta

import schedule

import Accounts
import Vars
import Sniper  # noqa: F401 — регистрирует @bot.gateway.command
import WebDashboard
from Function import (
    _use_account,
    bot,
    simpleRoll,
    use_stacked_rolls,
    log_msg,
    human_sleep,
    set_next_roll_at,
    wait_channel_quiet,
)
from Minigames import play_ouroharvest, play_ourochest, play_ouroquest

_ROLL_JOBS = []
_ROLL_LOCK = threading.Lock()
_ROLL_SEQ = 0
_ROLL_WAKE = threading.Event()
# Роллы и миниигры делят один канал. Пока замок занят, вторая активность ждёт.
_CHANNEL_LOCK = threading.Lock()
_mg_deferred = False
_ROLL_BUSY = False


next_oh_attempt = None
next_oc_attempt = None
next_oq_attempt = None
_mg_next = {}

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


def _window_margin():
    try:
        return max(0.0, float(getattr(Vars, 'roll_window_margin_sec', 90)))
    except (TypeError, ValueError):
        return 90.0


def _session_seconds(account):
    saved = account.last_session_seconds
    if saved and saved > 0:
        return float(saved)
    return 5 * 60


def plan_roll_window(after=None, duration_sec=None, margin_sec=None):
    """Случайный старт в [ресет, следующий ресет − длительность − запас].

    Возвращает (start, finish_by, empty). finish_by — ресет, до которого сессия
    должна закончиться. empty — окно короче сессии, старт ровно в ресет.
    """
    after = after or datetime.now()
    minute = _repeat_minute()
    duration_sec = max(1.0, float(duration_sec if duration_sec is not None else 5 * 60))
    margin_sec = max(0.0, float(margin_sec if margin_sec is not None else _window_margin()))

    def close_of(reset_at):
        return reset_at + timedelta(hours=1) - timedelta(seconds=duration_sec + margin_sec)

    reset_at = after.replace(second=0, microsecond=0, minute=minute)
    if reset_at > after:
        reset_at -= timedelta(hours=1)
    close_at = close_of(reset_at)
    empty = close_at <= reset_at
    if empty or after > close_at:
        reset_at = reset_at + timedelta(hours=1)
        close_at = close_of(reset_at)
        empty = close_at <= reset_at
    finish_by = reset_at + timedelta(hours=1)
    if empty:
        return reset_at, finish_by, True
    earliest = max(after, reset_at)
    if earliest >= close_at:
        reset_at = reset_at + timedelta(hours=1)
        close_at = close_of(reset_at)
        finish_by = reset_at + timedelta(hours=1)
        if close_at <= reset_at:
            return reset_at, finish_by, True
        earliest = max(after, reset_at)
    span = max(0.0, (close_at - earliest).total_seconds())
    start = earliest + timedelta(seconds=random.uniform(0, span))
    return start, finish_by, False


def enqueue_roll(kind, account_key='main', *, not_before=None, finish_by=None, note='', rolls=None):
    """Ставит крутку в общую очередь. Параллельных сессий нет."""
    global _ROLL_SEQ
    when = not_before
    if when is None:
        when = time.time()
    elif hasattr(when, 'timestamp'):
        when = float(when.timestamp())
    else:
        when = float(when)
    finish = finish_by
    if hasattr(finish, 'timestamp'):
        finish = float(finish.timestamp())
    with _ROLL_LOCK:
        _ROLL_SEQ += 1
        _ROLL_JOBS.append({
            'seq': _ROLL_SEQ,
            'kind': kind,
            'account': account_key or 'main',
            'not_before': when,
            'finish_by': finish,
            'note': note or '',
            'rolls': int(rolls) if rolls else None,
        })
        _publish_queued_locked()
    _ROLL_WAKE.set()


def roll_busy():
    """True, пока очередь выполняет уже снятую задачу."""
    return _ROLL_BUSY


def _urgent_waiting():
    """$us и ручной прокрут должны обойти часовую крутку, которая ещё не началась."""
    now = time.time()
    with _ROLL_LOCK:
        return any(
            job['kind'] in ('us', 'force') and job['not_before'] <= now
            for job in _ROLL_JOBS
        )


def _defer_job(job, reason):
    log_msg('ROLL', reason, dim=True)
    enqueue_roll(
        job['kind'],
        job['account'],
        not_before=job['not_before'],
        finish_by=job.get('finish_by'),
        note=job.get('note') or '',
        rolls=job.get('rolls'),
    )


def _publish_queued_locked():
    scheduled = [job for job in _ROLL_JOBS if job['kind'] == 'scheduled']
    if not scheduled:
        return
    nxt = min(scheduled, key=lambda job: job['not_before'])
    account = Accounts.by_key(nxt['account'])
    label = account.label if account else nxt['account']
    publish_next_roll(
        datetime.fromtimestamp(nxt['not_before']),
        note=f"{label}" + (f", {nxt['note']}" if nxt['note'] else ''),
    )
    if account is not None:
        account.next_roll_at = nxt['not_before']


def _schedule_next(account, *, after=None, fresh_hour=True):
    """fresh_hour: эта крутка уже была, следующее окно открывается на следующем ресете."""
    moment = after or datetime.now()
    if fresh_hour:
        minute = _repeat_minute()
        anchor = moment.replace(second=0, microsecond=0, minute=minute)
        if anchor <= moment:
            anchor += timedelta(hours=1)
        moment = anchor
    start, finish_by, empty = plan_roll_window(
        moment,
        _session_seconds(account),
        _window_margin(),
    )
    note = 'ровно в ресет, сессия длиннее часа' if empty else 'окно после ресета'
    if empty:
        log_msg(
            'WARN',
            f"{account.label}: сессия длиннее часа, следующий старт ровно в "
            f"{start.strftime('%H:%M')}.",
        )
    account.next_roll_at = start.timestamp()
    enqueue_roll(
        'scheduled',
        account.key,
        not_before=start,
        finish_by=finish_by,
        note=note,
    )


def _run_roll_job(job):
    global _ROLL_BUSY
    _ROLL_BUSY = True
    try:
        _run_roll_job_body(job)
    finally:
        _ROLL_BUSY = False


def _run_roll_job_body(job):
    account = Accounts.by_key(job['account']) or Accounts.MAIN
    kind = job['kind']
    finish_by = job.get('finish_by')
    stop_before = datetime.fromtimestamp(finish_by) if finish_by else None

    if kind == 'scheduled' and is_sleep_time():
        wake = _sleep_wake_at()
        log_msg('SYS', f"Режим сна: {account.label} отдыхает до {wake.strftime('%H:%M')}")
        _schedule_next(account, after=wake, fresh_hour=False)
        return

    if kind == 'scheduled' and finish_by and time.time() > finish_by:
        log_msg('ROLL', f"{account.label}: окно закрылось, переношу на следующий ресет.", dim=True)
        _schedule_next(account, after=datetime.now(), fresh_hour=False)
        return

    if not _CHANNEL_LOCK.acquire(blocking=False):
        log_msg('ROLL', f"{account.label}: жду, пока в канале закончатся миниигры.", dim=True)
        _CHANNEL_LOCK.acquire()
    try:
        _run_roll_job_locked(job, account, kind, finish_by, stop_before)
    finally:
        _CHANNEL_LOCK.release()


def _run_roll_job_locked(job, account, kind, finish_by, stop_before):
    urgent = kind in ('us', 'force')
    if not urgent and _urgent_waiting():
        _defer_job(job, f"{account.label}: в очереди $us, эту крутку оставляю на потом.")
        return
    if kind == 'us':
        # Кнопка с панели не должна сидеть до ресета из-за чужого ролла.
        quiet_deadline = None
        max_wait = 15
    elif kind in ('startup', 'force'):
        quiet_deadline = stop_before
        max_wait = None if quiet_deadline else 15 * 60
    else:
        quiet_deadline = stop_before
        max_wait = None
    quiet = wait_channel_quiet(
        deadline=quiet_deadline,
        max_wait=max_wait,
        abort=None if urgent else _urgent_waiting,
    )
    if quiet is None:
        _defer_job(job, f"{account.label}: в очереди $us, эту крутку оставляю на потом.")
        return
    if not quiet:
        log_msg('ROLL', f"{account.label}: эфир занят до конца окна, переношу крутку.")
        if kind == 'scheduled':
            _schedule_next(account, after=datetime.now(), fresh_hour=False)
        return

    try:
        if kind == 'us':
            use_stacked_rolls(job.get('rolls') or 20, stop_before=stop_before)
            return
        simpleRoll(
            account,
            record_duration=True,
            stop_before=stop_before,
        )
    except Exception as error:
        log_msg('WARN', f"Сессия {account.label} оборвалась (сеть/Discord): {error}")
    if kind in ('startup', 'scheduled'):
        _schedule_next(account, after=datetime.now())


def roll_worker():
    """Одна очередь на всех: старт сразу, дальше случайное окно, без наложений."""
    log_msg('SYS', "Очередь роллов запущена.", dim=True)
    Accounts.load_saved_durations()
    if is_sleep_time():
        wake = _sleep_wake_at()
        log_msg('SYS', f"Режим сна на старте: первая крутка после {wake.strftime('%H:%M')}")
        for account in Accounts.all_accounts():
            _schedule_next(account, after=wake, fresh_hour=False)
    else:
        for account in Accounts.all_accounts():
            enqueue_roll('startup', account.key, not_before=time.time(), note='старт')
    while True:
        _ROLL_WAKE.wait(timeout=0.5)
        _ROLL_WAKE.clear()
        with _ROLL_LOCK:
            if not _ROLL_JOBS:
                continue
            now = time.time()
            due = [job for job in _ROLL_JOBS if job['not_before'] <= now]
            if not due:
                continue
            due.sort(key=lambda job: (0 if job['kind'] in ('us', 'force') else 1, job['seq']))
            job = due[0]
            _ROLL_JOBS.remove(job)
            _publish_queued_locked()
        _run_roll_job(job)


def scheduled_simple_roll():
    """Совместимость: ручной вызов кладёт основного в очередь."""
    enqueue_roll('force', 'main', note='ручной')


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


def _mg_bucket(account):
    return _mg_next.setdefault(account.key, {'oh': None, 'oc': None, 'oq': None})


def reset_minigame_timers():
    """Сбрасывает кулдауны миниигр у всех аккаунтов (кнопка на панели)."""
    global next_oh_attempt, next_oc_attempt, next_oq_attempt
    _mg_next.clear()
    next_oh_attempt = None
    next_oc_attempt = None
    next_oq_attempt = None


def _sync_main_minigame_timers(account):
    global next_oh_attempt, next_oc_attempt, next_oq_attempt
    if not account.is_main:
        return
    bucket = _mg_bucket(account)
    next_oh_attempt = bucket['oh']
    next_oc_attempt = bucket['oc']
    next_oq_attempt = bucket['oq']


def _play_account_minigames(account, now):
    """$oh/$oc/$oq одного аккаунта. Кулдаун у каждого свой."""
    bucket = _mg_bucket(account)
    games = (
        ('oh', 'Ouroharvest ($oh)', play_ouroharvest),
        ('oc', 'Ourochest ($oc)', play_ourochest),
        ('oq', 'Ouroquest ($oq)', play_ouroquest),
    )
    launched = False
    with _use_account(account):
        for key, title, play in games:
            due = bucket[key]
            if due is not None and now < due:
                continue
            if launched:
                human_sleep(
                    float(getattr(Vars, 'minigame_gap_min', 1.5)),
                    float(getattr(Vars, 'minigame_gap_max', 4.0)),
                    kind='minigame',
                )
            log_msg('ROLL', f"{account.label}: запускаю {title}...", dim=True)
            result = play()
            retry_after = _minigame_retry_delay(result.get('retry_after', 300))
            bucket[key] = now + timedelta(seconds=retry_after)
            log_msg(
                'SYS',
                f"{account.label}: следующая проверка ${key} "
                f"{bucket[key].strftime('%d.%m %H:%M')} "
                f"({result.get('status', 'error')}, sleep+КД {retry_after:.0f}с).",
                dim=True,
            )
            launched = True
    _sync_main_minigame_timers(account)
    return launched


def run_all_minigames():
    """Запускает $oh/$oc/$oq у основного и у каждого слейва."""
    global _mg_deferred
    if is_sleep_time():
        publish_minigame_cooldowns()
        return

    if not getattr(Vars, 'ouroMinigamesEnabled', True):
        publish_minigame_cooldowns()
        return

    if not _CHANNEL_LOCK.acquire(blocking=False):
        if not _mg_deferred:
            log_msg('SYS', 'Миниигры подождут: в канале идёт прокрут.', dim=True)
            _mg_deferred = True
        publish_minigame_cooldowns()
        return
    _mg_deferred = False
    try:
        _run_minigames_locked()
    finally:
        _CHANNEL_LOCK.release()


def _run_minigames_locked():
    now = datetime.now()
    played = False
    for account in Accounts.all_accounts():
        bucket = _mg_bucket(account)
        due = any(bucket[key] is None or now >= bucket[key] for key in ('oh', 'oc', 'oq'))
        if not due:
            continue
        if played:
            human_sleep(
                float(getattr(Vars, 'minigame_gap_min', 1.5)),
                float(getattr(Vars, 'minigame_gap_max', 4.0)),
                kind='minigame',
            )
        try:
            _play_account_minigames(account, now)
        except Exception as error:
            log_msg('WARN', f"{account.label}: миниигры: {error}")
        played = True

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
    names = ', '.join(account.label for account in Accounts.all_accounts())
    log_msg(
        'SYS',
        f"Рой: {names}. Ресет роллов в XX:{_repeat_minute():02d}, "
        f"старт случайный после ресета.",
    )

    # 2. Gateway сразу — парсер какеры ($k / $ku) не зависит от снайпа
    start_gateway()

    # 3. Очередь роллов: первый проход сразу, дальше окно после ресета
    threading.Thread(target=roll_worker, daemon=True, name='RollQueue').start()

    # 4. Миниигры по-прежнему раз в минуту
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
