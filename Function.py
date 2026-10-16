import json
import random
import re
import threading
import time
import requests
import Vars
from discum.utils.slash import SlashCommander

import Accounts
import WebDashboard

_ACCOUNT_LOCAL = threading.local()


def _use_account(account):
    """На время блока команды и клики идут от этого аккаунта."""
    class _Ctx:
        def __enter__(self):
            self._prev = getattr(_ACCOUNT_LOCAL, 'account', None)
            _ACCOUNT_LOCAL.account = account or Accounts.MAIN
            return _ACCOUNT_LOCAL.account

        def __exit__(self, exc_type, exc, tb):
            if self._prev is None:
                if hasattr(_ACCOUNT_LOCAL, 'account'):
                    del _ACCOUNT_LOCAL.account
            else:
                _ACCOUNT_LOCAL.account = self._prev
            return False

    return _Ctx()


def _active_account():
    return getattr(_ACCOUNT_LOCAL, 'account', None) or Accounts.MAIN


def human_delay(lo, hi=None):
    """Случайная пауза в секундах (имитация человека).

    human_delay(1.0, 2.5) — равномерно в [lo, hi].
    human_delay(0.8) — около базы с джиттером ±35%.
    При humanize_enabled=False возвращает середину диапазона / базу.
    """
    lo = float(lo)
    if hi is None:
        if not getattr(Vars, 'humanize_enabled', True):
            return max(0.0, lo)
        spread = max(0.05, abs(lo) * 0.35)
        return max(0.05, random.uniform(lo - spread, lo + spread))

    hi = float(hi)
    if hi < lo:
        lo, hi = hi, lo
    if not getattr(Vars, 'humanize_enabled', True):
        return (lo + hi) / 2.0
    return random.uniform(lo, hi)


def human_sleep(lo, hi=None, *, kind=None, log=None):
    """time.sleep с человеческим разбросом. Возвращает фактическую паузу.

    kind — тип паузы для UI. В ленту событий пауза не пишется.
    """
    delay = human_delay(lo, hi)
    should_log = bool(log)
    if kind:
        WebDashboard.emit_timing(
            last_sleep_sec=round(delay, 2),
            last_sleep_kind=kind,
            last_sleep_at=time.time(),
        )
    if should_log and delay > 0:
        label = WebDashboard.sleep_kind_label(kind)
        log_msg('SYS', f"Sleep {delay:.2f}с ({label}).", dim=True)
    if delay > 0:
        time.sleep(delay)
    return delay


def action_pause():
    """Короткая пауза перед кликом/реакцией (как будто читаем карту)."""
    lo = float(getattr(Vars, 'action_delay_min', 0.35))
    hi = float(getattr(Vars, 'action_delay_max', 1.1))
    return human_sleep(lo, hi, kind='action')


def roll_pause():
    """Пауза между роллами; иногда чуть длиннее («отвлёкся»)."""
    lo = float(getattr(Vars, 'roll_delay_min', 1.2))
    hi = float(getattr(Vars, 'roll_delay_max', 2.8))
    slept = human_sleep(lo, hi, kind='roll')
    chance = float(getattr(Vars, 'roll_distract_chance', 0.08))
    if getattr(Vars, 'humanize_enabled', True) and chance > 0 and random.random() < chance:
        slept += human_sleep(2.0, 6.0, kind='distract')
    return slept


def set_rolling(active):
    """Помечает сессию прокрутов для UI (next roll / «идёт сессия»)."""
    WebDashboard.emit_timing(rolling=bool(active))


def set_next_roll_at(when):
    """Фиксирует unix-время следующего прокрута (datetime | float | None)."""
    if when is None:
        ts = None
    elif hasattr(when, 'timestamp'):
        ts = float(when.timestamp())
    else:
        ts = float(when)
    WebDashboard.emit_timing(next_roll_at=ts)
    return ts

botID = Accounts.MUDAE_BOT_ID
bot = Accounts.MAIN.client
auth = Accounts.MAIN.auth

url = f'https://discord.com/api/v9/channels/{Vars.channelId}/messages'

# Время последнего собственного слэш-ролла (чтобы Sniper не перехватывал ответ Mudae)
LAST_OUR_ROLL_TIME = 0.0

# Профиль основного аккаунта. Тот же dict, что Accounts.MAIN.profile.
PROFILE_STATE = Accounts.MAIN.profile

# Зеркало кулдауна основного. Читатели ходят в аккаунт через kakera_on_cooldown().
KAKERA_COOLDOWN_UNTIL = 0.0

# Последняя чужая карта в нашем канале (эфир занят).
LAST_FOREIGN_ROLL_AT = 0.0

# $rolls можно тратить только один раз за интервал сброса роллов
ROLLS_RESET_USED_UNTIL = 0.0
_DEFAULT_ROLLS_INTERVAL_MINUTES = 60
_ROLLS_INTERVAL_BLOCK_RE = re.compile(
    r"one rolls reset per interval",
    re.IGNORECASE,
)
_ROLLS_INTERVAL_TIME_RE = re.compile(
    r"time left:\s*(?:(\d+)\s*h\s*)?(\d+)\s*min",
    re.IGNORECASE,
)


def rolls_reset_on_cooldown(account=None):
    account = account or _active_account()
    return time.time() < account.rolls_reset_used_until


def rolls_reset_cooldown_left_seconds(account=None):
    account = account or _active_account()
    return max(0, int(account.rolls_reset_used_until - time.time()))


def _set_rolls_reset_cooldown(minutes, *, reason='', account=None):
    """Блокирует повторный $rolls до конца текущего интервала."""
    global ROLLS_RESET_USED_UNTIL
    account = account or _active_account()
    minutes = max(1, int(minutes or 0))
    account.rolls_reset_used_until = time.time() + minutes * 60
    if account.is_main:
        ROLLS_RESET_USED_UNTIL = account.rolls_reset_used_until
    suffix = f" ({reason})" if reason else ''
    log_msg(
        'ROLL',
        f"$rolls уже использован в этом интервале{suffix} ({account.label}). "
        f"Следующий через {minutes} мин "
        f"(до {time.strftime('%H:%M:%S', time.localtime(account.rolls_reset_used_until))}).",
    )


def _parse_rolls_interval_minutes(text):
    match = _ROLLS_INTERVAL_TIME_RE.search(text or '')
    if not match:
        return _DEFAULT_ROLLS_INTERVAL_MINUTES
    return int(match.group(1) or 0) * 60 + int(match.group(2) or 0)


def _parse_rolls_interval_block(text):
    """«One rolls reset per interval. Time left: 5 min.»"""
    if not _ROLLS_INTERVAL_BLOCK_RE.search(text or ''):
        return None
    minutes = _parse_rolls_interval_minutes(text)
    _set_rolls_reset_cooldown(minutes, reason='ответ Mudae')
    return {'type': 'rolls_interval', 'minutes': minutes}

# Дедуп логов DROP и начислений по message id
_processed_message_ids = {}

# Кэш Discord-имён (display + login)
_CACHED_USERNAME = None
_CACHED_USERNAMES = None
_CACHED_USER_ID = None
# Слушатель канала поднял подписку (Sniper выставляет после lazyGuild).
GATEWAY_LISTENING = threading.Event()

# Сценарий A: отказ / КД какеры
# "username, You can't react to kakera for 1h 01 min. ($ku)"
_KAKERA_CD_RE = re.compile(
    r"(?:^|\n)\s*(?:(?P<name>[^,\n]+?)\s*,\s*)?"
    r"you can't react to kakera for\s+"
    r"(?:(?P<hours>\d+)\s*h\s*)?"
    r"(?:(?P<mins>\d+)\s*min)?",
    re.IGNORECASE,
)
# Сумма: 110 / 2,241 / 2241
_AMOUNT = r'(?P<amount>\d{1,3}(?:,\d{3})+|\d+)'
# Сценарий B: успех какеры — ищем хвост "Name +N ($k)" (после эмодзи / breaks down / Free)
# Примеры:
#   => **username +2,241** ($k)
#   <:kakeraP:…>(Free) **username +110** ($k)
#   💎 Name +N ($k)
_KAKERA_GAIN_RE = re.compile(
    rf'(?:💎\s*)?(?P<name>[A-Za-z0-9_.\-]{{1,32}})\s*\+{_AMOUNT}\s*'
    r'(?:\(\s*\$?k\s*\)|\$k(?!u)\b)',
    re.IGNORECASE,
)
# Фолбэк без имени: "+110 ($k)" / "+2,241 ($k)"
_KAKERA_PLUS_RE = re.compile(
    rf'\+{_AMOUNT}\s*(?:\(\s*\$?k\s*\)|\$k(?!u)\b)',
    re.IGNORECASE,
)
# Сценарий C: клейм / свадьба
# "💖 **claimer** and **Character Name** are now married! 💖"
_MARRIAGE_RE = re.compile(
    r'are now married!',
    re.IGNORECASE,
)
_MARRIAGE_PAIR_RE = re.compile(
    r'(?P<a>[^\n]+?)\s+and\s+(?P<b>[^\n]+?)\s+are now married',
    re.IGNORECASE,
)
_DISCORD_EMOJI_RE = re.compile(r'<a?:\w+:\d+>')



