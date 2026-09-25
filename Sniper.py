import threading
import time

import Vars
import Function
from Function import (
    PROFILE_STATE,
    bot,
    botID,
    log_msg,
    handle_mudae_chat,
    _drop_log,
    parse_card_embed,
    kakera_on_cooldown,
    kakera_cooldown_left_seconds,
    click_desired_kakera,
    human_sleep,
)

def _our_roll_ignore_window():
    return float(getattr(Vars, 'snipe_roll_ignore_window', 2.5))


def _watch_channel_ids():
    """Каналы для парсинга чата Mudae: основной + снайп."""
    ids = {str(Vars.channelId)}
    for cid in getattr(Vars, 'snipeChannelIds', []) or []:
        ids.add(str(cid))
    return ids


def _snipe_ids():
    return {str(cid) for cid in Vars.snipeChannelIds}


def _snipe_claim_threshold():
    """Порог из кэша $tu (тот же, что использует simpleRoll)."""
    cached = PROFILE_STATE.get('current_threshold')
    if cached is not None:
        try:
            return float(cached)
        except (TypeError, ValueError):
            pass
    return float(getattr(Vars, 'min_power_threshold', 80))


def _claim_reaction(channel_id, message_id, message=None, allow_rt=False):
    """Снайп-клейм через общую логику Function._claim_card (кнопка → реакция)."""
    # Гарантируем channel_id в объекте сообщения для корректного клика/реакции
    msg = message
    if isinstance(msg, dict) and not msg.get('channel_id'):
        msg = dict(msg)
        msg['channel_id'] = channel_id
    elif msg is None:
        msg = {'id': message_id, 'channel_id': channel_id}

    return Function._claim_card(
        message_id,
        f"снайп в канале {channel_id}",
        PROFILE_STATE,
        message=msg,
        allow_rt=allow_rt,
    )


def _snipe_kakera(message):
    """Жмёт кнопки какеры на чужих роллах (если нет КД)."""
    if kakera_on_cooldown():
        log_msg(
            'KAKER',
            f"Снайп какеры пропущен: КД ещё {kakera_cooldown_left_seconds()} сек.",
            dim=True,
        )
        return

    click_desired_kakera(message, log_prefix='СНАЙП КАКЕРЫ')


def _safe_subscribe_guild_events():
    """OP14 lazy guild: подписка на каналы роллов/снайпа без ожидания guilds в READY.

    Современный Discord часто не кладёт полный список guilds в READY при урезанных
    capabilities — старый wait по settings_ready['guilds'] всегда фейлился.
    """
    # READY=True выставляется только на READY_SUPPLEMENTAL
    for _ in range(20):
        if getattr(bot.gateway, 'READY', False):
            break
        time.sleep(0.4)
    else:
        log_msg('WARN', "Gateway READY так и не поднялся — пробую lazyGuild всё равно.")

    time.sleep(0.6)

    for attempt in range(8):
        try:
            channel_ranges = {
                str(cid): [[0, 99]] for cid in _watch_channel_ids()
            }
            if not channel_ranges:
                channel_ranges = {str(Vars.channelId): [[0, 99]]}

            bot.gateway.request.lazyGuild(
                str(Vars.serverId),
                channel_ranges,
                typing=True,
                threads=True,
                activities=False,
                members=[],
                thread_member_lists=[],
            )
            log_msg(
                'SYS',
                f"Lazy guild OK: сервер {Vars.serverId}, каналов {len(channel_ranges)}.",
                dim=True,
            )

            # Опционально — полная подписка, если discum уже разобрал guilds
            settings = getattr(bot.gateway.session, 'settings_ready', None) or {}
            if settings.get('guilds'):
                try:
                    bot.gateway.subscribeToGuildEvents(onlyLarge=False, wait=0.25)
                except Exception as error:
                    log_msg('WARN', f"subscribeToGuildEvents: {error}", dim=True)
            return
        except Exception as error:
            log_msg(
                'WARN',
                f"lazyGuild попытка {attempt + 1}/8: {error}",
                dim=True,
            )
            time.sleep(1.2)

    log_msg(
        'WARN',
        "Не удалось отправить lazyGuild — сообщения из больших серверов могут не приходить.",
    )


@bot.gateway.command
def sniper(resp):
    """Слушает MESSAGE_CREATE / MESSAGE_UPDATE: чат-парсер + снайп карт/какеры."""
    # Подписка только после READY_SUPPLEMENTAL (тогда gateway.READY=True)
    if resp.event.ready_supplemental:
        log_msg('SYS', "Gateway READY — подписываюсь на события гильдий...", dim=True)
        threading.Thread(
            target=_safe_subscribe_guild_events,
            daemon=True,
            name='GuildSubscribe',
        ).start()
        return

    if resp.event.ready:
        return

    if not (resp.event.message or resp.event.message_updated):
        return

    if resp.event.message:
        message = resp.parsed.message_create()
    else:
        message = resp.raw.get('d') or {}

    author = message.get('author') or {}
    if str(author.get('id', '')) != str(botID):
        return

    channel_id = str(message.get('channel_id', ''))
    if channel_id not in _watch_channel_ids():
        return

    # Реактивный парсер подтверждений (пока Gateway жив)
    handle_mudae_chat(message.get('content', ''), message_id=message.get('id'))

    if not getattr(Vars, 'snipeEnabled', False):
        return

    if channel_id not in _snipe_ids():
        return

    # Свой ролл из simpleRoll — не перехватываем карту
    if time.time() - Function.LAST_OUR_ROLL_TIME < _our_roll_ignore_window():
        return

    embeds = message.get('embeds') or []
    components = message.get('components') or []

    if components:
        _snipe_kakera(message)

    if not embeds:
        return

    embed = embeds[0]
    card_name, card_series, card_power, is_claimed = parse_card_embed(embed)

    _drop_log(
        card_name,
        card_series,
        card_power,
        is_claimed,
        message_id=message.get('id'),
    )
    if is_claimed:
        return

    threshold = _snipe_claim_threshold()
    jackpot_threshold = float(Vars.min_power_threshold) * float(
        getattr(Vars, 'jackpot_multiplier', 3)
    )
    # Ценная карта: серия ИЛИ цена >= текущего порога (не только джекпот!)
    valuable = (
        card_series in Vars.desiredSeries
        or card_power >= threshold
        or card_power >= jackpot_threshold
    )
    if not valuable:
        # Лог только для «почти ценных», чтобы не спамить чат
        if card_power >= float(Vars.min_power_threshold) * 0.75:
            log_msg(
                'CLAIM',
                f"Снайп пропуск: {card_name} цена {card_power} < порог {threshold:g}.",
                dim=True,
            )
        return

    message_id = message.get('id')
    if not message_id:
        return

    log_msg(
        'CLAIM',
        f"СНАЙПИНГ! {card_name} (цена {card_power}, порог {threshold:g}) "
        f"в канале {channel_id}!",
        dim=False,
    )

    # Небольшая «реакция человека», без большого риска потерять карту
    human_sleep(0.2, 0.65)
    _claim_reaction(
        channel_id,
        message_id,
        message=message,
        allow_rt=card_power >= jackpot_threshold,
    )
