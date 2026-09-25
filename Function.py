import discum
import json
import random
import re
import time
import requests
import Vars
from discum.utils.slash import SlashCommander

import WebDashboard


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

    kind — тип паузы для UI/логов (roll, action, schedule, …).
    log — True/False; по умолчанию логируем всё, кроме action/poll.
    """
    delay = human_delay(lo, hi)
    should_log = log if log is not None else kind not in (None, 'action', 'poll')
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

botID = '432610292342587392' # ID бота Mudae
auth = {'authorization': Vars.token}
bot = discum.Client(token=Vars.token, log=False)
# User-клиент discum: урезаем identify (без лишних activities) — слушаем сообщения.
bot.gateway.auth['presence'] = {
    'status': 'online',
    'since': 0,
    'activities': [],
    'afk': False,
}
# capabilities: базовый набор без тяжёлых guild-features (discum default=509)
bot.gateway.auth['capabilities'] = int(getattr(Vars, 'gateway_capabilities', 253))

url = f'https://discord.com/api/v9/channels/{Vars.channelId}/messages'

# Время последнего собственного слэш-ролла (чтобы Sniper не перехватывал ответ Mudae)
LAST_OUR_ROLL_TIME = 0.0

# Локальный кэш профиля из $tu — для снайпера и порога клейма
PROFILE_STATE = {
    'can_claim': False,
    'rt_ready': False,
    'claim_minutes_left': 0,
    'rolls_left': 0,
    'current_threshold': float(getattr(Vars, 'min_power_threshold', 80)),
    'kakera_power': 0,
    'kakera_cost': 0,
    'kakera_ready': True,
}

# Не кликать какеру до этого unix-timestamp (после отказа Mudae)
KAKERA_COOLDOWN_UNTIL = 0.0

# Дедуп логов DROP и начислений по message id
_processed_message_ids = {}

# Кэш Discord-имён (display + login)
_CACHED_USERNAME = None
_CACHED_USERNAMES = None

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


def kakera_on_cooldown():
    return time.time() < KAKERA_COOLDOWN_UNTIL


def kakera_cooldown_left_seconds():
    return max(0, int(KAKERA_COOLDOWN_UNTIL - time.time()))


# Ожидающий клейм (карточка до подтверждения «married»)
_PENDING_CLAIM = None


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
    """$dk при откате реакции какеры (если $dk готов по кэшу профиля)."""
    if not PROFILE_STATE.get('dk_ready'):
        return False
    log_msg('KAKER', "Какера в КД — пробую $dk...", dim=True)
    if _send_text_command('$dk') not in [200, 204]:
        return False
    log_msg('KAKER', '$dk отправлен (после КД какеры).')
    PROFILE_STATE['dk_ready'] = False
    PROFILE_STATE['dk_minutes_left'] = 0
    sync_profile_state(PROFILE_STATE)
    return True


def _resolve_username():
    """Имя аккаунта: Vars.username или GET /users/@me."""
    names = _our_discord_names()
    return names[0] if names else ''


def _our_discord_names():
    """Возможные имена аккаунта (display / username / Vars.username)."""
    global _CACHED_USERNAME, _CACHED_USERNAMES
    names = []
    configured = (getattr(Vars, 'username', None) or '').strip()
    if configured:
        names.append(configured)

    if _CACHED_USERNAMES is None:
        fetched = []
        try:
            response = requests.get(
                'https://discord.com/api/v9/users/@me', headers=auth, timeout=8
            )
            if response.status_code == 200:
                data = response.json()
                display = (data.get('global_name') or '').strip()
                login = (data.get('username') or '').strip()
                if display:
                    fetched.append(display)
                if login:
                    fetched.append(login)
                _CACHED_USERNAME = display or login or ''
        except requests.RequestException:
            pass
        _CACHED_USERNAMES = fetched

    names.extend(_CACHED_USERNAMES or [])
    if _CACHED_USERNAME:
        names.append(_CACHED_USERNAME)

    seen = set()
    result = []
    for name in names:
        key = name.lower()
        if key and key not in seen:
            seen.add(key)
            result.append(name)
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


def handle_mudae_chat(content, message_id=None):
    """Реактивный парсер ответов Mudae: КД какеры, начисления, свадьбы.

    Возвращает dict с распознанным событием или None.
    """
    global KAKERA_COOLDOWN_UNTIL, _PENDING_CLAIM

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
        if cd_name and not _is_our_name(cd_name):
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
        KAKERA_COOLDOWN_UNTIL = time.time() + minutes * 60
        log_msg(
            'KAKER',
            f"Какера в откате на {minutes} мин "
            f"(до {time.strftime('%H:%M:%S', time.localtime(KAKERA_COOLDOWN_UNTIL))}).",
        )
        try_use_dk_for_kakera_cd()
        return {'type': 'kakera_cooldown', 'minutes': minutes}

    # Явный маркер ($ku) без стандартной фразы — всё равно ставим КД
    if '$ku' in lower and 'kakera' in lower:
        if not _once('kakera_cd'):
            return None
        minutes = 60
        time_bits = re.search(
            r'(?:(\d+)\s*h\s*)?(?:(\d+)\s*min)', lower
        )
        if time_bits:
            minutes = int(time_bits.group(1) or 0) * 60 + int(time_bits.group(2) or 0)
        if minutes > 0:
            KAKERA_COOLDOWN_UNTIL = time.time() + minutes * 60
            log_msg(
                'KAKER',
                f"Какера в откате на {minutes} мин ($ku) "
                f"(до {time.strftime('%H:%M:%S', time.localtime(KAKERA_COOLDOWN_UNTIL))}).",
            )
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
    log_msg(
        'SYS',
        f"Профиль: Клейм {claim_status}, $rt {_util_status(state.get('rt_ready'))}, "
        f"$daily {_util_status(state.get('daily_ready'))}, "
        f"$dk {_util_status(state.get('dk_ready'))}, "
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

def _send_text_command(command):
    """Отправляет обычную текстовую команду Mudae и возвращает код ответа."""
    action_pause()
    try:
        response = requests.post(
            url=url, headers=auth, json={'content': command}, timeout=10
        )
    except requests.RequestException as error:
        log_msg('WARN', f"Не удалось отправить {command}: {error}")
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
    """Обновляет локальный кэш PROFILE_STATE и пушит в Web UI."""
    global PROFILE_STATE
    if not state:
        return PROFILE_STATE
    for key in (
        'can_claim', 'rt_ready', 'claim_minutes_left', 'rolls_left', 'rolls_resets',
        'rolls_reset_minutes', 'kakera_power', 'kakera_cost', 'kakera_ready',
        'current_threshold', 'rt_minutes_left', 'daily_ready', 'daily_minutes_left',
        'dk_ready', 'dk_minutes_left', 'claim_ttl',
    ):
        if key in state:
            PROFILE_STATE[key] = state[key]
    if 'current_threshold' not in state:
        PROFILE_STATE['current_threshold'] = compute_claim_threshold(
            PROFILE_STATE.get('claim_minutes_left', 0)
        )
    WebDashboard.emit_profile_update(dict(PROFILE_STATE))
    return PROFILE_STATE


def _rolls_worth_spinning(profile_state):
    """Есть смысл тратить $rolls: клейм доступен или реакция какеры не в КД."""
    if profile_state.get('can_claim'):
        return True
    # Живой таймер: в сессии КД мог измениться после кликов по какере
    return not kakera_on_cooldown()


def _try_use_rolls(profile_state, *, context=''):
    """Отправляет $rolls, если есть сбросы в запасе и есть смысл крутить дальше."""
    rolls_resets = int(profile_state.get('rolls_resets', 0) or 0)
    if rolls_resets <= 0:
        return False

    if not _rolls_worth_spinning(profile_state):
        suffix = f" ({context})" if context else ''
        log_msg(
            'ROLL',
            f"$rolls пропущен{suffix}: "
            f"клейм={'да' if profile_state.get('can_claim') else 'нет'}, "
            f"какера={'готова' if not kakera_on_cooldown() else 'в КД'} "
            f"(таймер КД {kakera_cooldown_left_seconds()} сек).",
            dim=True,
        )
        return False

    suffix = f" — {context}" if context else ''
    log_msg('ROLL', f"Использую $rolls{suffix}.")
    if _send_text_command('$rolls') not in [200, 204]:
        return False

    profile_state['rolls_reset_used'] = True
    profile_state['rolls_resets'] = rolls_resets - 1
    profile_state['rolls_left'] = max(
        1, int(getattr(Vars, 'max_rolls_per_session', 150))
    )
    sync_profile_state(profile_state)
    human_sleep(1.0, 2.0)
    return True


def updateProfileState(_refreshed_after_dk=False, skip_actions=False):
    """Запрашивает $tu, обновляет состояние профиля и запускает доступные действия."""
    last_message_id = _get_latest_message_id()
    log_msg('SYS', "Запрашиваю состояние профиля ($tu)...", dim=True)
    if _send_text_command('$tu') not in [200, 204]:
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

    # КД реакции какеры из $tu — источник истины (не глобальный таймер из чата)
    global KAKERA_COOLDOWN_UNTIL
    kakera_cd = re.search(
        r"you can't react to kakera for\s+(?:(\d+)\s*h\s*)?(\d+)\s*min",
        parsed_text,
        re.IGNORECASE,
    )
    # Явная готовность: "You can react to kakera right now!"
    kakera_ready_tu = bool(
        re.search(r"you\s+can\s+react\s+to\s+kakera\b", lower_text, re.IGNORECASE)
    )
    if kakera_ready_tu:
        KAKERA_COOLDOWN_UNTIL = 0.0
        kakera_ready = True
    elif kakera_cd:
        minutes = int(kakera_cd.group(1) or 0) * 60 + int(kakera_cd.group(2) or 0)
        if minutes > 0:
            KAKERA_COOLDOWN_UNTIL = time.time() + minutes * 60
        kakera_ready = False
    elif "can't react to kakera" in lower_text:
        # Фраза КД есть, но время не распарсили — не сбрасываем таймер
        kakera_ready = False
    else:
        # В $tu нет строки про реакцию — опираемся на локальный таймер
        kakera_ready = not kakera_on_cooldown()
        if kakera_ready:
            KAKERA_COOLDOWN_UNTIL = 0.0

    state = {
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
        'kakera_power': extract_int(r"Power:\s*(\d+)%"),
        'kakera_cost': extract_int(r"consumes\s+(\d+)%\s+of your reaction power"),
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
        if _send_text_command('$daily') in [200, 204]:
            log_msg('SYS', '🎁 Ежедневная награда ($daily) успешно забрана!')

    # $dk — только если какера в КД реакции (не по «энергии»)
    if (
        state['dk_ready'] is True
        and not kakera_ready
        and not _refreshed_after_dk
    ):
        log_msg('KAKER', "$dk доступен и какера в КД. Отправляю $dk.", dim=True)
        if _send_text_command('$dk') in [200, 204]:
            log_msg('KAKER', '$dk отправлен.')
            KAKERA_COOLDOWN_UNTIL = 0.0
            return updateProfileState(_refreshed_after_dk=True)

    # $rolls: есть смысл крутить, если доступен клейм ИЛИ реакция какеры ($tu)
    if state['rolls_left'] == 0 and state['rolls_resets'] > 0:
        _try_use_rolls(state, context='профиль $tu')

    return state

def _ensure_claim_ready(profile_state, allow_rt=False):
    """Проверяет доступность клейма.

    $rt тратим только на джекпот: обычные карты при КД клейма пропускаем.
    """
    if profile_state.get('can_claim', False):
        return True

    if not allow_rt:
        log_msg('WARN', "Клейм в откате. $rt бережём для джекпота — реакцию не отправляю.")
        return False

    if profile_state.get('rt_ready') is not True:
        log_msg('WARN', "Джекпот, но клейм в откате и $rt недоступен. Реакция не отправлена.")
        return False

    log_msg('CLAIM', "Джекпот при КД клейма: $rt доступен, отправляю $rt перед клеймом.", dim=True)
    if _send_text_command('$rt') in [200, 204]:
        # Команда принята Discord; не отправляем второй $rt в этой сессии.
        profile_state['can_claim'] = True
        profile_state['rt_ready'] = False
        sync_profile_state(profile_state)
        return True

    return False


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
KAKERA_VALUES = {
    'kakeraW': 8,
    'kakeraL': 7,
    'kakeraR': 6,
    'kakeraO': 5,
    'kakeraY': 4,
    'kakeraT': 3,
    'kakeraB': 2,
    'kakeraP': 1,
}


def _is_kakera_button(button):
    name = (button.get('emoji') or {}).get('name') or ''
    desired = getattr(Vars, 'desiredKakeras', []) or []
    return name in desired or name.lower().startswith('kakera')


def _kakera_targets(message):
    """Включённые кнопки из desiredKakeras, дорогие первыми."""
    desired = getattr(Vars, 'desiredKakeras', []) or []
    targets = []
    for button in _iter_message_buttons(message.get('components')):
        emoji = (button.get('emoji') or {}).get('name') or ''
        if emoji in desired:
            targets.append((emoji, button))
    targets.sort(key=lambda item: KAKERA_VALUES.get(item[0], 0), reverse=True)
    return targets


def _kakera_signature(message):
    names = []
    for emoji, button in _kakera_targets(message):
        names.append(f"{emoji}:{button.get('custom_id', '')}")
    return tuple(names)


def _refresh_kakera_message(message):
    channel_id = message.get('channel_id') or Vars.channelId
    fetched = _fetch_channel_message(channel_id, message.get('id'), quiet=True)
    return fetched or message


def _wait_kakera_buttons_ready(message, *, timeout=1.15):
    """Ждёт, пока Mudae дорисует кнопки ($bku часто добавляет 3-ю какеру правкой)."""
    desc = ' '.join(
        (embed.get('description') or '')
        for embed in (message.get('embeds') or [])
    ).lower()
    expect_late_button = '$bku' in desc or bool(_kakera_targets(message))
    if not expect_late_button:
        return message

    deadline = time.monotonic() + timeout
    last_sig = _kakera_signature(message)
    last_change = time.monotonic()
    current = message

    while time.monotonic() < deadline:
        human_sleep(0.18, 0.28, kind='kakera', log=False)
        current = _refresh_kakera_message(current)
        sig = _kakera_signature(current)
        if sig != last_sig:
            last_sig = sig
            last_change = time.monotonic()
            log_msg(
                'KAKER',
                f"Кнопки какеры обновились: {len(sig)} шт. "
                f"({', '.join(s.split(':')[0] for s in sig)}).",
                dim=True,
            )
        elif last_sig and time.monotonic() - last_change >= 0.32:
            break
    return current


def click_desired_kakera(message, *, log_prefix='Найдена кнопка какеры'):
    """Жмёт какеры по одной, каждый раз с актуальными custom_id.

    Пачка кликов по первому снимку кнопки ломает 3-ю+: после $bku / клика
    Mudae правит сообщение, старые custom_id перестают работать, а новая
    какера появляется только в следующем GET.
    """
    if not message or kakera_on_cooldown():
        return 0

    limit = int(getattr(Vars, 'kakera_max_clicks', 8))
    if limit <= 0:
        limit = 32

    message = _wait_kakera_buttons_ready(message)
    if not _kakera_targets(message):
        return 0

    channel_id = message.get('channel_id') or Vars.channelId
    guild_id = message.get('guild_id') or Vars.serverId
    clicks = 0
    clicked_ids = set()
    last_poll_id = str(message['id'])

    while clicks < limit:
        if kakera_on_cooldown():
            break

        targets = [
            item for item in _kakera_targets(message)
            if item[1].get('custom_id') not in clicked_ids
        ]
        if not targets:
            human_sleep(0.2, 0.32, kind='kakera', log=False)
            message = _refresh_kakera_message(message)
            targets = [
                item for item in _kakera_targets(message)
                if item[1].get('custom_id') not in clicked_ids
            ]
            if not targets:
                break

        emoji, button = targets[0]
        custom_id = button.get('custom_id')
        remaining = len(targets)
        log_msg(
            'KAKER',
            f"{log_prefix}: {emoji}. "
            f"Пытаюсь взять какеру №{clicks + 1} "
            f"(осталось на карте: {remaining})...",
            dim=True,
        )
        try:
            human_sleep(0.08, 0.18, kind='kakera', log=False)
            response = bot.click(
                message['author']['id'],
                channelID=channel_id,
                guildID=guild_id,
                messageID=message['id'],
                messageFlags=message.get('flags', 0),
                data={'component_type': 2, 'custom_id': custom_id},
            )
            status = getattr(response, 'status_code', None)
            if status not in (200, 204, None):
                log_msg('WARN', f"Клик {emoji} отклонён (код: {status})")
                clicked_ids.add(custom_id)
                message = _refresh_kakera_message(message)
                continue
            clicks += 1
            clicked_ids.add(custom_id)
            log_msg('KAKER', f"Нажатие на {emoji} отправлено.")
        except Exception as error:
            log_msg('WARN', f"Ошибка нажатия: {error}")
            clicked_ids.add(custom_id)

        poll_mudae_chat_replies(
            last_poll_id,
            channel_id=channel_id,
            attempts=2,
            delay=human_delay(0.16, 0.28),
        )
        message = _refresh_kakera_message(message)

    if clicks:
        poll_mudae_chat_replies(
            last_poll_id,
            channel_id=channel_id,
            attempts=2,
            delay=human_delay(0.2, 0.35),
        )
    return clicks


def find_claim_button(components):
    """Ищет кнопку клейма среди components (сердце / claim / первая не-kakera)."""
    buttons = list(_iter_message_buttons(components))
    if not buttons:
        return None

    for button in buttons:
        if _is_kakera_button(button):
            continue
        emoji_name = ((button.get('emoji') or {}).get('name') or '').lower()
        label = (button.get('label') or '').lower()
        custom_id = (button.get('custom_id') or '').lower()
        blob = f'{emoji_name} {label} {custom_id}'
        if any(hint in blob for hint in _CLAIM_HINTS):
            return button

    for button in buttons:
        if not _is_kakera_button(button):
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
    log_msg(
        'CLAIM',
        f"Клейм через кнопку ({emoji_name}, custom_id={button.get('custom_id')[:24]}...).",
        dim=True,
    )
    action_pause()
    try:
        response = bot.click(
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
            headers=auth,
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
    if not _ensure_claim_ready(profile_state, allow_rt=allow_rt):
        return False

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

    msg = message if isinstance(message, dict) else None
    resolved_channel = channel_id or Vars.channelId
    if msg:
        resolved_channel = msg.get('channel_id') or resolved_channel
        message_id = msg.get('id') or message_id
        if components is None:
            components = msg.get('components')

    # Если components нет — подтянем свежее сообщение с API
    if not components and message_id:
        fetched = _fetch_channel_message(resolved_channel, message_id)
        if fetched:
            msg = fetched
            components = fetched.get('components')
            resolved_channel = fetched.get('channel_id') or resolved_channel

    claim_button = find_claim_button(components)
    success = False
    if claim_button and msg:
        success = _claim_via_button(msg, claim_button)
        if not success:
            log_msg('WARN', "Клик по кнопке клейма не удался — пробую реакцию.")
            success = _claim_via_reaction(message_id, channel_id=resolved_channel)
    else:
        success = _claim_via_reaction(message_id, channel_id=resolved_channel)

    if success:
        log_msg('CLAIM', "УСПЕХ: Клейм принят Discord API.")
        profile_state['can_claim'] = False
        sync_profile_state(profile_state)
        WebDashboard.emit_buffer_clear()
        return True
    return False

def _send_pokeslot():
    """Отправляет $p. Сбой сети не должен ронять поток расписания."""
    log_msg('ROLL', "Пытаюсь крутить Pokeslot ($p)...", dim=True)
    try:
        poke_req = requests.post(
            url=url, headers=auth, json={'content': '$p'}, timeout=15
        )
    except requests.RequestException as error:
        log_msg('WARN', f"Pokeslot: соединение сброшено ({error})")
        return
    if poke_req.status_code in [200, 204]:
        log_msg('ROLL', "Pokeslot успешно отправлен.")
    else:
        log_msg('WARN', f"Не удалось отправить Pokeslot (код: {poke_req.status_code})")


def simpleRoll():
    global LAST_OUR_ROLL_TIME
    set_rolling(True)
    log_msg('ROLL', f"Начинаю сессию прокрутов: /{Vars.rollCommand}")

    try:
        _simpleRoll_body()
    finally:
        set_rolling(False)


def _simpleRoll_body():
    global LAST_OUR_ROLL_TIME
    profile_state = updateProfileState()
    if profile_state is None:
        # При отсутствии ответа $tu не рискуем клеймом и используем базовый порог.
        profile_state = {'can_claim': False, 'rt_ready': False, 'claim_minutes_left': 0}
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

    # Динамическая формула джекпота (защищена от пересечения с текущим порогом)
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
    if rolls_left == 0:
        log_msg('ROLL', "Роллов нет (rolls_left=0). Сессия прокрутов пропущена.")
        if getattr(Vars, 'pokeRoll', False):
            _send_pokeslot()
        return

    limit_reached = False
    rolls_done = 0
    claim_buffer = []
    claim_spent = False

    def process_claim_buffer(force=False):
        """Клеймит лучшую карточку в буфере, когда истекает её безопасный TTL."""
        nonlocal claim_spent
        if claim_spent:
            claim_buffer.clear()
            WebDashboard.emit_buffer_clear()
            return
        if not claim_buffer:
            return

        time_elapsed = time.time() - claim_buffer[0]['timestamp']
        claim_ttl = get_claim_ttl()
        if not force and time_elapsed < claim_ttl:
            return

        trigger = "достигнут лимит роллов" if force else f"сработал таймер {claim_ttl} сек"
        best_card = max(claim_buffer, key=lambda card: card['power'])
        log_msg('CLAIM',
            f"Буфер: {trigger}; старшей карте {time_elapsed:.1f} сек. "
            f"Выбрана лучшая карта (цена персонажа {best_card['power']}) "
            f"из {len(claim_buffer)}."
        )
        claim_spent = _claim_card(
            best_card['msg_id'],
            f"снайпинг из буфера (цена {best_card['power']})",
            profile_state,
            message=best_card.get('message'),
            components=best_card.get('components'),
            card_name=best_card.get('name') or '',
            card_series=best_card.get('series') or '',
            card_power=best_card.get('power') or 0,
        )
        claim_buffer.clear()
        WebDashboard.emit_buffer_clear()

    # ИЗМЕНЕНИЕ ЗДЕСЬ: лимит-предохранитель сессии роллов
    max_rolls = int(getattr(Vars, 'max_rolls_per_session', 150))
    while not limit_reached and rolls_done < max_rolls:
        process_claim_buffer()

        # 1. Получаем ID самого свежего сообщения ДО прокрута
        last_msg_id = 0
        r_before = requests.get(f"{url}?limit=1", headers=auth)
        if r_before.status_code == 200 and len(r_before.json()) > 0:
            last_msg_id = int(r_before.json()[0]['id'])

        # 2. Отправляем команду прокрута
        log_msg('ROLL', f"#{rolls_done + 1}: отправляю Slash-команду прокрута...", dim=True)
        action_pause()
        LAST_OUR_ROLL_TIME = time.time()
        bot.triggerSlashCommand(botID, Vars.channelId, Vars.serverId, data=rollCommand)
        
        # 3. Активный поллинг (Безопасное ожидание)
        mudae_msg = None
        attempts = 0
        
        while attempts < 4:  # ~3–5 сек с джиттером
            human_sleep(0.7, 1.15)

            # Запрашиваем только новые сообщения, появившиеся после last_msg_id
            r = requests.get(f"{url}?after={last_msg_id}&limit=5", headers=auth)
            
            if r.status_code == 200:
                messages = r.json()
                mudae_msg = next((m for m in messages if m.get('author', {}).get('id') == botID), None)
                
                if mudae_msg:
                    break # Сообщение найдено, выходим из ожидания
            elif r.status_code == 429:
                wait_time = r.json().get('retry_after', 1.0)
                log_msg('WARN', f"Discord просит притормозить (Rate Limit). Ждем {wait_time} сек...")
                time.sleep(wait_time)
            
            attempts += 1

        # 4. Проверяем, ответила ли Mudae вообще
        if not mudae_msg:
            log_msg('WARN', "Mudae не ответила за 4 секунды (возможно лаг). Идем дальше.")
            process_claim_buffer()
            continue

        # ПРОВЕРКА НА ЛИМИТ ПРОКРУТОВ / чат-события
        msg_content = mudae_msg.get('content', '')
        handle_mudae_chat(msg_content, message_id=mudae_msg.get('id'))
        if "the roulette is limited to" in msg_content.lower():
            log_msg('WARN', "Mudae сообщила о лимите прокрутов. Прокруты закончились!")
            profile_state['rolls_left'] = 0
            process_claim_buffer(force=True)
            if _try_use_rolls(profile_state, context='лимит в сессии'):
                log_msg('ROLL', "Продолжаю сессию после $rolls.")
                continue
            limit_reached = True
            break

        # РАБОТА С КАРТОЧКОЙ (EMBED)
        embeds = mudae_msg.get('embeds', [])
        if not embeds:
            process_claim_buffer()
            continue

        embed = embeds[0]
        cardName, cardSeries, cardPower, is_claimed = parse_card_embed(embed)

        _drop_log(cardName, cardSeries, cardPower, is_claimed, message_id=mudae_msg.get('id'))
        apply_key_kakera_bonus(embed, message_id=mudae_msg.get('id'))

        # ЛОГИКА КЛЕЙМА И ОТЛОЖЕННОГО СНАПИНГА
        # Джекпот проверяем даже после обычного клейма: тогда тратим $rt.
        # claim_spent не должен глушить эту ветку — иначе джекпот просто игнорируется.
        if not is_claimed:
            is_jackpot = cardPower >= jackpot_threshold
            if is_jackpot:
                if _claim_card(
                    mudae_msg['id'],
                    f"джекпот цена {cardPower} >= {jackpot_threshold:g}",
                    profile_state,
                    message=mudae_msg,
                    card_name=cardName,
                    card_series=cardSeries,
                    card_power=cardPower,
                    allow_rt=True,
                ):
                    claim_spent = True
            elif not claim_spent:
                if cardSeries in Vars.desiredSeries:
                    claim_spent = _claim_card(
                        mudae_msg['id'],
                        f"совпадение по серии: '{cardSeries}'",
                        profile_state,
                        message=mudae_msg,
                        card_name=cardName,
                        card_series=cardSeries,
                        card_power=cardPower,
                    )
                elif cardPower >= current_threshold:
                    claim_buffer.append({
                        'msg_id': mudae_msg['id'],
                        'power': cardPower,
                        'name': cardName,
                        'series': cardSeries,
                        'timestamp': time.time(),
                        'message': mudae_msg,
                        'components': mudae_msg.get('components'),
                    })
                    ttl = get_claim_ttl()
                    WebDashboard.emit_buffer_card({
                        'msg_id': mudae_msg['id'],
                        'power': cardPower,
                        'name': cardName,
                        'series': cardSeries,
                        'ttl_ms': ttl * 1000,
                        'claim_ttl': ttl,
                    })
                    log_msg('CLAIM',
                        f"Карта (цена персонажа {cardPower}) добавлена в буфер "
                        f"(порог {current_threshold:g}, размер: {len(claim_buffer)})."
                    )

        # ЛОГИКА КАКЕРЫ — клик по свежим custom_id; КД/начисления ловим из чата
        mudae_msg = _refresh_kakera_message(mudae_msg)
        if not kakera_on_cooldown():
            click_desired_kakera(mudae_msg)
        elif mudae_msg.get('components'):
            log_msg(
                'KAKER',
                f"Пропуск какеры: КД ещё {kakera_cooldown_left_seconds()} сек.",
                dim=True,
            )

        rolls_done += 1
        process_claim_buffer()
        roll_pause()

    log_msg('ROLL', f"Сессия прокрутов завершена. Сделано роллов: {rolls_done}")

    # ЛОГИКА POKESLOT
    if getattr(Vars, 'pokeRoll', False):
        _send_pokeslot()