def _remember_message_id(message_id, *, limit=2500):
    """True если id новый (ещё не обрабатывали)."""
    if message_id is None:
        return True
    mid = str(message_id)
    if mid in _processed_message_ids:
        return False
    _processed_message_ids[mid] = time.time()
    if len(_processed_message_ids) > limit:
        # удаляем самые старые
        for old_id, _ in sorted(_processed_message_ids.items(), key=lambda x: x[1])[: limit // 2]:
            _processed_message_ids.pop(old_id, None)
    return True


def _set_kakera_cooldown(until, account=None):
    """Ставит кулдаун реакции конкретному аккаунту. У основного обновляет и глобал."""
    global KAKERA_COOLDOWN_UNTIL
    account = account or _active_account()
    account.kakera_cooldown_until = float(until or 0)
    if account.is_main:
        KAKERA_COOLDOWN_UNTIL = account.kakera_cooldown_until


def kakera_on_cooldown(account=None):
    account = account or _active_account()
    return time.time() < account.kakera_cooldown_until


def kakera_cooldown_left_seconds(account=None):
    account = account or _active_account()
    return max(0, int(account.kakera_cooldown_until - time.time()))


def account_reaction_ready(account):
    """Силы на реакцию хватает. Смотрит профиль этого аккаунта, не общий глобал."""
    return not reaction_energy_missing(account.profile)


def reaction_energy_missing(state=None):
    """Силы на реакцию нет: кулдаун Mudae, kakera_ready=False или Power ниже цены кнопки."""
    if state is None:
        state = _active_account().profile
    account = Accounts.by_key(state.get('account_key')) or _active_account()
    if kakera_on_cooldown(account):
        return True
    if state.get('kakera_ready') is False:
        return True
    if state.get('kakera_power_full'):
        return False
    power = state.get('kakera_power')
    cost = int(state.get('kakera_cost') or 0)
    if power is None or cost <= 0:
        return False
    try:
        return int(power) < cost
    except (TypeError, ValueError):
        return False


def note_kakera_restored_by_dk(account=None):
    """$dk полностью возвращает силу реакции. Старый Power клики больше не блокирует."""
    account = account or _active_account()
    _set_kakera_cooldown(0.0, account)
    account.profile['kakera_ready'] = True
    account.profile['kakera_power'] = None
    account.profile['kakera_power_full'] = True
    account.profile['dk_ready'] = False
    account.profile['dk_minutes_left'] = 0
    sync_profile_state(account.profile)


# Ожидающий клейм (карточка до подтверждения «married»)
_PENDING_CLAIM = None
# Последняя наша свадьба. Gateway часто разбирает её раньше, чем опрос после клика.
_LAST_OUR_MARRIAGE = {'at': 0.0, 'name': ''}


def note_pending_claim(*, name='', series='', power=0, reason=''):
    """Запоминает карту перед кликом клейма — для истории после свадьбы."""
    global _PENDING_CLAIM
    _PENDING_CLAIM = {
        'name': (name or '').strip(),
        'series': series or '',
        'power': int(power or 0),
        'reason': reason or '',
        'at': time.time(),
    }


def _parse_marriage_parties(parsed):
    """Разбирает свадьбу Mudae: (claimer, character) или None.

    Формат: «claimer and Character Name are now married!»
    """
    match = _MARRIAGE_PAIR_RE.search(parsed)
    if not match:
        return None
    claimer = re.sub(r'[💖💕❤️❤]', '', match.group('a')).strip(' *·-')
    character = re.sub(r'[💖💕❤️❤]', '', match.group('b')).strip(' *·-')
    if not claimer or not character:
        return None
    return {'claimer': claimer, 'character': character}


def try_use_dk_for_kakera_cd():
    """$dk только если команда готова и силы на реакцию нет."""
    account = _active_account()
    if not account.profile.get('dk_ready'):
        return False
    if not reaction_energy_missing(account.profile):
        return False
    if _send_text_command('$dk', account) not in (200, 204):
        return False
    note_kakera_restored_by_dk(account)
    log_msg('KAKER', f"$dk отправлен ({account.label}). Сила реакции восстановлена.")
    return True


def _resolve_username():
    """Имя аккаунта: Vars.username или GET /users/@me."""
    names = _our_discord_names()
    return names[0] if names else ''


def _our_user_id():
    """Discord id основного аккаунта."""
    return Accounts.MAIN.user_id or _CACHED_USER_ID or ''


def _our_discord_names():
    """Имена всех аккаунтов роя (display / username / Vars.username)."""
    global _CACHED_USERNAME, _CACHED_USERNAMES, _CACHED_USER_ID
    if Accounts.MAIN.user_id:
        _CACHED_USER_ID = Accounts.MAIN.user_id
    names = []
    for account in Accounts.all_accounts():
        names.extend(account.names())
    if not names and _CACHED_USERNAMES:
        names.extend(_CACHED_USERNAMES)
    seen = set()
    result = []
    for name in names:
        key = name.lower()
        if key and key not in seen:
            seen.add(key)
            result.append(name)
    if result:
        _CACHED_USERNAME = result[0]
        _CACHED_USERNAMES = list(result)
    return result


def _is_our_name(candidate):
    """True если candidate — наше имя (или имя неизвестно → считаем своим)."""
    if not candidate:
        return True
    ours = _our_discord_names()
    if not ours:
        return True
    needle = candidate.strip().lower()
    # точное совпадение или последнее слово (на случай хвостов)
    last = needle.split()[-1] if needle.split() else needle
    return any(
        needle == n.lower() or last == n.lower()
        for n in ours
    )


def _parse_amount(raw):
    """'2,241' / '2241' / '110' → int."""
    if raw is None:
        return 0
    digits = re.sub(r'[^\d]', '', str(raw))
    return int(digits) if digits else 0


def _normalize_mudae_text(content):
    """Убирает markdown/эмодзи/(Free), сохраняя underscores в никах (l_i_m_i_n_a_l)."""
    text = content or ''
    # Типографские апострофы → ASCII (can’t / can't)
    text = text.replace('\u2019', "'").replace('\u2018', "'").replace('\u02bc', "'")
    # **bold** / __underline__ / ~~strike~~ — без вырезания одиночных _
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'~~(.+?)~~', r'\1', text)
    text = re.sub(r'`+', '', text)
    # одиночные *italic* (не трогаем _)
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'\1', text)
    text = _DISCORD_EMOJI_RE.sub('', text)
    text = re.sub(r'\(\s*Free\s*\)', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()


def _account_for_chat_name(name):
    """Аккаунт роя по нику из ответа Mudae. None — это чужой ник."""
    if name:
        found = Accounts.by_name(name)
        if found:
            return found
        if not _is_our_name(name):
            return None
    return _active_account()


def handle_mudae_chat(content, message_id=None):
    """Реактивный парсер ответов Mudae: КД какеры, начисления, свадьбы.

    Возвращает dict с распознанным событием или None.
    """
    global _PENDING_CLAIM

    if not content:
        return None

    parsed = _normalize_mudae_text(content)
    lower = parsed.lower()

    def _once(event_key):
        if message_id is None:
            return True
        return _remember_message_id(f'chat:{message_id}:{event_key}')

    # --- A: отказ / кулдаун какеры ---
    cd_match = _KAKERA_CD_RE.search(parsed)
    if cd_match:
        cd_name = (cd_match.group('name') or '').strip()
        target = _account_for_chat_name(cd_name)
        if target is None:
            return None
        if not _once('kakera_cd'):
            return None
        hours = int(cd_match.group('hours') or 0)
        mins = int(cd_match.group('mins') or 0)
        minutes = hours * 60 + mins
        if minutes <= 0:
            # Фолбэк: если время не распарсилось, но есть ($ku) — ставим 60 мин
            if '$ku' in lower:
                minutes = 60
            else:
                return None
        _set_kakera_cooldown(time.time() + minutes * 60, target)
        log_msg(
            'KAKER',
            f"Какера в откате на {minutes} мин ({target.label}) "
            f"(до {time.strftime('%H:%M:%S', time.localtime(target.kakera_cooldown_until))}).",
        )
        with _use_account(target):
            try_use_dk_for_kakera_cd()
        return {'type': 'kakera_cooldown', 'minutes': minutes}

    if _ROLLS_INTERVAL_BLOCK_RE.search(parsed):
        if not _once('rolls_interval'):
            return None
        return _parse_rolls_interval_block(parsed)

    # Явный маркер ($ku) без стандартной фразы — всё равно ставим КД
    if '$ku' in lower and 'kakera' in lower:
        target = _active_account()
        if not _once('kakera_cd'):
            return None
        minutes = 60
        time_bits = re.search(
            r'(?:(\d+)\s*h\s*)?(?:(\d+)\s*min)', lower
        )
        if time_bits:
            minutes = int(time_bits.group(1) or 0) * 60 + int(time_bits.group(2) or 0)
        if minutes > 0:
            _set_kakera_cooldown(time.time() + minutes * 60, target)
            log_msg(
                'KAKER',
                f"Какера в откате на {minutes} мин ($ku, {target.label}) "
                f"(до {time.strftime('%H:%M:%S', time.localtime(target.kakera_cooldown_until))}).",
            )
            with _use_account(target):
                try_use_dk_for_kakera_cd()
            return {'type': 'kakera_cooldown', 'minutes': minutes}

    # --- C: успешный клейм (свадьба) — только НАША ---
    if _MARRIAGE_RE.search(parsed):
        parties = _parse_marriage_parties(parsed)
        if not parties:
            return None
        claimer = parties['claimer']
        character = parties['character']
        # Чужие свадьбы в канале игнорируем
        if not _is_our_name(claimer):
            # На случай редкого порядка «Character and claimer»
            if _is_our_name(character):
                claimer, character = character, claimer
            else:
                return None
        if not _once('marriage'):
            return None

        global _LAST_OUR_MARRIAGE
        _LAST_OUR_MARRIAGE = {'at': time.time(), 'name': character}

        claimer_account = Accounts.by_name(claimer) or _active_account()
        claimer_account.profile['can_claim'] = False
        sync_profile_state(claimer_account.profile)
        if _ROLL_SESSION.get('active'):
            _finish_claim_success(claimer_account, None)
        else:
            WebDashboard.emit_buffer_clear()

        amounts = [_parse_amount(x) for x in re.findall(r'\+(\d{1,3}(?:,\d{3})+|\d+)', parsed)]
        kakera_amount = amounts[0] if amounts else 0
        ouros_amount = amounts[1] if len(amounts) > 1 else 0

        series, power, reason = '', 0, 'marriage'
        pending = _PENDING_CLAIM
        if pending and (time.time() - float(pending.get('at') or 0)) < 180:
            pending_name = (pending.get('name') or '').strip()
            if (
                not pending_name
                or pending_name == '???'
                or pending_name.lower() == character.lower()
            ):
                series = pending.get('series') or ''
                power = int(pending.get('power') or 0)
                reason = pending.get('reason') or 'marriage'
        _PENDING_CLAIM = None

        WebDashboard.update_stats(
            inc_kakera=kakera_amount,
            inc_claims=1,
            inc_ourospheres=ouros_amount,
        )
        WebDashboard.record_claim({
            'name': character,
            'series': series,
            'power': power,
            'kakera': kakera_amount,
            'ourospheres': ouros_amount,
            'reason': reason,
            'ts': time.time(),
        })
        log_msg(
            'CLAIM',
            f"Свадьба! {character} · +{kakera_amount} kakera, +{ouros_amount} ourosphere "
            f"(всего k={WebDashboard.BOT_STATS['kakera_earned']}, "
            f"o={WebDashboard.BOT_STATS.get('ourospheres_earned', 0)}).",
        )
        return {
            'type': 'marriage',
            'kakera': kakera_amount,
            'ourospheres': ouros_amount,
            'name': character,
            'claimer': claimer,
        }

    # --- B: успех какеры ("Name +2,241 ($k)" / "(Free) Name +110 ($k)") ---
    # Берём последнее совпадение — ближе к хвосту сообщения после breaks down / эмодзи
    gain_matches = list(_KAKERA_GAIN_RE.finditer(parsed))
    if gain_matches:
        gain = gain_matches[-1]
        name = gain.group('name').strip()
        amount = _parse_amount(gain.group('amount'))
        if amount > 0:
            if _is_our_name(name):
                if not _once('kakera_gain'):
                    return None
                WebDashboard.update_stats(inc_kakera=amount)
                log_msg(
                    'KAKER',
                    f"{name} +{amount} ($k) — всего {WebDashboard.BOT_STATS['kakera_earned']}",
                )
                return {'type': 'kakera_gain', 'amount': amount, 'name': name}
            # Чужое имя (напр. l_i_m_i_n_a_l) — не засчитываем и не идём в анонимный фолбэк
            return None

    looks_like_kakera = bool(
        re.search(r'dark\s*kakera|\bkakera\b|\(\s*\$?k\s*\)|\$k(?!u)', lower)
    )
    if looks_like_kakera:
        # Анонимный "+N ($k)" только если в тексте нет чужого "Name +N ($k)"
        plus_matches = list(_KAKERA_PLUS_RE.finditer(parsed))
        if plus_matches:
            amount = _parse_amount(plus_matches[-1].group('amount'))
            if amount > 0:
                if not _once('kakera_gain'):
                    return None
                WebDashboard.update_stats(inc_kakera=amount)
                log_msg(
                    'KAKER',
                    f"+{amount} kakera — всего {WebDashboard.BOT_STATS['kakera_earned']}",
                )
                return {'type': 'kakera_gain', 'amount': amount}

    return None


def poll_mudae_chat_replies(after_message_id, *, channel_id=None, attempts=4, delay=0.55):
    """После клика какеры — REST-поллинг ответов Mudae (+N $k / КД $ku).

    Нужен потому что Gateway часто выключен (snipeEnabled=False) или
    не подписан на нужный канал большого сервера.
    """
    if not after_message_id:
        return []

    ch = channel_id or Vars.channelId
    poll_url = f'https://discord.com/api/v9/channels/{ch}/messages'
    results = []
    last_id = str(after_message_id)
    seen_ids = set()

    for _ in range(attempts):
        human_sleep(delay)
        try:
            response = requests.get(
                f'{poll_url}?after={last_id}&limit=10',
                headers=auth,
                timeout=8,
            )
        except requests.RequestException:
            continue

        if response.status_code == 429:
            wait_time = float(response.json().get('retry_after', 1.0))
            time.sleep(wait_time)
            continue
        if response.status_code != 200:
            continue

        messages = response.json()
        if not messages:
            if results:
                break
            continue

        # Discord отдаёт новые сверху — обрабатываем от старых к новым
        ordered = sorted(messages, key=lambda m: int(m.get('id', 0)))
        for msg in ordered:
            mid = str(msg.get('id', ''))
            if mid:
                last_id = mid
            if mid in seen_ids:
                continue
            if str((msg.get('author') or {}).get('id', '')) != str(botID):
                continue
            seen_ids.add(mid)

            content = msg.get('content') or ''
            if not content and msg.get('embeds'):
                content = (msg['embeds'][0].get('description') or '')

            event = handle_mudae_chat(content, message_id=mid)
            if event:
                results.append(event)

        if any(ev.get('type') == 'kakera_cooldown' for ev in results):
            break
        # Успех уже поймали — можно не ждать лишние попытки
        if any(ev.get('type') == 'kakera_gain' for ev in results):
            break

    return results


def parse_kakera_gain(content, message_id=None):
    """Обратная совместимость: делегирует в handle_mudae_chat."""
    result = handle_mudae_chat(content, message_id=message_id)
    if result and result.get('type') == 'kakera_gain':
        return result.get('amount')
    return None


def log_msg(tag, message=None, *, dim=False, **extra):
    """Отправляет лог-событие в веб-дашборд через SocketIO.

    Старые однопараметрические вызовы сохраняются: для них тег и приглушение
    подбираются по тексту.
    """
    if message is None:
        message = tag
        text = str(message).lower()
        if any(word in text for word in ('ошибка', 'неудача', 'лимит', 'не дождался', 'не ответила', 'притормозить')):
            tag = 'WARN'
        elif any(word in text for word in ('какера', 'kakera')):
            tag = 'KAKER'
        elif any(word in text for word in ('клейм', '$rt')):
            tag = 'CLAIM'
        elif any(word in text for word in ('прокрут', 'ролл', 'pokeslot')):
            tag = 'ROLL'
        else:
            tag = 'SYS'
        dim = dim or any(word in text for word in ('отправляю', 'запрашиваю', 'пытаюсь', 'переход'))

    current_time = time.strftime("%H:%M:%S", time.localtime())
    payload = {
        'tag': tag,
        'message': str(message),
        'time': current_time,
        'dim': bool(dim),
    }
    payload.update(extra)

    try:
        WebDashboard.emit_log(payload)
    except Exception:
        print(f"[{current_time}] [{tag}] {message}")


def _profile_log(state):
    claim_status = 'ДОСТУПЕН' if state['can_claim'] else 'В ОТКАТЕ'

    def _util_status(ready):
        if ready is True:
            return 'ДОСТУПЕН'
        if ready is False:
            return 'В ОТКАТЕ'
        return 'НЕИЗВЕСТНО'

    thr = state.get('current_threshold')
    thr_part = f", порог клейма: {thr:g}" if thr is not None else ''
    if state.get('kakera_power_full'):
        power_part = ', сила реакции восстановлена'
    elif state.get('kakera_power') is not None and int(state.get('kakera_cost') or 0):
        power_part = f", сила {int(state['kakera_power'])}%"
    else:
        power_part = ''
    log_msg(
        'SYS',
        f"Профиль: Клейм {claim_status}, $rt {_util_status(state.get('rt_ready'))}, "
        f"$daily {_util_status(state.get('daily_ready'))}, "
        f"$dk {_util_status(state.get('dk_ready'))}{power_part}, "
        f"Роллы: {state['rolls_left']} (сброс через {state.get('rolls_reset_minutes', 0)} мин), "
        f"Сбросы: {state['rolls_resets']}{thr_part}",
    )


_CARD_POWER_BOLD_RE = re.compile(r'\*\*([\d,]+)\*\*')
# Цена персонажа: «24,290:kakera» без плюса. «+19,350:kakera:» — бонус ключей/$bku.
_CARD_POWER_KAKERA_RE = re.compile(
    rf'(?<![\d+,]){_AMOUNT}\s*:kakera:?',
    re.IGNORECASE,
)
_KEY_KAKERA_BONUS_RE = re.compile(
    rf'\+(?P<bonus>\d{{1,3}}(?:,\d{{3}})+|\d+)\s*:kakera:?',
    re.IGNORECASE,
)
_WISH_LINE_RE = re.compile(r'wished\s+by', re.IGNORECASE)
_CLAIMS_LIKES_RE = re.compile(r'^(claims|likes)\s*:', re.IGNORECASE)
_MENTION_LINE_RE = re.compile(r'^(?:<@!?\d+>\s*,?\s*)+$')
_WISHPROTECT_RE = re.compile(r':wishprotect:', re.IGNORECASE)


def _names_match(a, b):
    return (a or '').strip().casefold() == (b or '').strip().casefold()


def _clean_series_name(raw):
    """Убирает markdown, Discord-эмодзи и хвост :wishprotect: у серии."""
    text = (raw or '').strip('* ').strip()
    text = _DISCORD_EMOJI_RE.sub('', text)
    text = _WISHPROTECT_RE.sub('', text)
    text = re.sub(r':\w+:\s*$', '', text)
    return text.strip(' :·-')


def _wish_series_from_desc(desc, card_name):
    """Серия из wish-embed: пропускает Wished by, имя персонажа, Claims/Likes, kakera."""
    for line in desc.splitlines():
        line = line.strip()
        if not line:
            continue
        if _WISH_LINE_RE.search(line):
            continue
        if _CLAIMS_LIKES_RE.match(line):
            continue
        if _CARD_POWER_KAKERA_RE.search(line) or _KEY_KAKERA_BONUS_RE.search(line):
            continue
        if '$bku' in line.lower() or 'chaoskey' in line.lower():
            continue
        if _MENTION_LINE_RE.match(line):
            continue
        bold = re.fullmatch(r'\*\*(.+?)\*\*', line)
        series_candidate = bold.group(1).strip() if bold else line.strip('* ')
        if not series_candidate or re.fullmatch(r'[\d,]+', series_candidate):
            continue
        if _names_match(series_candidate, card_name):
            continue
        cleaned = _clean_series_name(series_candidate)
        if cleaned:
            return cleaned
    return 'Неизвестно'


def parse_card_embed(embed):
    """Парсит embed Mudae: имя, серия, цена (kakera value), заклеймлена ли."""
    card_name = (embed.get('author') or {}).get('name') or 'Неизвестно'
    desc = (embed.get('description') or '').strip()
    footer_icon = (embed.get('footer') or {}).get('icon_url') or ''
    is_claimed = bool(footer_icon)

    if not desc:
        return card_name, 'Неизвестно', 0, is_claimed

    card_power = 0
    k_matches = list(_CARD_POWER_KAKERA_RE.finditer(desc))
    if k_matches:
        card_power = _parse_amount(k_matches[-1].group('amount'))
    if not card_power:
        for match in _CARD_POWER_BOLD_RE.finditer(desc):
            card_power = _parse_amount(match.group(1))
            if card_power:
                break

    card_series = 'Неизвестно'
    if _WISH_LINE_RE.search(desc):
        card_series = _wish_series_from_desc(desc, card_name)
    else:
        lines = [ln.strip() for ln in desc.splitlines() if ln.strip()]
        if lines:
            first = lines[0]
            bold = re.fullmatch(r'\*\*(.+?)\*\*', first)
            if bold:
                inner = bold.group(1).strip()
                if not re.fullmatch(r'[\d,]+', inner):
                    card_series = _clean_series_name(inner)
            elif not re.fullmatch(r'[\d,]+', first):
                card_series = _clean_series_name(first)

        if card_series == 'Неизвестно':
            for match in re.finditer(r'\*\*(.+?)\*\*', desc):
                inner = match.group(1).strip()
                if re.fullmatch(r'[\d,]+', inner):
                    continue
                card_series = _clean_series_name(inner)
                break

    return card_name, card_series, card_power, is_claimed


def parse_key_kakera_bonus(embed):
    """Сумма с ключей / $bku: строки вида «+19,350:kakera:», не цена карты."""
    desc = (embed or {}).get('description') or ''
    total = 0
    for match in _KEY_KAKERA_BONUS_RE.finditer(desc):
        total += _parse_amount(match.group('bonus'))
    return total


def apply_key_kakera_bonus(embed, message_id=None):
    """Добавляет бонус ключей в счётчик kakera_earned (один раз на сообщение)."""
    amount = parse_key_kakera_bonus(embed)
    if amount <= 0:
        return 0
    if message_id is not None and not _remember_message_id(f'keykakera:{message_id}'):
        return 0
    WebDashboard.update_stats(inc_kakera=amount)
    log_msg(
        'KAKER',
        f"Ключи / $bku: +{amount} kakera — всего "
        f"{WebDashboard.BOT_STATS['kakera_earned']}",
    )
    return amount


def _drop_log(card_name, card_series, card_power, is_claimed, message_id=None):
    """Лог дропа: цена персонажа. Дедуп по message_id."""
    if message_id is not None and not _remember_message_id(f'drop:{message_id}'):
        return False
    status = 'Заклеймена' if is_claimed else 'Свободна'
    log_msg(
        'DROP',
        f"Цена {card_power} | {card_name} ({card_series}) | {status}",
        power=card_power,
        name=card_name,
        series=card_series,
        status=status,
    )
    return True

def _get_latest_message_id():
    """Возвращает ID последнего сообщения в канале перед отправкой команды."""
    try:
        response = requests.get(f"{url}?limit=1", headers=auth, timeout=10)
    except requests.RequestException as error:
        log_msg('WARN', f"Не удалось получить последнее сообщение: {error}")
        return 0
    if response.status_code == 200 and response.json():
        return int(response.json()[0]['id'])
    return 0

def _wait_for_mudae_reply(after_message_id, attempts=6):
    """Ожидает первое новое текстовое сообщение от Mudae."""
    for _ in range(attempts):
        human_sleep(0.7, 1.2)
        try:
            response = requests.get(
                f"{url}?after={after_message_id}&limit=10", headers=auth, timeout=10
            )
        except requests.RequestException as error:
            log_msg('WARN', f"Ошибка ожидания ответа Mudae: {error}")
            continue

        if response.status_code == 200:
            messages = response.json()
            mudae_message = next(
                (message for message in messages
                 if message.get('author', {}).get('id') == botID
                 and message.get('content')),
                None,
            )
            if mudae_message:
                return mudae_message
        elif response.status_code == 429:
            wait_time = response.json().get('retry_after', 1.0)
            log_msg('WARN', f"Discord просит притормозить. Ждем {wait_time} сек...")
            time.sleep(wait_time)

    return None

def post_text_message(command, account=None):
    """Пишет текст в канал от аккаунта и возвращает ответ Discord (или None)."""
    account = account or _active_account()
    action_pause()
    try:
        return requests.post(
            url=url, headers=account.auth, json={'content': command}, timeout=10
        )
    except requests.RequestException as error:
        log_msg('WARN', f"Не удалось отправить {command}: {error}")
        return None


def _send_text_command(command, account=None):
    """Отправляет обычную текстовую команду Mudae и возвращает код ответа."""
    response = post_text_message(command, account)
    if response is None:
        return 0
    if response.status_code not in [200, 204]:
        log_msg('WARN',
            f"Не удалось отправить {command} "
            f"(код: {response.status_code})"
        )
    return response.status_code

def _parse_cmd_cooldown(parsed_text, cmd):
    """Статус утилиты Mudae: (ready: bool|None, minutes_left: int).

    ready=None — в тексте нет явного статуса (UI покажет «—»).
    Сначала ищем «available/ready», потом кулдауны — иначе ложные [КД].
    """
    cmd = re.escape(cmd)
    # $rt is available! / $rt is ready! / rt is available
    if re.search(
        rf'(?:\$)?{cmd}\s+is\s+(?:available|ready)\b',
        parsed_text,
        re.IGNORECASE,
    ):
        return True, 0
    if re.search(
        rf'(?:\$)?{cmd}\s+(?:available|ready)\s*!',
        parsed_text,
        re.IGNORECASE,
    ):
        return True, 0

    cooldown_patterns = (
        rf'(?:\$)?{cmd}\s+is\s+ready\s+in\s+(?:(\d+)\s*h\s+)?(\d+)\s+min',
        rf'(?:\$)?{cmd}\s+ready\s+in\s+(?:(\d+)\s*h\s+)?(\d+)\s+min',
        rf'(?:\$)?{cmd}\s+resets?\s+in\s+(?:(\d+)\s*h\s+)?(\d+)\s+min',
        rf'(?:the\s+)?next\s+(?:\$)?{cmd}\s+(?:reset\s+)?in\s+(?:(\d+)\s*h\s+)?(\d+)\s+min',
        rf'(?:the\s+)?(?:\$)?{cmd}\s+is\s+ready\s+in\s+(?:(\d+)\s*h\s+)?(\d+)\s+min',
        rf'you\s+can\'?t\s+use\s+(?:\$)?{cmd}\s+for\s+(?:(\d+)\s*h\s+)?(\d+)\s+min',
        # The cooldown of $rt is not over. Time left: 2h 05 min. ($rtu)
        rf'(?:the\s+)?cooldown\s+of\s+(?:\$)?{cmd}\b[\s\S]{{0,120}}?time\s+left:\s*(?:(\d+)\s*h\s+)?(\d+)\s+min',
        rf'time\s+left:\s*(?:(\d+)\s*h\s+)?(\d+)\s+min\.?\s*\(\$?{cmd}u\)',
    )
    for pattern in cooldown_patterns:
        match = re.search(pattern, parsed_text, re.IGNORECASE)
        if match:
            minutes = int(match.group(1) or 0) * 60 + int(match.group(2))
            return False, minutes

    # Кулдаун упомянут, но время не распарсили — всё равно «в откате», не «неизвестно»
    if re.search(
        rf'(?:the\s+)?cooldown\s+of\s+(?:\$)?{cmd}\b',
        parsed_text,
        re.IGNORECASE,
    ):
        return False, 0

    return None, 0


def compute_claim_threshold(claim_minutes_left):
    """Динамический порог цены персонажа для клейма."""
    base = float(getattr(Vars, 'min_power_threshold', 80))
    minutes = int(claim_minutes_left or 0)
    mid = int(getattr(Vars, 'hunger_minutes_mid', 60))
    high = int(getattr(Vars, 'hunger_minutes_high', 120))
    mult_mid = float(getattr(Vars, 'hunger_multiplier', 1.5))
    mult_high = float(getattr(Vars, 'hunger_multiplier_high', 2.5))
    if minutes > high:
        return base * mult_high
    if minutes > mid:
        return base * mult_mid
    return base


def get_claim_ttl():
    """TTL активного буфера в секундах (Vars.claim_ttl, по умолчанию 90)."""
    try:
        return max(1, int(getattr(Vars, 'claim_ttl', 90)))
    except (TypeError, ValueError):
        return 90


def sync_profile_state(state):
    """Пишет состояние в профиль того аккаунта, чей это $tu, и обновляет панель."""
    if not state:
        return PROFILE_STATE
    account = Accounts.by_key(state.get('account_key')) or _active_account()
    keys = (
        'can_claim', 'rt_ready', 'claim_minutes_left', 'rolls_left', 'rolls_resets',
        'rolls_reset_minutes', 'kakera_power', 'kakera_cost', 'kakera_cost_keys',
        'kakera_power_full', 'kakera_ready',
        'current_threshold', 'rt_minutes_left', 'daily_ready', 'daily_minutes_left',
        'dk_ready', 'dk_minutes_left', 'claim_ttl',
    )
    target = account.profile
    for key in keys:
        if key in state:
            target[key] = state[key]
    target['account_key'] = account.key
    if 'current_threshold' not in state:
        target['current_threshold'] = compute_claim_threshold(
            target.get('claim_minutes_left', 0)
        )
    WebDashboard.emit_profile_update(dict(Accounts.MAIN.profile))
    WebDashboard.emit_swarm_accounts()
    return target


def _rolls_worth_spinning(profile_state, account=None):
    """Есть смысл тратить $rolls: клейм доступен или реакция какеры не в КД."""
    if profile_state.get('can_claim'):
        return True
    account = account or Accounts.by_key(profile_state.get('account_key')) or _active_account()
    return not kakera_on_cooldown(account)


def _try_use_rolls(profile_state, *, context=''):
    """Отправляет $rolls один раз за интервал, если есть сбросы и смысл крутить."""
    account = Accounts.by_key((profile_state or {}).get('account_key')) or _active_account()
    if not getattr(Vars, 'rollsEnabled', True):
        suffix = f" ({context})" if context else ''
        log_msg('ROLL', f"$rolls выключен в Vars.rollsEnabled{suffix}.", dim=True)
        return False

    rolls_resets = int(profile_state.get('rolls_resets', 0) or 0)
    if rolls_resets <= 0:
        return False

    if rolls_reset_on_cooldown(account):
        left_min = max(1, (rolls_reset_cooldown_left_seconds(account) + 59) // 60)
        suffix = f" ({context})" if context else ''
        log_msg(
            'ROLL',
            f"$rolls пропущен{suffix}: уже использован в этом интервале "
            f"(ещё {left_min} мин).",
            dim=True,
        )
        return False

    if not _rolls_worth_spinning(profile_state, account):
        suffix = f" ({context})" if context else ''
        log_msg(
            'ROLL',
            f"$rolls пропущен{suffix}: "
            f"клейм={'да' if profile_state.get('can_claim') else 'нет'}, "
            f"какера={'готова' if not kakera_on_cooldown(account) else 'в КД'} "
            f"(таймер КД {kakera_cooldown_left_seconds(account)} сек).",
            dim=True,
        )
        return False

    suffix = f" — {context}" if context else ''
    log_msg('ROLL', f"Использую $rolls{suffix} ({account.label}).")
    last_message_id = _get_latest_message_id()
    if _send_text_command('$rolls', account) not in [200, 204]:
        return False

    mudae_message = _wait_for_mudae_reply(last_message_id)
    if mudae_message:
        with _use_account(account):
            handle_mudae_chat(
                mudae_message.get('content', ''),
                message_id=mudae_message.get('id'),
            )
        if rolls_reset_on_cooldown(account):
            return False

    reset_in = int(profile_state.get('rolls_reset_minutes', 0) or 0)
    if reset_in <= 0:
        reset_in = _DEFAULT_ROLLS_INTERVAL_MINUTES
    _set_rolls_reset_cooldown(reset_in, reason='успешный сброс', account=account)

    profile_state['rolls_reset_used'] = True
    profile_state['rolls_resets'] = rolls_resets - 1
    profile_state['rolls_left'] = max(
        1, int(getattr(Vars, 'max_rolls_per_session', 150))
    )
    sync_profile_state(profile_state)
    human_sleep(1.0, 2.0)
    return True


def updateProfileState(_refreshed_after_dk=False, skip_actions=False, account=None):
    """Запрашивает $tu, обновляет состояние профиля и запускает доступные действия."""
    account = account or _active_account()
    last_message_id = _get_latest_message_id()
    log_msg('SYS', f"Запрашиваю состояние профиля ($tu, {account.label})...", dim=True)
    if _send_text_command('$tu', account) not in [200, 204]:
        return None

    mudae_message = _wait_for_mudae_reply(last_message_id)
    if not mudae_message:
        log_msg('WARN', "Не дождался ответа Mudae на $tu.")
        return None

    profile_text = mudae_message.get('content', '')
    # Нормализация: markdown + эмодзи, underscores в никах сохраняем
    parsed_text = _normalize_mudae_text(profile_text)
    lower_text = parsed_text.lower()

    def extract_int(pattern, default=0):
        match = re.search(pattern, parsed_text, re.IGNORECASE)
        if not match:
            return default
        digits = re.sub(r'[^0-9]', '', match.group(1))
        return int(digits) if digits else default

    claim_match = re.search(
        r"you can't claim for another\s+(?:(\d+)\s*h\s+)?(\d+)\s+min",
        parsed_text,
        re.IGNORECASE,
    )
    if not claim_match:
        # Когда клейм уже доступен, $tu сообщает время до следующего сброса
        # отдельной фразой: "The next claim reset is in 1h 57 min".
        claim_match = re.search(
            r"next claim reset is in\s+(?:(\d+)\s*h\s+)?(\d+)\s+min",
            parsed_text,
            re.IGNORECASE,
        )
    claim_minutes_left = (
        int(claim_match.group(1) or 0) * 60 + int(claim_match.group(2))
        if claim_match else 0
    )

    rolls_reset_match = re.search(
        r"Next rolls reset in\s+(?:(\d+)\s*h\s+)?(\d+)\s+min",
        parsed_text,
        re.IGNORECASE,
    )
    rolls_reset_minutes = (
        int(rolls_reset_match.group(1) or 0) * 60 + int(rolls_reset_match.group(2))
        if rolls_reset_match else 0
    )

    rt_ready, rt_minutes = _parse_cmd_cooldown(parsed_text, 'rt')
    daily_ready, daily_minutes = _parse_cmd_cooldown(parsed_text, 'daily')
    dk_ready, dk_minutes = _parse_cmd_cooldown(parsed_text, 'dk')

    # КД реакции из $tu. Сразу после $dk старая фраза «can't react» таймер не возвращает.
    prior = account.profile
    kakera_power = extract_int(r"Power:\s*(\d+)%")
    kakera_cost = extract_int(r"consumes\s+(\d+)%\s+of your reaction power")
    half_match = re.search(
        r"10\+\s*keys consume half the power \((\d+)%\)",
        parsed_text,
        re.IGNORECASE,
    )
    kakera_cost_keys = int(half_match.group(1)) if half_match else 0
    kakera_cd = re.search(
        r"you can't react to kakera for\s+(?:(\d+)\s*h\s*)?(\d+)\s*min",
        parsed_text,
        re.IGNORECASE,
    )
    kakera_ready_tu = bool(
        re.search(r"you\s+can\s+react\s+to\s+kakera\b", lower_text, re.IGNORECASE)
    )
    power_full = False
    if _refreshed_after_dk:
        full_cost = kakera_cost or int(prior.get('kakera_cost') or 0)
        restored = kakera_ready_tu or (full_cost > 0 and kakera_power >= full_cost)
        _set_kakera_cooldown(0.0, account)
        kakera_ready = True
        if restored:
            power_full = False
        else:
            kakera_power = None
            power_full = True
            if not kakera_cost:
                kakera_cost = int(prior.get('kakera_cost') or 0)
            if not kakera_cost_keys:
                kakera_cost_keys = int(prior.get('kakera_cost_keys') or 0)
    elif kakera_ready_tu:
        _set_kakera_cooldown(0.0, account)
        kakera_ready = True
    elif kakera_cd:
        minutes = int(kakera_cd.group(1) or 0) * 60 + int(kakera_cd.group(2) or 0)
        if minutes > 0:
            _set_kakera_cooldown(time.time() + minutes * 60, account)
        kakera_ready = False
    elif "can't react to kakera" in lower_text:
        kakera_ready = False
    else:
        kakera_ready = not kakera_on_cooldown(account)
        if kakera_ready:
            _set_kakera_cooldown(0.0, account)

    state = {
        'account_key': account.key,
        'can_claim': "you can't claim" not in lower_text,
        'claim_minutes_left': claim_minutes_left,
        'rolls_reset_minutes': rolls_reset_minutes,
        'rolls_left': extract_int(r"You have\s+(\d+)\s+rolls left"),
        'rolls_resets': extract_int(r"You have\s+(\d+)\s+rolls reset in stock"),
        'rt_ready': rt_ready,
        'rt_minutes_left': rt_minutes,
        'daily_ready': daily_ready,
        'daily_minutes_left': daily_minutes,
        'dk_ready': dk_ready,
        'dk_minutes_left': dk_minutes,
        'kakera_power': kakera_power,
        'kakera_cost': kakera_cost,
        'kakera_cost_keys': kakera_cost_keys,
        'kakera_power_full': power_full,
        'kakera_ready': kakera_ready,
        'current_threshold': compute_claim_threshold(claim_minutes_left),
        'claim_ttl': get_claim_ttl(),
    }
    _profile_log(state)
    sync_profile_state(state)

    if skip_actions:
        return state

    if state['daily_ready'] is True:
        log_msg('SYS', "$daily доступен. Отправляю команду.", dim=True)
        if _send_text_command('$daily', account) in [200, 204]:
            log_msg('SYS', '🎁 Ежедневная награда ($daily) успешно забрана!')

    # $dk полностью восстанавливает силу. Жать только когда он готов и реакции нет.
    if (
        state['dk_ready'] is True
        and reaction_energy_missing(state)
        and not _refreshed_after_dk
    ):
        if _send_text_command('$dk', account) in (200, 204):
            note_kakera_restored_by_dk(account)
            log_msg('KAKER', f"$dk отправлен ({account.label}). Сила реакции восстановлена.")
            return updateProfileState(_refreshed_after_dk=True, account=account)

    # $rolls: есть смысл крутить, если доступен клейм ИЛИ реакция какеры ($tu)
    if state['rolls_left'] == 0 and state['rolls_resets'] > 0:
        _try_use_rolls(state, context='профиль $tu')

    return state

_RT_FAIL_RE = re.compile(
    r"cooldown of\s+\$?rt\b|can'?t use\s+\$?rt\b|\$rt\s+is\s+ready\s+in\b",
    re.IGNORECASE,
)
_RT_OK_RE = re.compile(
    r'claim (?:has been |was )?reset|reset(?:ting)? (?:your |the )?claim|'
    r'you can claim right now|can now claim',
    re.IGNORECASE,
)
_CANT_CLAIM_RE = re.compile(r"you can'?t claim\b", re.IGNORECASE)
_CLAIM_CLICKS = {}


def _ensure_claim_ready(profile_state, allow_rt=False, channel_id=None, message_id=None):
    """Проверяет доступность клейма.

    $rt тратим только на вишлист и джекпот. После команды ждём, пока Mudae
    реально откроет клейм: клик в ту же секунду она отклоняет, а Discord
    при этом уже ответил 204.
    """
    if profile_state.get('can_claim', False):
        return True

    if not allow_rt:
        log_msg('WARN', "Клейм в откате. $rt бережём для вишлиста и джекпота — реакцию не отправляю.")
        return False

    if profile_state.get('rt_ready') is not True:
        log_msg('WARN', "Вишлист/джекпот, но клейм в откате и $rt недоступен. Реакция не отправлена.")
        return False

    if _rt_would_reclaim_ours(message_id, channel_id):
        log_msg('CLAIM', "Этот персонаж уже заклеймлен. $rt не трачу.", dim=True)
        return False

    log_msg('CLAIM', "Клейм уже потрачен. $rt доступен — отправляю и жду сброс перед кнопкой.", dim=True)
    after_id = _get_latest_message_id()
    if _send_text_command('$rt', Accounts.by_key(profile_state.get('account_key')) or _active_account()) not in [200, 204]:
        return False

    # Команда ушла в Discord. Второй $rt в этой попытке не шлём.
    profile_state['rt_ready'] = False
    sync_profile_state(profile_state)
    applied = _wait_until_claim_reset(after_id, channel_id=channel_id)
    if not applied:
        profile_state['rt_ready'] = True
        profile_state['can_claim'] = False
        sync_profile_state(profile_state)
        log_msg('WARN', "Mudae не сбросила клейм после $rt. Кнопку не жму.")
        return False

    profile_state['can_claim'] = True
    sync_profile_state(profile_state)
    return True


def _fetch_new_mudae_texts(after_id, channel_id):
    """Новые текстовые сообщения Mudae после after_id: (id, raw, normalized)."""
    try:
        response = requests.get(
            f'https://discord.com/api/v9/channels/{channel_id}/messages'
            f'?after={after_id}&limit=10',
            headers=auth,
            timeout=8,
        )
    except requests.RequestException:
        return []
    if response.status_code != 200:
        return []

    found = []
    ordered = sorted(response.json() or [], key=lambda item: int(item.get('id') or 0))
    for msg in ordered:
        if str((msg.get('author') or {}).get('id', '')) != str(botID):
            continue
        mid = str(msg.get('id') or '')
        raw = msg.get('content') or ''
        if not raw and msg.get('embeds'):
            raw = (msg['embeds'][0].get('description') or '')
        found.append((mid, raw, _normalize_mudae_text(raw)))
    return found


def _wait_until_claim_reset(after_message_id, channel_id=None):
    """True, если после $rt можно жать клейм. False — Mudae явно отказала."""
    channel_id = channel_id or Vars.channelId
    started = time.monotonic()
    deadline = started + 4.5
    last_id = str(after_message_id or 0)
    while time.monotonic() < deadline:
        human_sleep(0.35, 0.55, kind='action', log=False)
        for mid, raw, text in _fetch_new_mudae_texts(last_id, channel_id):
            if mid:
                last_id = mid
            if raw:
                handle_mudae_chat(raw, message_id=mid)
            if text and _RT_FAIL_RE.search(text):
                return False
            if text and _RT_OK_RE.search(text):
                log_msg('CLAIM', "Mudae подтвердила сброс клейма после $rt.", dim=True)
                human_sleep(0.3, 0.5, kind='action', log=False)
                return True
        if time.monotonic() - started >= 2.0:
            log_msg('CLAIM', "Пауза после $rt прошла, жму кнопку клейма.", dim=True)
            return True
    return True


def remember_gateway_message(message):
    """Склеивает правку Discord с уже виденным сообщением.

    MESSAGE_UPDATE часто приходит без author и без старых embeds.
    Без склейки такой апдейт отбрасывается, и кнопка kakeraY не нажимается.
    """
    if not isinstance(message, dict):
        return message
    mid = str(message.get('id') or '')
    if not mid:
        return message
    previous = _MESSAGE_CACHE.get(mid)
    if previous:
        merged = dict(previous)
        for key, value in message.items():
            if value is None:
                continue
            merged[key] = value
        message = merged
    _MESSAGE_CACHE[mid] = message
    if len(_MESSAGE_CACHE) > 400:
        for old in list(_MESSAGE_CACHE)[:200]:
            _MESSAGE_CACHE.pop(old, None)
    return message


def _await_claim_button(message, message_id, channel_id, components, *, timeout=2.6):
    """Ждёт кнопку сердца. $rt до этого не отправляем."""
    msg = message if isinstance(message, dict) else None
    resolved_channel = channel_id or Vars.channelId
    if msg:
        resolved_channel = msg.get('channel_id') or resolved_channel
        message_id = msg.get('id') or message_id
        if components is None:
            components = msg.get('components')

    deadline = time.monotonic() + timeout
    while True:
        button = find_claim_button(components)
        if button and msg:
            return msg, button, resolved_channel, message_id
        if time.monotonic() >= deadline or not message_id:
            return msg, None, resolved_channel, message_id
        human_sleep(0.28, 0.45, kind='action', log=False)
        fetched = _fetch_channel_message(resolved_channel, message_id, quiet=True)
        if not fetched:
            continue
        msg = fetched
        components = fetched.get('components')
        resolved_channel = fetched.get('channel_id') or resolved_channel
        message_id = fetched.get('id') or message_id
        remember_gateway_message(fetched)


def _claim_click_count(message_id):
    mid = str(message_id or '')
    count = _CLAIM_CLICKS.get(mid, 0) + 1
    _CLAIM_CLICKS[mid] = count
    if len(_CLAIM_CLICKS) > 400:
        _CLAIM_CLICKS.clear()
        _CLAIM_CLICKS[mid] = count
    return count


def _footer_owner_name(message):
    embeds = (message or {}).get('embeds') or []
    if not embeds:
        return ''
    footer = ((embeds[0].get('footer') or {}).get('text') or '')
    match = re.search(r'belongs to\s+(.+)', footer, re.IGNORECASE)
    return match.group(1).strip() if match else ''


def _we_own_card(message):
    owner = _footer_owner_name(message)
    return bool(owner) and _is_our_name(owner)


def _refresh_card_message(channel_id, message_id):
    fetched = _fetch_channel_message(channel_id, message_id, quiet=True)
    if fetched:
        remember_gateway_message(fetched)
    return fetched


def _our_marriage_since(since):
    """Свадьба уже разобрана (часто Gateway-потоком), а кнопка сердца ещё на месте."""
    return float(_LAST_OUR_MARRIAGE.get('at') or 0) >= float(since or 0) - 0.5


def _poll_claim_outcome(after_id, channel_id, message_id, *, since=None):
    """'ok' — наш клейм прошёл. 'lost' — карту забрали не мы. 'rejected' — сердце ещё живо.

    Сердце после свадьбы часто остаётся на карте. Это не отказ.
    """
    started = time.monotonic()
    since = time.time() if since is None else since
    last_id = str(after_id or 0)
    while time.monotonic() - started < 2.4:
        human_sleep(0.35, 0.5, kind='action', log=False)
        if _our_marriage_since(since):
            return 'ok'
        for mid, raw, text in _fetch_new_mudae_texts(last_id, channel_id):
            if mid:
                last_id = mid
            if raw:
                event = handle_mudae_chat(raw, message_id=mid)
                if event and event.get('type') == 'marriage':
                    return 'ok'
            if text and _CANT_CLAIM_RE.search(text):
                return 'rejected'
        if _our_marriage_since(since):
            return 'ok'
        fetched = _refresh_card_message(channel_id, message_id)
        if fetched and _we_own_card(fetched):
            return 'ok'
        if fetched and _footer_owner_name(fetched) and not _we_own_card(fetched):
            return 'lost'
    if _our_marriage_since(since):
        return 'ok'
    fetched = _refresh_card_message(channel_id, message_id)
    if fetched and _we_own_card(fetched):
        return 'ok'
    if fetched and _footer_owner_name(fetched):
        return 'lost'
    return 'rejected'


_CLAIM_REACTION_EMOJI = "%F0%9F%A6%BF"  # 🦫 — старый fallback
_CLAIM_HINTS = (
    'heart', 'hearts', 'orange_heart', 'yellow_heart', 'green_heart',
    'blue_heart', 'purple_heart', 'black_heart', 'white_heart', 'brown_heart',
    'heartpulse', 'gift_heart', 'two_hearts', 'revolving_hearts',
    'sparkling_heart', 'cupid', 'love_letter',
    '❤', '❤️', '🤍', '💕', '💖', '💗', '💓', '💞', '💟', '🧡', '💛', '💚', '💙', '💜',
    'claim', 'marry', 'wedding', 'bride', 'groom',
)


def _iter_message_buttons(components):
    for row in components or []:
        for button in row.get('components') or []:
            if button.get('type') == 2 and not button.get('disabled') and button.get('custom_id'):
                yield button


# Ценность какеры для сортировки кликов: сначала самые дорогие.
# Золотая — kakeraY. Имя «kakera» без суффикса тоже есть в Mudae, но берём его
# только если оно записано в Vars.desiredKakeras.
KAKERA_VALUES = {
    'kakeraW': 8,
    'kakeraL': 7,
    'kakeraD': 7,
    'kakeraC': 7,
    'kakeraR': 6,
    'kakeraO': 5,
    'kakeraY': 4,
    'kakeraG': 3,
    'kakeraT': 3,
    'kakeraB': 2,
    'kakeraP': 1,
    'kakera': 1,
}

# Ourosphere на карточке ролла (кнопка рядом с какерой). Имена — custom emoji Mudae.
OUROSPHERE_VALUES = {
    'sp': 150,
    'spP': 5,
    'spB': 10,
    'spT': 20,
    'spG': 35,
    'spY': 55,
    'spO': 90,
    'spW': 500,
    'spL': 200,
    'spD': 40,
}
_OUROSPHERE_VALUES_CF = {name.casefold(): value for name, value in OUROSPHERE_VALUES.items()}
_KAKERA_VALUES_CF = {name.casefold(): value for name, value in KAKERA_VALUES.items()}
_EMOJI_NAMES_BY_ID = {}


def _learn_emoji_shortcodes(text):
    for name, emoji_id in re.findall(r'<a?:(\w+):(\d+)>', text or ''):
        _EMOJI_NAMES_BY_ID[emoji_id] = name


def _log_unclicked_kakera(message):
    """Если на карте была kakera, но имя кнопки не совпало со списком — пишем это один раз."""
    mid = str((message or {}).get('id') or '')
    if not mid or not _remember_set(_KAKERA_CD_LOGGED, f'names:{mid}'):
        return
    names = []
    for button in _iter_message_buttons((message or {}).get('components')):
        emoji = button.get('emoji') or {}
        label = _button_emoji_name(button) or emoji.get('name') or ''
        if not label and emoji.get('id'):
            label = f"id:{emoji.get('id')}"
        if label:
            names.append(label)
    interesting = [name for name in names if 'kakera' in name.lower() or name.startswith('id:')]
    if not interesting:
        return
    log_msg(
        'KAKER',
        f"Кнопку какеры вижу, но не жму: {', '.join(interesting)}.",
        dim=True,
    )


def _button_emoji_name(button):
    emoji = button.get('emoji') or {}
    name = emoji.get('name') or ''
    emoji_id = str(emoji.get('id') or '')
    if name and emoji_id:
        _EMOJI_NAMES_BY_ID[emoji_id] = name
    if not name and emoji_id:
        name = _EMOJI_NAMES_BY_ID.get(emoji_id, '')
    return name


def _is_ourosphere_name(name):
    return (name or '').casefold() in _OUROSPHERE_VALUES_CF


def _kakera_click_names():
    """Какие какеры жать. Только Vars.desiredKakeras — пользователь сам выбирает."""
    configured = getattr(Vars, 'desiredKakeras', []) or []
    return {str(name).casefold() for name in configured}


def _is_kakera_button(button):
    name = _button_emoji_name(button)
    return name.casefold() in _kakera_click_names() or name.lower().startswith('kakera')


def _is_ourosphere_button(button):
    return _is_ourosphere_name(_button_emoji_name(button))


_KEY_COUNT_RE = re.compile(
    r'<\s*a?:chaoskey:\d+\s*>\s*\**\s*(\d{1,4})'
    r'|(\d{1,4})\s*\**\s*<\s*a?:chaoskey:\d+\s*>'
    r'|\bkeys?\s*[:=]?\s*(\d{1,4})\b'
    r'|\b(\d{1,4})\s+keys?\b',
    re.IGNORECASE,
)
_LOOT_DIRTY = set()


def _card_key_count(message):
    """Число ключей, если оно написано на карте. Иначе 0 — скидку не берём."""
    parts = [_message_text_blob(message or {})]
    for embed in (message or {}).get('embeds') or []:
        if not isinstance(embed, dict):
            continue
        for field in embed.get('fields') or []:
            if isinstance(field, dict):
                parts.append(str(field.get('name') or ''))
                parts.append(str(field.get('value') or ''))
    match = _KEY_COUNT_RE.search('\n'.join(parts))
    if not match:
        return 0
    for group in match.groups():
        if group:
            return int(group)
    return 0


def _kakera_button_cost(message, account=None):
    profile = (account or _active_account()).profile
    full = int(profile.get('kakera_cost') or 0)
    half = int(profile.get('kakera_cost_keys') or 0)
    if half and message is not None and _card_key_count(message) >= 10:
        return half
    return full


def _kakera_clicks_open(message=None, account=None):
    """Можно жать какеру: нет кулдауна и хватает Power. Сферы от этого не зависят."""
    account = account or _active_account()
    profile = account.profile
    if kakera_on_cooldown(account):
        return False
    if profile.get('kakera_power_full'):
        return True
    power = profile.get('kakera_power')
    if power is None:
        return True
    cost = _kakera_button_cost(message, account) if message is not None else int(
        profile.get('kakera_cost') or 0
    )
    if cost <= 0:
        return True
    try:
        return int(power) >= cost
    except (TypeError, ValueError):
        return True


def _spend_kakera_power(message, account=None):
    account = account or _active_account()
    profile = account.profile
    if profile.get('kakera_power_full'):
        return
    power = profile.get('kakera_power')
    if power is None:
        return
    cost = _kakera_button_cost(message, account)
    if cost <= 0:
        return
    left = max(0, int(power) - cost)
    profile['kakera_power'] = left
    if left < cost:
        profile['kakera_ready'] = False
    sync_profile_state(profile)


def _raw_loot_buttons(message):
    kakera_names = _kakera_click_names()
    found = []
    for button in _iter_message_buttons((message or {}).get('components')):
        emoji = _button_emoji_name(button)
        key = emoji.casefold()
        if key in _OUROSPHERE_VALUES_CF or key in kakera_names or emoji.lower().startswith('kakera'):
            found.append((emoji, button))
    return found


def _desired_kakera_buttons(message):
    names = _kakera_click_names()
    return [
        (emoji, button)
        for emoji, button in _raw_loot_buttons(message)
        if emoji.casefold() in names
    ]


def _raw_loot_signature(message):
    return tuple(
        f"{emoji}:{button.get('custom_id', '')}"
        for emoji, button in _raw_loot_buttons(message)
    )


def _join_name_counts(names):
    order = []
    counts = {}
    for name in names:
        if name not in counts:
            order.append(name)
            counts[name] = 0
        counts[name] += 1
    return ', '.join(f"{name} ×{counts[name]}" for name in order)


def _loot_shortfall_reason(message):
    account = _active_account()
    if kakera_on_cooldown(account):
        return f"КД {kakera_cooldown_left_seconds(account)} сек"
    if not _kakera_clicks_open(message, account):
        power = account.profile.get('kakera_power')
        cost = _kakera_button_cost(message, account)
        if power is None:
            return 'силы нет'
        return f"сила {int(power)}% < {int(cost)}%"
    return 'кнопка не дорисовалась'


def _log_loot_summary(message, clicked_kakera, clicked_spheres, seen, log_prefix):
    parts = []
    total = len(clicked_kakera)
    if seen or total:
        if seen > total:
            if len(set(clicked_kakera)) == 1:
                label = clicked_kakera[0]
            elif clicked_kakera:
                label = 'какера'
            else:
                visible = _desired_kakera_buttons(message)
                names = {name for name, _button in visible}
                label = visible[0][0] if len(names) == 1 else 'какера'
            parts.append(f"{label} {total}/{seen}, {_loot_shortfall_reason(message)}")
        elif clicked_kakera:
            parts.append(_join_name_counts(clicked_kakera))
    if clicked_spheres:
        parts.append(_join_name_counts(clicked_spheres))
    if not parts:
        return
    text = ', '.join(parts)
    if log_prefix and log_prefix != 'Найдена кнопка какеры':
        text = f"{log_prefix}: {text}"
    log_msg('KAKER', text, dim=seen <= total)


def _kakera_targets(message, *, include_spheres=True):
    """Кнопки какеры и ourosphere. Сферы жмутся и без силы на реакцию."""
    kakera_names = _kakera_click_names()
    take_kakera = _kakera_clicks_open(message)
    targets = []
    for button in _iter_message_buttons(message.get('components')):
        emoji = _button_emoji_name(button)
        key = emoji.casefold()
        if include_spheres and key in _OUROSPHERE_VALUES_CF:
            targets.append((emoji, button))
        elif take_kakera and key in kakera_names:
            targets.append((emoji, button))

    def _sort_key(item):
        key = item[0].casefold()
        if key in _OUROSPHERE_VALUES_CF:
            return 1000 + _OUROSPHERE_VALUES_CF[key]
        return _KAKERA_VALUES_CF.get(key, 0)

    targets.sort(key=_sort_key, reverse=True)
    return targets


def _refresh_kakera_message(message):
    if not message:
        return message
    mid = str(message.get('id') or '')
    fetched = None
    if message.get('id'):
        fetched = _fetch_channel_message(
            message.get('channel_id') or Vars.channelId,
            message.get('id'),
            quiet=True,
        )
    cached = _MESSAGE_CACHE.get(mid) if mid else None
    best = message
    best_n = len(_raw_loot_signature(best))
    for candidate in (cached, fetched):
        if not isinstance(candidate, dict) or not candidate.get('components'):
            continue
        count = len(_raw_loot_signature(candidate))
        if count >= best_n:
            best = candidate
            best_n = count
    return best


def _wait_kakera_buttons_ready(message, *, timeout=None):
    """Ждёт, пока набор кнопок перестанет меняться. $bku часто дорисовывает третью."""
    blob = _message_text_blob(message).lower()
    if '$bku' not in blob and not _raw_loot_buttons(message):
        return message
    if timeout is None:
        timeout = 2.2 if '$bku' in blob else 1.15
    stable_for = 0.45 if '$bku' in blob else 0.32

    deadline = time.monotonic() + timeout
    last_sig = _raw_loot_signature(message)
    last_change = time.monotonic()
    current = message

    while time.monotonic() < deadline:
        human_sleep(0.18, 0.28, kind='kakera', log=False)
        mid = str(current.get('id') or '')
        if mid and mid in _LOOT_DIRTY:
            _LOOT_DIRTY.discard(mid)
        current = _refresh_kakera_message(current)
        sig = _raw_loot_signature(current)
        if sig != last_sig:
            last_sig = sig
            last_change = time.monotonic()
        elif last_sig and time.monotonic() - last_change >= stable_for:
            break
    return current


def click_desired_kakera(
    message,
    *,
    log_prefix='Найдена кнопка какеры',
    clicked_ids=None,
    wait_for_buttons=True,
    include_spheres=True,
):
    """Жмёт какеры и ourosphere по одной, каждый раз с актуальным custom_id."""
    if not message or not message.get('id'):
        return 0
    if clicked_ids is None:
        clicked_ids = set()

    limit = int(getattr(Vars, 'kakera_max_clicks', 8))
    if limit <= 0:
        limit = 32

    if wait_for_buttons:
        message = _wait_kakera_buttons_ready(message)
    elif not _raw_loot_buttons(message):
        message = _refresh_kakera_message(message)
        if not _raw_loot_buttons(message):
            human_sleep(0.35, 0.55, kind='kakera', log=False)
            message = _refresh_kakera_message(message)

    channel_id = message.get('channel_id') or Vars.channelId
    guild_id = message.get('guild_id') or Vars.serverId
    clicks = 0
    clicked_kakera = []
    clicked_spheres = []
    seen = len(_desired_kakera_buttons(message))
    last_poll_id = str(message['id'])
    late_settled = False
    energy_tried = False
    final_polled = False

    while clicks < limit:
        seen = max(seen, len(_desired_kakera_buttons(message)))
        targets = [
            item for item in _kakera_targets(message, include_spheres=include_spheres)
            if item[1].get('custom_id') not in clicked_ids
        ]
        if not targets:
            mid = str(message.get('id') or '')
            dirty = mid in _LOOT_DIRTY
            if dirty:
                _LOOT_DIRTY.discard(mid)
            late = '$bku' in _message_text_blob(message).lower()
            if (dirty or late) and not late_settled:
                late_settled = True
                message = _wait_kakera_buttons_ready(message)
                continue
            if not energy_tried and _desired_kakera_buttons(message):
                energy_tried = True
                if try_use_dk_for_kakera_cd():
                    message = _refresh_kakera_message(message)
                    continue
            if clicks and not final_polled:
                final_polled = True
                poll_mudae_chat_replies(
                    last_poll_id,
                    channel_id=channel_id,
                    attempts=2,
                    delay=human_delay(0.2, 0.35),
                )
                message = _refresh_kakera_message(message)
                seen = max(seen, len(_desired_kakera_buttons(message)))
                continue
            break

        emoji, button = targets[0]
        custom_id = button.get('custom_id')
        clicked_ids.add(custom_id)
        is_kakera = emoji.casefold() in _kakera_click_names()
        try:
            human_sleep(0.08, 0.18, kind='kakera', log=False)
            author_id = (message.get('author') or {}).get('id') or botID
            response = _active_account().client.click(
                author_id,
                channelID=channel_id,
                guildID=guild_id,
                messageID=message['id'],
                messageFlags=message.get('flags', 0),
                data={'component_type': 2, 'custom_id': custom_id},
            )
            status = getattr(response, 'status_code', None)
            if status not in (200, 204, None):
                log_msg('WARN', f"Клик {emoji} отклонён (код: {status})")
            else:
                clicks += 1
                energy_tried = False
                if is_kakera:
                    clicked_kakera.append(emoji)
                    _spend_kakera_power(message, _active_account())
                else:
                    clicked_spheres.append(emoji)
        except Exception as error:
            log_msg('WARN', f"Ошибка нажатия: {error}")

        poll_mudae_chat_replies(
            last_poll_id,
            channel_id=channel_id,
            attempts=2,
            delay=human_delay(0.16, 0.28),
        )
        message = _refresh_kakera_message(message)

    seen = max(seen, len(_desired_kakera_buttons(message)), len(clicked_kakera))
    if seen == 0 and not clicked_spheres:
        _log_unclicked_kakera(message)
    else:
        _log_loot_summary(message, clicked_kakera, clicked_spheres, seen, log_prefix)
    return clicks


def _is_loot_button(button):
    """Какера или ourosphere — это не кнопка клейма."""
    return _is_kakera_button(button) or _is_ourosphere_button(button)


def find_claim_button(components):
    """Ищет кнопку клейма среди components (сердце / claim / первая не-какера и не-сфера)."""
    buttons = list(_iter_message_buttons(components))
    if not buttons:
        return None

    for button in buttons:
        if _is_loot_button(button):
            continue
        emoji_name = ((button.get('emoji') or {}).get('name') or '').lower()
        label = (button.get('label') or '').lower()
        custom_id = (button.get('custom_id') or '').lower()
        blob = f'{emoji_name} {label} {custom_id}'
        if any(hint in blob for hint in _CLAIM_HINTS):
            return button

    for button in buttons:
        if not _is_loot_button(button):
            return button
    return None


def _fetch_channel_message(channel_id, message_id, *, quiet=False):
    """Подтягивает актуальное сообщение (актуальные components)."""
    try:
        response = requests.get(
            f'https://discord.com/api/v9/channels/{channel_id}/messages/{message_id}',
            headers=auth,
            timeout=10,
        )
    except requests.RequestException as error:
        if not quiet:
            log_msg('WARN', f"Не удалось получить сообщение: {error}")
        return None
    if response.status_code == 200:
        return response.json()
    if not quiet:
        log_msg('WARN', f"GET message: код {response.status_code}")
    return None


def _claim_via_button(message, button):
    """Клик по кнопке-компоненту клейма."""
    channel_id = message.get('channel_id') or Vars.channelId
    guild_id = message.get('guild_id') or Vars.serverId
    author_id = (message.get('author') or {}).get('id') or botID
    emoji_name = (button.get('emoji') or {}).get('name') or button.get('label') or '?'
    claimer = _active_account()
    log_msg(
        'CLAIM',
        f"Клейм через кнопку ({claimer.label}, {emoji_name}, custom_id={button.get('custom_id')[:24]}...).",
        dim=True,
    )
    action_pause()
    try:
        response = claimer.client.click(
            author_id,
            channelID=str(channel_id),
            guildID=str(guild_id),
            messageID=str(message['id']),
            messageFlags=message.get('flags', 0),
            data={'component_type': 2, 'custom_id': button['custom_id']},
        )
    except Exception as error:
        log_msg('WARN', f"Ошибка клика по кнопке клейма: {error}")
        return False

    status = getattr(response, 'status_code', None)
    # Discord interactions часто отвечают 204 No Content
    if status in (200, 204):
        return True
    if status == 429:
        log_msg('WARN', "Rate Limit при клике по кнопке клейма.")
        return False
    log_msg('WARN', f"Клик по кнопке клейма отклонён (код: {status})")
    return False


def _claim_via_reaction(message_id, channel_id=None):
    """Старый fallback: реакция 🦫 под сообщением."""
    channel_id = channel_id or Vars.channelId
    log_msg('CLAIM', "Клейм через реакцию (fallback, нет кнопки).", dim=True)
    action_pause()
    try:
        claim_req = requests.put(
            f'https://discord.com/api/v9/channels/{channel_id}/messages/'
            f'{message_id}/reactions/{_CLAIM_REACTION_EMOJI}/%40me',
            headers=_active_account().auth,
            timeout=10,
        )
    except requests.RequestException as error:
        log_msg('WARN', f"Ошибка сети при клейме-реакции: {error}")
        return False

    if claim_req.status_code in (200, 204):
        return True
    if claim_req.status_code == 429:
        log_msg('WARN', "Rate Limit при попытке заклеймить карту (реакция).")
    else:
        log_msg('WARN', f"Ошибка клейма-реакции (код: {claim_req.status_code})")
    return False


def _claim_card(
    message_id,
    reason,
    profile_state,
    *,
    message=None,
    components=None,
    channel_id=None,
    card_name='',
    card_series='',
    card_power=0,
    allow_rt=False,
):
    """Клеймит карту: сначала кнопка-компонент, иначе реакция-эмодзи.

    message — полный объект Discord-сообщения (предпочтительно).
    components — можно передать отдельно, если message урезан.
    channel_id — канал сообщения (для снайпа / fallback).
    """
    resolved_channel = channel_id or Vars.channelId
    if isinstance(message, dict):
        resolved_channel = message.get('channel_id') or resolved_channel
        message_id = message.get('id') or message_id

    if message_id and _CLAIM_CLICKS.get(str(message_id), 0) >= 3:
        log_msg('WARN', "Клейм этой карты уже пробовали несколько раз. Останавливаюсь.")
        return False

    msg, claim_button, resolved_channel, message_id = _await_claim_button(
        message, message_id, resolved_channel, components,
    )
    if not claim_button:
        log_msg(
            'WARN',
            "Кнопка клейма ещё не появилась. $rt не трачу, дождусь правки сообщения.",
        )
        return None

    if not _ensure_claim_ready(
        profile_state,
        allow_rt=allow_rt,
        channel_id=resolved_channel,
        message_id=message_id,
    ):
        return False

    # После $rt Mudae часто пересобирает кнопки — берём свежий custom_id.
    msg, claim_button, resolved_channel, message_id = _await_claim_button(
        msg, message_id, resolved_channel, None, timeout=1.6,
    )
    if not claim_button:
        log_msg('WARN', "После $rt кнопка клейма пропала из сообщения. Повторю на следующей правке.")
        return None

    name = card_name
    series = card_series
    power = card_power
    if isinstance(message, dict):
        embeds = message.get('embeds') or []
        if embeds:
            emb = embeds[0]
            parsed_name, parsed_series, parsed_power, _ = parse_card_embed(emb)
            if not name:
                name = parsed_name
            if not series:
                series = parsed_series if parsed_series != 'Неизвестно' else ''
            if not power:
                power = parsed_power

    note_pending_claim(
        name=name,
        series=series,
        power=power,
        reason=reason,
    )

    log_msg('CLAIM', f"Причина клейма — {reason}. Пытаюсь заклеймить...", dim=True)

    attempt_started = time.time()
    while True:
        if _our_marriage_since(attempt_started):
            log_msg('CLAIM', "УСПЕХ: Mudae уже сообщила о свадьбе.")
            _note_claimed_ok(message_id)
            profile_state['can_claim'] = False
            sync_profile_state(profile_state)
            WebDashboard.emit_buffer_clear()
            return True
        clicks = _claim_click_count(message_id)
        after_click = _get_latest_message_id()
        clicked = _claim_via_button(msg, claim_button)
        outcome = 'rejected'
        if clicked:
            outcome = _poll_claim_outcome(
                after_click, resolved_channel, message_id, since=attempt_started,
            )
        if outcome != 'ok' and _our_marriage_since(attempt_started):
            outcome = 'ok'
        if outcome == 'ok':
            log_msg('CLAIM', "УСПЕХ: Mudae забрала персонажа.")
            _note_claimed_ok(message_id)
            profile_state['can_claim'] = False
            sync_profile_state(profile_state)
            WebDashboard.emit_buffer_clear()
            return True
        if outcome == 'lost':
            log_msg('WARN', "Персонажа уже забрал кто-то другой. Клейм не списываю.")
            return False
        if clicks >= 3:
            log_msg('WARN', "Три клика по сердцу не заклеймили персонажа. Останавливаюсь.")
            return False
        log_msg('WARN', "Клейм не прошёл. Жду и жму сердце ещё раз.", dim=True)
        human_sleep(0.7, 1.1, kind='action', log=False)
        msg, claim_button, resolved_channel, message_id = _await_claim_button(
            msg, message_id, resolved_channel, None, timeout=1.4,
        )
        if not claim_button:
            fetched = _refresh_card_message(resolved_channel, message_id)
            if _our_marriage_since(attempt_started) or (fetched and _we_own_card(fetched)):
                log_msg('CLAIM', "УСПЕХ: Mudae забрала персонажа.")
                _note_claimed_ok(message_id)
                profile_state['can_claim'] = False
                sync_profile_state(profile_state)
                WebDashboard.emit_buffer_clear()
                return True
            log_msg('WARN', "Кнопка клейма исчезла до подтверждения. Повторю, если сообщение ещё раз обновят.")
            return None

def _send_pokeslot():
    """Отправляет $p. Сбой сети не должен ронять поток расписания."""
    log_msg('ROLL', "Пытаюсь крутить Pokeslot ($p)...", dim=True)
    try:
        poke_req = requests.post(
            url=url, headers=_active_account().auth, json={'content': '$p'}, timeout=15
        )
    except requests.RequestException as error:
        log_msg('WARN', f"Pokeslot: соединение сброшено ({error})")
        return
    if poke_req.status_code in [200, 204]:
        log_msg('ROLL', "Pokeslot успешно отправлен.")
    else:
        log_msg('WARN', f"Не удалось отправить Pokeslot (код: {poke_req.status_code})")


# Сессия роллов: поток отправки только шлёт команды, карточки разбирает Gateway.
_ROLL_LOCK = threading.Lock()
_ROLL_SESSION = {
    'active': False,
    'limit_reached': False,
    'claim_spent': False,
    'claiming': False,
    'claimed_msg_id': '',
    'claim_buffer': [],
    'profile_state': None,
    'current_threshold': 0.0,
    'jackpot_threshold': 0.0,
    'last_send_at': 0.0,
    'last_event_at': 0.0,
    'bonus_us': False,
    'claims_used': set(),
    'roller_key': 'main',
    'stop_before': None,
}
_OWNER_BY_MESSAGE = {}
_MESSAGE_CACHE = {}
_CLAIM_DECIDED = set()
_CLAIM_INFLIGHT = set()
_CLAIMED_OK = set()
_UNKNOWN_OWNER_LOGGED = set()
_KAKERA_CD_LOGGED = set()
_CLICKED_COMPONENTS = {}
_LOGGED_DROPS = set()


def _remember_set(bucket, key, limit=400):
    """Добавляет key в set. True, если key ещё не было."""
    if key in bucket:
        return False
    if len(bucket) > limit:
        bucket.clear()
    bucket.add(key)
    return True


def _note_claimed_ok(message_id):
    """Карта уже забрана нашим клеймом. Повтор этого сообщения не тратит $rt."""
    mid = str(message_id or '')
    if not mid:
        return
    with _ROLL_LOCK:
        if len(_CLAIMED_OK) > 400:
            _CLAIMED_OK.clear()
        _CLAIMED_OK.add(mid)
        _ROLL_SESSION['claimed_msg_id'] = mid


def _rt_would_reclaim_ours(message_id, channel_id=None):
    """True, если $rt ушёл бы в карту, которую мы уже заклеймили."""
    mid = str(message_id or '')
    with _ROLL_LOCK:
        if mid and (mid in _CLAIMED_OK or mid == str(_ROLL_SESSION.get('claimed_msg_id') or '')):
            return True
    if not mid or not channel_id:
        return False
    fresh = _refresh_card_message(channel_id, mid)
    if fresh and _we_own_card(fresh):
        _note_claimed_ok(mid)
        return True
    return False


def _begin_claim(mid):
    """False — это сообщение уже клеймится или уже заклеймлено."""
    if not mid:
        return True
    with _ROLL_LOCK:
        if mid in _CLAIM_INFLIGHT or mid in _CLAIMED_OK or mid in _CLAIM_DECIDED:
            return False
        _CLAIM_INFLIGHT.add(mid)
        return True


def _end_claim(mid, result):
    if not mid:
        return
    with _ROLL_LOCK:
        _CLAIM_INFLIGHT.discard(mid)
        if result:
            if len(_CLAIMED_OK) > 400:
                _CLAIMED_OK.clear()
            _CLAIMED_OK.add(mid)
            _ROLL_SESSION['claimed_msg_id'] = mid
    if result is not None:
        _remember_set(_CLAIM_DECIDED, mid)


def _track_claim(mid, fn):
    """Один клейм на сообщение. Правка Discord не запускает второй, пока первый жив."""
    if mid and not _begin_claim(mid):
        return False
    result = None
    try:
        result = fn()
    finally:
        _end_claim(mid, result)
    return result


def _clicked_components(message_id):
    mid = str(message_id)
    bucket = _CLICKED_COMPONENTS.get(mid)
    if bucket is None:
        if len(_CLICKED_COMPONENTS) > 300:
            _CLICKED_COMPONENTS.clear()
        bucket = set()
        _CLICKED_COMPONENTS[mid] = bucket
    return bucket


def _interaction_user_id(message):
    """Id пользователя, который вызвал слэш-команду Mudae."""
    for key in ('interaction_metadata', 'interaction'):
        block = message.get(key) or {}
        if not isinstance(block, dict):
            continue
        user = block.get('user') or {}
        if not user and isinstance(block.get('member'), dict):
            user = (block.get('member') or {}).get('user') or {}
        uid = str(user.get('id') or block.get('user_id') or '')
        if uid:
            return uid
    return ''


def _message_text_blob(message):
    parts = [message.get('content') or '']
    for embed in message.get('embeds') or []:
        if not isinstance(embed, dict):
            continue
        parts.append(embed.get('description') or '')
        parts.append(embed.get('title') or '')
        parts.append((embed.get('author') or {}).get('name') or '')
        parts.append((embed.get('footer') or {}).get('text') or '')
    return '\n'.join(parts)


def _wish_mention_owner(message):
    """'ours' / 'other' / None по строке Wished by."""
    blob = _message_text_blob(message) if isinstance(message, dict) else ''
    wish = re.search(r'wished\s+by\s+(.+)', blob, re.IGNORECASE)
    if not wish:
        return None
    tail = wish.group(1).splitlines()[0]
    for account in Accounts.all_accounts():
        if account.user_id and (
            f'<@{account.user_id}>' in tail or f'<@!{account.user_id}>' in tail
        ):
            return 'ours'
    if re.search(r'<@!?\d+>', tail):
        return 'other'
    who = re.sub(r'<@!?\d+>', '', tail)
    who = re.sub(r'\(edited\).*', '', who, flags=re.IGNORECASE)
    who = who.strip(' *@`_')
    who_cf = who.casefold()
    if not who_cf:
        return None
    for name in _our_discord_names():
        n = (name or '').casefold()
        if n and (who_cf == n or who_cf.startswith(n + ' ')):
            return 'ours'
    return 'other'


def _priority_claim_reason(our_wish, name, power, jackpot_threshold):
    if our_wish:
        return f"наш вишлист: {name}"
    return f"джекпот цена {power} >= {jackpot_threshold:g}"


_ROLLER_BY_MESSAGE = {}


def _remember_roller(message, account):
    mid = str((message or {}).get('id') or '')
    if not mid or account is None:
        return
    _ROLLER_BY_MESSAGE[mid] = account.key
    if len(_ROLLER_BY_MESSAGE) > 500:
        _ROLLER_BY_MESSAGE.clear()
        _ROLLER_BY_MESSAGE[mid] = account.key


def roller_account(message):
    """Кто из роя отправил этот ролл. Иначе крутящий текущей сессии."""
    mid = str((message or {}).get('id') or '')
    found = Accounts.by_key(_ROLLER_BY_MESSAGE.get(mid))
    if found:
        return found
    with _ROLL_LOCK:
        key = _ROLL_SESSION.get('roller_key')
    return Accounts.by_key(key)


def _classify_roll_owner_fresh(message):
    """'ours' / 'other' / 'unknown' по этому payload, без кэша."""
    uid = _interaction_user_id(message)
    if uid:
        account = Accounts.by_user_id(uid)
        if account:
            _remember_roller(message, account)
            return 'ours'
        return 'other'

    ref = message.get('referenced_message') or {}
    if isinstance(ref, dict):
        ref_author = str((ref.get('author') or {}).get('id') or '')
        if ref_author:
            account = Accounts.by_user_id(ref_author)
            if account:
                _remember_roller(message, account)
                return 'ours'
            return 'other'

    blob = _message_text_blob(message)
    _learn_emoji_shortcodes(blob)
    return _wish_mention_owner(message) or 'unknown'


def classify_roll_owner(message):
    """Чей ролл. Правка сообщения наследует решение с первого события."""
    mid = str(message.get('id') or '')
    fresh = _classify_roll_owner_fresh(message)
    if fresh in ('ours', 'other') and mid:
        _OWNER_BY_MESSAGE[mid] = fresh
        if len(_OWNER_BY_MESSAGE) > 500:
            _OWNER_BY_MESSAGE.clear()
            _OWNER_BY_MESSAGE[mid] = fresh
        return fresh
    if mid and mid in _OWNER_BY_MESSAGE:
        return _OWNER_BY_MESSAGE[mid]
    return 'unknown'


def _looks_like_card(message):
    return bool(message.get('embeds') or message.get('components'))


def _log_unknown_owner(message):
    mid = str(message.get('id') or '')
    if not mid or not _remember_set(_UNKNOWN_OWNER_LOGGED, mid):
        return
    log_msg(
        'ROLL',
        f"Не понял, чей ролл (нет interaction и Wished by), сообщение {mid} — не кликаю.",
        dim=True,
    )


def begin_roll_session(
    profile_state,
    current_threshold,
    jackpot_threshold,
    *,
    bonus_us=False,
    roller_key='main',
    stop_before=None,
):
    with _ROLL_LOCK:
        _ROLL_SESSION['active'] = True
        _ROLL_SESSION['limit_reached'] = False
        _ROLL_SESSION['claim_spent'] = False
        _ROLL_SESSION['claiming'] = False
        _ROLL_SESSION['claimed_msg_id'] = ''
        _ROLL_SESSION['claim_buffer'] = []
        _ROLL_SESSION['claims_used'] = set()
        _ROLL_SESSION['bonus_us'] = bool(bonus_us)
        _ROLL_SESSION['roller_key'] = roller_key or 'main'
        _ROLL_SESSION['stop_before'] = stop_before
        _ROLL_SESSION['profile_state'] = profile_state
        _ROLL_SESSION['current_threshold'] = float(current_threshold)
        _ROLL_SESSION['jackpot_threshold'] = float(jackpot_threshold)
        now = time.time()
        _ROLL_SESSION['last_send_at'] = now
        _ROLL_SESSION['last_event_at'] = now
    WebDashboard.emit_buffer_clear()


def _someone_can_still_claim():
    """Вызывать под _ROLL_LOCK. Свободный клейм ещё есть у кого-то из роя."""
    used = _ROLL_SESSION.get('claims_used') or set()
    return any(
        account.key not in used and account.profile.get('can_claim')
        for account in Accounts.all_accounts()
    )


def _finish_claim_success(account, message_id):
    """Клейм списан только у этого аккаунта. Буфер остаётся, если кто-то ещё может взять карту."""
    cleared = False
    with _ROLL_LOCK:
        used = _ROLL_SESSION.setdefault('claims_used', set())
        if account is not None:
            used.add(account.key)
        if message_id:
            mid = str(message_id)
            _ROLL_SESSION['claimed_msg_id'] = mid
            if len(_CLAIMED_OK) > 400:
                _CLAIMED_OK.clear()
            _CLAIMED_OK.add(mid)
        if _ROLL_SESSION.get('active') and not _someone_can_still_claim():
            _ROLL_SESSION['claim_spent'] = True
            _ROLL_SESSION['claim_buffer'] = []
            cleared = True
        elif _ROLL_SESSION.get('active'):
            _ROLL_SESSION['claim_spent'] = False
    if cleared:
        WebDashboard.emit_buffer_clear()
    elif message_id:
        _drop_buffered_card(message_id)
    return True


def end_roll_session():
    with _ROLL_LOCK:
        _ROLL_SESSION['active'] = False
        _ROLL_SESSION['claim_buffer'] = []
        _ROLL_SESSION['claiming'] = False
    WebDashboard.emit_buffer_clear()


def roll_session_limit_reached():
    with _ROLL_LOCK:
        return bool(_ROLL_SESSION['limit_reached'])


def note_roll_sent():
    with _ROLL_LOCK:
        _ROLL_SESSION['last_send_at'] = time.time()


def _session_profile():
    return _ROLL_SESSION.get('profile_state') or PROFILE_STATE


def _note_roll_event():
    _ROLL_SESSION['last_event_at'] = time.time()


def _drop_buffered_card(message_id):
    mid = str(message_id)
    removed = False
    remaining = []
    with _ROLL_LOCK:
        kept = []
        for card in _ROLL_SESSION['claim_buffer']:
            if str(card.get('msg_id')) == mid:
                removed = True
                continue
            kept.append(card)
        _ROLL_SESSION['claim_buffer'] = kept
        remaining = list(kept)
    if not removed:
        return
    WebDashboard.emit_buffer_clear()
    ttl = get_claim_ttl()
    for card in remaining:
        WebDashboard.emit_buffer_card({
            'msg_id': card['msg_id'],
            'power': card['power'],
            'name': card['name'],
            'series': card['series'],
            'ttl_ms': ttl * 1000,
            'claim_ttl': ttl,
        })


def _refresh_buffered_card(message):
    mid = str(message.get('id') or '')
    with _ROLL_LOCK:
        for card in _ROLL_SESSION['claim_buffer']:
            if str(card.get('msg_id')) == mid:
                card['message'] = message
                card['components'] = message.get('components')
                return


def _claim_from_session(message, reason, *, allow_rt, name, series, power, clear_buffer=False):
    """Клейм своей карты. Если буфер уже клеймит, ждём его и потом решаем."""
    message_id = str(message.get('id') or '')
    deadline = time.monotonic() + 8
    while True:
        skip_same = False
        with _ROLL_LOCK:
            if not _ROLL_SESSION['active']:
                return False
            if not _ROLL_SESSION['claiming']:
                # Джекпот включает allow_rt. Это не повод второй раз клеймить
                # ту же карту и сжигать $rt после уже успешного свободного клейма.
                if message_id and message_id == str(_ROLL_SESSION.get('claimed_msg_id') or ''):
                    skip_same = True
                else:
                    if _ROLL_SESSION['claim_spent'] and not allow_rt:
                        return False
                    _ROLL_SESSION['claiming'] = True
                    roller_key = _ROLL_SESSION.get('roller_key')
                    used = set(_ROLL_SESSION.get('claims_used') or ())
                    if clear_buffer:
                        _ROLL_SESSION['claim_buffer'] = []
                    break
        if skip_same:
            log_msg(
                'CLAIM',
                'Этот персонаж уже заклеймлен свободным клеймом. $rt не трачу.',
                dim=True,
            )
            return False
        if time.monotonic() > deadline:
            return None
        time.sleep(0.05)
    if clear_buffer:
        WebDashboard.emit_buffer_clear()
    import Swarm
    account = Swarm.pick_claimer(roller_key, allow_rt=allow_rt, claims_used=used)
    success = False
    try:
        if account is None:
            log_msg('CLAIM', 'Клеймить некому: в рое нет свободного клейма.', dim=True)
            return False
        success = _claim_card(
            message.get('id'),
            f'{account.label}: {reason}',
            account.profile,
            message=message,
            components=message.get('components'),
            card_name=name or '',
            card_series=series or '',
            card_power=power or 0,
            allow_rt=allow_rt,
        )
    finally:
        with _ROLL_LOCK:
            _ROLL_SESSION['claiming'] = False
        if success:
            _finish_claim_success(account, message_id)
    return success


def process_roll_claim_buffer(force=False):
    """Клеймит лучшую карточку буфера, когда истекает TTL (или force)."""
    with _ROLL_LOCK:
        if not _ROLL_SESSION['active']:
            return
        if _ROLL_SESSION['claim_spent']:
            dirty = bool(_ROLL_SESSION['claim_buffer'])
            _ROLL_SESSION['claim_buffer'] = []
            best = None
            should_clear = dirty
        elif not _ROLL_SESSION['claim_buffer']:
            return
        elif _ROLL_SESSION['claiming']:
            return
        else:
            should_clear = False
            elapsed = time.time() - _ROLL_SESSION['claim_buffer'][0]['timestamp']
            claim_ttl = get_claim_ttl()
            if not force and elapsed < claim_ttl:
                return
            best = max(_ROLL_SESSION['claim_buffer'], key=lambda card: card['power'])
            trigger = "достигнут лимит роллов" if force else f"сработал таймер {claim_ttl} сек"
            size = len(_ROLL_SESSION['claim_buffer'])
            _ROLL_SESSION['claiming'] = True
            roller_key = _ROLL_SESSION.get('roller_key')
            used = set(_ROLL_SESSION.get('claims_used') or ())
            best_id = str(best.get('msg_id') or '')
            _ROLL_SESSION['claim_buffer'] = [
                card for card in _ROLL_SESSION['claim_buffer']
                if str(card.get('msg_id')) != best_id
            ]
            remaining = list(_ROLL_SESSION['claim_buffer'])
    if best is None:
        if should_clear:
            WebDashboard.emit_buffer_clear()
        return

    WebDashboard.emit_buffer_clear()
    ttl = get_claim_ttl()
    for card in remaining:
        WebDashboard.emit_buffer_card({
            'msg_id': card['msg_id'],
            'power': card['power'],
            'name': card['name'],
            'series': card['series'],
            'ttl_ms': ttl * 1000,
            'claim_ttl': ttl,
        })
    log_msg(
        'CLAIM',
        f"Буфер: {trigger}; старшей карте {elapsed:.1f} сек. "
        f"Выбрана лучшая карта (цена персонажа {best['power']}) из {size}.",
    )
    import Swarm
    account = Swarm.pick_claimer(roller_key, allow_rt=False, claims_used=used)
    success = False
    try:
        if account is None:
            log_msg('CLAIM', 'Буфер: клеймить некому, карта остаётся в канале.', dim=True)
        else:
            success = _claim_card(
                best['msg_id'],
                f"{account.label}: снайпинг из буфера (цена {best['power']})",
                account.profile,
                message=best.get('message'),
                components=best.get('components'),
                card_name=best.get('name') or '',
                card_series=best.get('series') or '',
                card_power=best.get('power') or 0,
            )
    finally:
        with _ROLL_LOCK:
            _ROLL_SESSION['claiming'] = False
        if success:
            _finish_claim_success(account, best.get('msg_id'))


def _buffer_own_card(message, name, series, power, threshold):
    with _ROLL_LOCK:
        if not _ROLL_SESSION['active'] or _ROLL_SESSION['claim_spent']:
            return False
        mid = str(message.get('id'))
        if any(str(card.get('msg_id')) == mid for card in _ROLL_SESSION['claim_buffer']):
            return False
        _ROLL_SESSION['claim_buffer'].append({
            'msg_id': message.get('id'),
            'power': power,
            'name': name,
            'series': series,
            'timestamp': time.time(),
            'message': message,
            'components': message.get('components'),
        })
        size = len(_ROLL_SESSION['claim_buffer'])
    ttl = get_claim_ttl()
    WebDashboard.emit_buffer_card({
        'msg_id': message.get('id'),
        'power': power,
        'name': name,
        'series': series,
        'ttl_ms': ttl * 1000,
        'claim_ttl': ttl,
    })
    log_msg(
        'CLAIM',
        f"Карта (цена персонажа {power}) добавлена в буфер "
        f"(порог {threshold:g}, размер: {size}).",
    )
    return True


def _consider_own_claim(message):
    """Клейм своей карты: в сессии — буфер, вне сессии — сразу по порогу."""
    embeds = message.get('embeds') or []
    if not embeds:
        return
    mid = str(message.get('id') or '')
    embed = embeds[0]
    name, series, power, is_claimed = parse_card_embed(embed)
    if mid and _remember_set(_LOGGED_DROPS, mid):
        _drop_log(name, series, power, is_claimed, message_id=message.get('id'))
        apply_key_kakera_bonus(embed, message_id=message.get('id'))

    if is_claimed:
        _drop_buffered_card(mid)
        _remember_set(_CLAIM_DECIDED, mid)
        return

    _refresh_buffered_card(message)
    if mid and mid in _CLAIM_DECIDED:
        return

    with _ROLL_LOCK:
        active = _ROLL_SESSION['active']
        claim_spent = _ROLL_SESSION['claim_spent']
        threshold = float(_ROLL_SESSION['current_threshold'] or 0)
        jackpot_threshold = float(_ROLL_SESSION['jackpot_threshold'] or 0)
        profile = _session_profile()

    our_wish = _wish_mention_owner(message) == 'ours'

    if not active:
        threshold = PROFILE_STATE.get('current_threshold')
        if threshold is None:
            threshold = compute_claim_threshold(PROFILE_STATE.get('claim_minutes_left', 0))
        threshold = float(threshold)
        jackpot_threshold = max(
            float(Vars.min_power_threshold) * float(getattr(Vars, 'jackpot_multiplier', 3)),
            threshold * float(getattr(Vars, 'jackpot_threshold_factor', 1.5)),
        )

        def _outside(reason, allow_rt):
            import Swarm
            roller = roller_account(message)
            account = Swarm.pick_claimer(
                roller.key if roller else Accounts.MAIN.key,
                allow_rt=allow_rt,
                claims_used=set(),
            )
            if account is None:
                log_msg('CLAIM', f'Пропуск {name}: в рое нет свободного клейма.', dim=True)
                return False
            return _claim_card(
                message.get('id'),
                f'{account.label}: {reason}',
                account.profile,
                message=message,
                card_name=name,
                card_series=series,
                card_power=power,
                allow_rt=allow_rt,
            )

        if our_wish or power >= jackpot_threshold:
            _track_claim(mid, lambda: _outside(
                _priority_claim_reason(our_wish, name, power, jackpot_threshold),
                True,
            ))
            return
        if series in Vars.desiredSeries:
            _track_claim(mid, lambda: _outside(f"совпадение по серии: '{series}'", False))
            return
        if power >= threshold:
            _track_claim(mid, lambda: _outside(
                f"своя карта цена {power} >= {threshold:g}",
                False,
            ))
            return
        if mid:
            _remember_set(_CLAIM_DECIDED, mid)
        return

    if our_wish or power >= jackpot_threshold:
        _track_claim(mid, lambda: _claim_from_session(
            message,
            _priority_claim_reason(our_wish, name, power, jackpot_threshold),
            allow_rt=True,
            name=name,
            series=series,
            power=power,
            clear_buffer=True,
        ))
        return
    if claim_spent:
        if mid:
            _remember_set(_CLAIM_DECIDED, mid)
        return
    if series in Vars.desiredSeries:
        _track_claim(mid, lambda: _claim_from_session(
            message,
            f"совпадение по серии: '{series}'",
            allow_rt=False,
            name=name,
            series=series,
            power=power,
            clear_buffer=True,
        ))
        return
    if power >= threshold:
        _buffer_own_card(message, name, series, power, threshold)
    if mid:
        _remember_set(_CLAIM_DECIDED, mid)


_LOOT_GUARD = threading.Lock()
_LOOT_ACTIVE = set()


def _loot_accounts(message, only_account):
    """Кто жмёт добычу. Снайп передаёт только основной. Сферу жмёт крутящий."""
    import Swarm
    if only_account is not None:
        return [only_account], only_account.key
    roller = roller_account(message) or Accounts.MAIN
    ordered = []
    seen = set()
    for account in [roller] + list(Swarm.reaction_accounts()):
        if account is None or account.key in seen:
            continue
        seen.add(account.key)
        ordered.append(account)
    if not ordered:
        ordered = [roller]
    return ordered, roller.key


def click_card_loot(message, *, log_prefix='Найдена кнопка какеры', only_account=None):
    """Какера из Vars.desiredKakeras и ourosphere. Повторный клик той же кнопки не шлём.

    На своём ролле какеру жмёт каждый, у кого есть сила. Сферу — только крутящий.
    """
    if not message or not message.get('id'):
        return 0
    _learn_emoji_shortcodes(_message_text_blob(message))
    mid = str(message['id'])
    with _LOOT_GUARD:
        if mid in _LOOT_ACTIVE:
            _LOOT_DIRTY.add(mid)
            return 0
        _LOOT_ACTIVE.add(mid)
        _LOOT_DIRTY.discard(mid)
    try:
        accounts, sphere_key = _loot_accounts(message, only_account)
        total = 0
        for index, account in enumerate(accounts):
            if index:
                human_sleep(0.4, 1.2, kind='action', log=False)
            clicked_ids = _clicked_components(f'{mid}:{account.key}')
            with _use_account(account):
                for _ in range(4):
                    total += click_desired_kakera(
                        message,
                        log_prefix=f'{log_prefix} · {account.label}',
                        clicked_ids=clicked_ids,
                        wait_for_buttons=True,
                        include_spheres=(account.key == sphere_key),
                    ) or 0
                    with _LOOT_GUARD:
                        if mid not in _LOOT_DIRTY:
                            break
                        _LOOT_DIRTY.discard(mid)
                    message = _MESSAGE_CACHE.get(mid) or message
        return total
    finally:
        with _LOOT_GUARD:
            _LOOT_ACTIVE.discard(mid)
            _LOOT_DIRTY.discard(mid)


def on_mudae_gateway_message(message, *, is_update=False):
    """Своя карточка с Gateway: клейм, какера, сфера. Чужие не трогает.

    Возвращает 'ours' / 'other' / 'unknown'.
    """
    if not isinstance(message, dict):
        return 'unknown'
    _learn_emoji_shortcodes(_message_text_blob(message))

    owner = classify_roll_owner(message)
    content = message.get('content') or ''
    channel_id = str(message.get('channel_id') or '')
    with _ROLL_LOCK:
        active = _ROLL_SESSION['active']

    limit_text = 'the roulette is limited to' in content.lower()
    if limit_text and (
        owner == 'ours'
        or (
            owner == 'unknown'
            and active
            and channel_id == str(Vars.channelId)
        )
    ):
        with _ROLL_LOCK:
            _ROLL_SESSION['limit_reached'] = True
            bonus = bool(_ROLL_SESSION.get('bonus_us'))
            profile = _session_profile()
            if profile is not None and not bonus:
                profile['rolls_left'] = 0
        if _ROLL_SESSION.get('bonus_us'):
            log_msg('ROLL', "Разовые роллы $us кончились. Часовые роллы не трогаю.")
        else:
            log_msg('WARN', "Mudae сообщила о лимите прокрутов. Прокруты закончились!")
        return 'ours'

    if owner == 'unknown':
        if _looks_like_card(message):
            _log_unknown_owner(message)
        recent_own_roll = (
            active
            and channel_id == str(Vars.channelId)
            and (time.time() - LAST_OUR_ROLL_TIME) <= 6
        )
        if recent_own_roll and (message.get('components') or message.get('embeds')):
            with _ROLL_LOCK:
                roller = Accounts.by_key(_ROLL_SESSION.get('roller_key')) or Accounts.MAIN
            _remember_roller(message, roller)
            owner = 'ours'
        else:
            return 'unknown'
    if owner != 'ours':
        if (
            owner == 'other'
            and channel_id == str(Vars.channelId)
            and _looks_like_card(message)
        ):
            note_foreign_roll()
        return owner

    with _ROLL_LOCK:
        _note_roll_event()

    if not is_update or message.get('embeds'):
        _consider_own_claim(message)
    elif is_update:
        _refresh_buffered_card(message)

    if message.get('components') or message.get('embeds'):
        click_card_loot(message)
    return 'ours'


def _settle_roll_session():
    """Ждёт последние карточки и TTL буфера после того, как команды уже отправлены."""
    grace = 4.0
    deadline = time.monotonic() + get_claim_ttl() + grace
    while time.monotonic() < deadline:
        if roll_session_limit_reached():
            process_roll_claim_buffer(force=True)
            break
        process_roll_claim_buffer(force=False)
        with _ROLL_LOCK:
            spent = _ROLL_SESSION['claim_spent']
            pending = bool(_ROLL_SESSION['claim_buffer'])
            last = max(_ROLL_SESSION['last_send_at'], _ROLL_SESSION['last_event_at'])
        if (spent or not pending) and time.time() - last >= grace:
            break
        time.sleep(0.25)
    else:
        process_roll_claim_buffer(force=True)


def note_foreign_roll():
    """Чужая карта в нашем канале: эфир занят, свою крутку не начинаем."""
    global LAST_FOREIGN_ROLL_AT
    LAST_FOREIGN_ROLL_AT = time.time()


def _deadline_ts(deadline):
    if deadline is None:
        return None
    if hasattr(deadline, 'timestamp'):
        return float(deadline.timestamp())
    return float(deadline)


def wait_channel_quiet(deadline=None, max_wait=None):
    """Ждёт, пока чужие роллы затихнут.

    False — упёрлись в ресет и крутить нельзя.
    True — тихо, либо ждать дальше нельзя и сессию всё же можно продолжить.
    """
    lo = float(getattr(Vars, 'channel_quiet_min', 25) or 0)
    hi = float(getattr(Vars, 'channel_quiet_max', 50) or 0)
    if hi < lo:
        lo, hi = hi, lo
    need = human_delay(lo, hi) if hi > 0 else 0
    if need <= 0:
        return True
    deadline_ts = _deadline_ts(deadline)
    started = time.monotonic()
    announced = False
    while True:
        if deadline_ts is not None and time.time() >= deadline_ts:
            log_msg('ROLL', 'До ресета роллов уже не успеть.')
            return False
        if not LAST_FOREIGN_ROLL_AT or time.time() - LAST_FOREIGN_ROLL_AT >= need:
            if announced:
                log_msg('ROLL', 'Эфир свободен, продолжаю.', dim=True)
            return True
        if max_wait is not None and time.monotonic() - started >= float(max_wait):
            log_msg('ROLL', 'Эфир всё ещё занят. Дальше не жду и иду крутить.', dim=True)
            return True
        if not announced:
            log_msg('ROLL', 'Канал занят чужой круткой, жду тишину.', dim=True)
            announced = True
        time.sleep(1)


_US_READY = threading.Event()
_US_MESSAGE_ID = ''

_CHECK_NAMES = {
    'white_check_mark',
    '✅',
    '\u2705',
    '✔',
    'heavy_check_mark',
}


def _message_has_checkmark(message_id, account):
    """Галочка на нашем сообщении. REST, если событие реакции не дошло."""
    try:
        response = requests.get(
            f'https://discord.com/api/v9/channels/{Vars.channelId}/messages/{message_id}',
            headers=account.auth,
            timeout=8,
        )
    except requests.RequestException:
        return False
    if response.status_code != 200:
        return False
    for reaction in response.json().get('reactions') or []:
        emoji = reaction.get('emoji') or {}
        name = (emoji.get('name') or '').strip().strip(':')
        if name in _CHECK_NAMES and int(reaction.get('count') or 0) > 0:
            return True
    return False


def arm_us_wait(message_id):
    global _US_MESSAGE_ID
    _US_READY.clear()
    _US_MESSAGE_ID = str(message_id or '')


def note_us_reaction(message_id, user_id, emoji_name):
    """Галочка Mudae на нашем $us."""
    if not _US_MESSAGE_ID or str(message_id or '') != _US_MESSAGE_ID:
        return
    if str(user_id or '') != str(botID):
        return
    name = (emoji_name or '').strip().strip(':')
    if name in _CHECK_NAMES:
        _US_READY.set()


_US_BATCH = 20


def _confirm_us_batch(account):
    """Один $us 20. True, только если Mudae поставила галочку."""
    response = post_text_message('$us 20', account)
    if response is None or response.status_code not in (200, 204):
        log_msg('WARN', 'Не удалось отправить $us 20.')
        return False
    message_id = ''
    try:
        message_id = str((response.json() or {}).get('id') or '')
    except ValueError:
        message_id = ''
    if not message_id:
        log_msg('WARN', '$us 20 ушло, но id сообщения не пришёл.')
        return False
    arm_us_wait(message_id)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not _US_READY.is_set():
        if _message_has_checkmark(message_id, account):
            _US_READY.set()
            break
        time.sleep(0.4)
    if not _US_READY.is_set():
        log_msg('WARN', 'Mudae не поставила галочку на $us 20.')
        return False
    return True


def use_stacked_rolls(count=20, stop_before=None):
    """Несколько $us 20, затем столько же слэшей. Часовой запас не тратит.

    100 роллов — это пять команд `$us 20`. Без галочки пачка обрывается,
    и крутятся только уже подтверждённые.
    """
    account = Accounts.MAIN
    requested = max(_US_BATCH, int(count or _US_BATCH))
    batches = (requested + _US_BATCH - 1) // _US_BATCH
    unlocked = 0
    with _use_account(account):
        for index in range(batches):
            if stop_before is not None and time.time() >= _deadline_ts(stop_before):
                log_msg('WARN', 'Ресет близко, оставшиеся $us 20 не отправляю.')
                break
            log_msg('ROLL', f'Отправляю $us 20 ({index + 1}/{batches}).')
            if not _confirm_us_batch(account):
                break
            unlocked += _US_BATCH
            if index + 1 < batches:
                human_sleep(0.8, 1.6, kind='action', log=False)
        if unlocked <= 0:
            log_msg('WARN', 'Ни один $us 20 не подтвердился. Роллы не начинаю.')
            return False
        log_msg('ROLL', f'Открыто {unlocked} разовых роллов. Кручу.')
        simpleRoll(
            account,
            max_rolls=unlocked,
            bonus_us=True,
            record_duration=False,
            stop_before=stop_before,
        )
        return True


def simpleRoll(account=None, *, max_rolls=None, bonus_us=False, record_duration=True, stop_before=None):
    account = Accounts.resolve(account)
    elapsed = 0
    with _use_account(account):
        set_rolling(True)
        log_msg('ROLL', f"Начинаю сессию прокрутов ({account.label}): /{Vars.rollCommand}")
        try:
            elapsed = _simpleRoll_body(
                account,
                max_rolls=max_rolls,
                bonus_us=bonus_us,
                stop_before=stop_before,
            ) or 0
        finally:
            set_rolling(False)
    if record_duration and elapsed > 0 and not bonus_us:
        import Database
        Database.save_roll_duration(account.key, elapsed)
        account.last_session_seconds = float(elapsed)
        WebDashboard.emit_swarm_accounts()
    return elapsed


def _simpleRoll_body(account=None, *, max_rolls=None, bonus_us=False, stop_before=None):
    global LAST_OUR_ROLL_TIME
    account = Accounts.resolve(account)
    if not GATEWAY_LISTENING.wait(timeout=12):
        log_msg(
            'WARN',
            "Gateway ещё не слушает канал. Команды отправлю, но карточки могу пропустить.",
        )

    profile_state = updateProfileState(account=account, skip_actions=bonus_us)
    if profile_state is None:
        # При отсутствии ответа $tu не рискуем клеймом и используем базовый порог.
        profile_state = {
            'account_key': account.key,
            'can_claim': False,
            'rt_ready': False,
            'claim_minutes_left': 0,
        }
        log_msg('WARN', "Использую базовый порог: состояние профиля недоступно.")

    claim_minutes_left = profile_state.get('claim_minutes_left', 0)
    rt_ready = profile_state.get('rt_ready', False)

    # Жадность зависит ТОЛЬКО от времени до следующего сброса клейма.
    # $rt служит подушкой безопасности и не должен снижать наши требования к текущему клейму.
    current_threshold = compute_claim_threshold(claim_minutes_left)
    profile_state['current_threshold'] = current_threshold
    profile_state['claim_ttl'] = get_claim_ttl()
    sync_profile_state(profile_state)

    log_msg('SYS',
        f"Текущий порог клейма (цена персонажа): {current_threshold:g} "
        f"(до сброса клейма: {claim_minutes_left} мин, rt_ready: {rt_ready})"
    )

    jackpot_threshold = max(
        float(Vars.min_power_threshold) * float(getattr(Vars, 'jackpot_multiplier', 3)),
        current_threshold * float(getattr(Vars, 'jackpot_threshold_factor', 1.5)),
    )

    try:
        rollCommand = SlashCommander(bot.getSlashCommands(botID).json()).get([Vars.rollCommand])
    except Exception as e:
        log_msg('WARN', f"Не удалось получить слэш-команду. Проверь токен: {e}")
        return

    rolls_left = int(profile_state.get('rolls_left', 0) or 0)
    if rolls_left == 0 and not bonus_us:
        log_msg('ROLL', f"Роллов нет ({account.label}, rolls_left=0). Сессия прокрутов пропущена.")
        if getattr(Vars, 'pokeRoll', False):
            _send_pokeslot()
        return 0

    begin_roll_session(
        profile_state,
        current_threshold,
        jackpot_threshold,
        bonus_us=bonus_us,
        roller_key=account.key,
        stop_before=stop_before,
    )
    rolls_done = 0
    first_slash = None
    if max_rolls is None:
        max_rolls = int(getattr(Vars, 'max_rolls_per_session', 150))
    max_rolls = max(1, int(max_rolls))
    try:
        while rolls_done < max_rolls:
            if not wait_channel_quiet(
                deadline=stop_before,
                max_wait=None if stop_before else 20 * 60,
            ):
                log_msg('ROLL', 'Сессия остановлена: дальше ресет роллов.')
                break
            if roll_session_limit_reached():
                process_roll_claim_buffer(force=True)
                if not bonus_us and _try_use_rolls(profile_state, context='лимит в сессии'):
                    with _ROLL_LOCK:
                        _ROLL_SESSION['limit_reached'] = False
                    log_msg('ROLL', "Продолжаю сессию после $rolls.")
                    continue
                break

            process_roll_claim_buffer(force=False)
            action_pause()
            LAST_OUR_ROLL_TIME = time.time()
            if first_slash is None:
                first_slash = time.monotonic()
            try:
                account.client.triggerSlashCommand(
                    botID, Vars.channelId, Vars.serverId, data=rollCommand,
                )
            except Exception as error:
                log_msg('WARN', f"Не удалось отправить прокрут: {error}")
                break
            rolls_done += 1
            note_roll_sent()
            roll_pause()

        _settle_roll_session()
    finally:
        end_roll_session()

    elapsed = (time.monotonic() - first_slash) if first_slash is not None else 0
    log_msg(
        'ROLL',
        f"Сессия {account.label} завершена. Команд: {rolls_done}"
        + (f", заняла {elapsed:.0f} с." if elapsed else "."),
    )

    if getattr(Vars, 'pokeRoll', False) and not bonus_us:
        _send_pokeslot()
    return elapsed if rolls_done else 0
