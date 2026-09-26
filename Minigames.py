"""Автоматизация миниигр Ourospheres Mudae."""

import itertools
import json
import random
import re
import time

import requests
import Vars
from Function import auth, bot, botID, url, log_msg, human_sleep, action_pause


def _mg_log(message):
    """Лог миниигр без rich-разметки."""
    clean = re.sub(r"\[[^\]]*\]", "", str(message))
    log_msg("SYS", clean)


# Названия цветов из $infospheres / отображения Mudae. 
sphere_values = {
    'Purple': 5,
    'Blue': 10,
    'Teal': 20,
    'Green': 35,
    'Yellow': 55,
    'Orange': 90,
    'Red': 150,
    'Rainbow': 500,
    'Light': 200,
    'Dark': 40,
    'Hidden': 0,
}

_OPEN_SPHERES = tuple(name for name in sphere_values if name != 'Hidden')
_CHEAP_SPHERES = {'Blue', 'Teal'}
# Dark/Hidden после клика могут остаться кнопкой (Light/Purple) — их надо дожать.
_TRANSFORM_TARGETS = frozenset({'Purple', 'Light', 'Dark', 'Rainbow'})

# Точный маппинг эмодзи Mudae на наши внутренние цвета
_SPHERE_ALIASES = {
    'spp': 'Purple',
    'spb': 'Blue',
    'spt': 'Teal',
    'spg': 'Green',
    'spy': 'Yellow',
    'spo': 'Orange',
    'sp': 'Red',
    'spw': 'Rainbow',
    'spl': 'Light',
    'spd': 'Dark',
    'spu': 'Hidden',

    # Резервный фоллбэк на полные названия
    'purple': 'Purple', 'blue': 'Blue', 'teal': 'Teal',
    'green': 'Green', 'yellow': 'Yellow', 'orange': 'Orange',
    'red': 'Red', 'rainbow': 'Rainbow', 'light': 'Light',
    'dark': 'Dark', 'hidden': 'Hidden', 'question': 'Hidden', 'sp0': 'Hidden'
}

_ALIAS_KEYS_BY_LEN = tuple(sorted(_SPHERE_ALIASES, key=len, reverse=True))


def _normalize_emoji_name(emoji_name):
    return (emoji_name or '').casefold().replace('_', '').replace('-', '').replace(' ', '')


def _sphere_from_emoji_name(emoji_name):
    """Цвет по имени custom-emoji (spP / spL / …)."""
    normalized = _normalize_emoji_name(emoji_name)
    if not normalized:
        return None
    if normalized in _SPHERE_ALIASES:
        return _SPHERE_ALIASES[normalized]
    for key in _ALIAS_KEYS_BY_LEN:
        if normalized.startswith(key):
            return _SPHERE_ALIASES[key]
    return None


def _components_snapshot(message):
    """Стабильное представление поля для сравнения после клика."""
    return json.dumps(message.get('components', []), sort_keys=True, ensure_ascii=False)


def _sphere_name(button):
    """Возвращает тип сферы по имени emoji Mudae (spP/spO/…)."""
    emoji = button.get('emoji') or {}
    emoji_name = emoji.get('name') or ''
    if not emoji_name:
        return 'Hidden'
    return _sphere_from_emoji_name(emoji_name) or 'Hidden'


def _button_at(message, x, y):
    """Кнопка в клетке (x, y) или None."""
    rows = message.get('components', [])
    if y < 1 or y > len(rows):
        return None
    buttons = rows[y - 1].get('components', [])
    if x < 1 or x > len(buttons):
        return None
    return buttons[x - 1]


def _content_delta(old_content, new_content):
    """Новый хвост content после клика (накопленный лог наград Mudae)."""
    old = old_content or ''
    new = new_content or ''
    if new.startswith(old):
        return new[len(old):]
    return new


def _parse_harvest_delta(delta_text):
    """Разбор добавленного Mudae-текста после одного клика."""
    plain = re.sub(r'[*_`~]', '', delta_text or '')
    result = {
        'free': bool(re.search(r'\(\s*free\s*\)', plain, re.IGNORECASE)),
        'turns_into': None,
        'broke_down': False,
        'emojis': [],
    }

    names = []
    for groups in re.findall(
        r'<:(sp[A-Za-z0-9]*):\d+>|:(sp[A-Za-z0-9]*):',
        delta_text or '',
        flags=re.IGNORECASE,
    ):
        names.append(next((g for g in groups if g), ''))

    for name in names:
        sphere = _sphere_from_emoji_name(name)
        if sphere and sphere != 'Hidden':
            result['emojis'].append(sphere)

    turns = re.search(
        r'turns?\s+into\s+<:(sp[A-Za-z0-9]*):\d+>|turns?\s+into\s+:(sp[A-Za-z0-9]*):',
        delta_text or '',
        flags=re.IGNORECASE,
    )
    if turns:
        result['turns_into'] = _sphere_from_emoji_name(next(g for g in turns.groups() if g))

    if re.search(r'breaks?\s+down', plain, re.IGNORECASE):
        result['broke_down'] = True

    # Purple + Free в одной порции — клик был бесплатным (даже если клетка была Hidden).
    if not result['free'] and 'Purple' in result['emojis']:
        if re.search(r'free', plain, re.IGNORECASE):
            result['free'] = True

    return result


def _clickable_buttons(message):
    """Кнопки поля, на которые можно нажать (не disabled)."""
    result = []
    for y, row in enumerate(message.get('components', []), start=1):
        for x, button in enumerate(row.get('components', []), start=1):
            if button.get('type') != 2 or button.get('disabled'):
                continue
            custom_id = button.get('custom_id')
            if not custom_id:
                continue
            result.append({
                'x': x,
                'y': y,
                'button': button,
                'sphere': _sphere_name(button),
            })
    return result


def _cell_still_clickable(message, x, y):
    """True, если клетка после клика всё ещё активна (Dark→Light/Purple и т.п.)."""
    button = _button_at(message, x, y)
    return (
        button is not None
        and button.get('type') == 2
        and not button.get('disabled')
    )


def _pick_harvest_target(buttons, prefer_cell, warned_unknown):
    """Выбор следующей клетки для $oh."""
    purple = [item for item in buttons if item['sphere'] == 'Purple']
    visible = [
        item for item in buttons
        if item['sphere'] in _OPEN_SPHERES and item['sphere'] != 'Purple'
    ]
    valuable = [item for item in visible if item['sphere'] not in _CHEAP_SPHERES]
    hidden = [item for item in buttons if item['sphere'] == 'Hidden']
    unknown = [item for item in buttons if item['sphere'] is None]

    for item in buttons:
        if item['sphere'] != 'Hidden':
            continue
        raw = (item['button'].get('emoji') or {}).get('name') or ''
        raw_norm = _normalize_emoji_name(raw)
        if (
            raw_norm
            and raw_norm not in ('spu', 'sp0', 'hidden', 'question')
            and raw_norm not in warned_unknown
        ):
            warned_unknown.add(raw_norm)
            _mg_log(
                f'[yellow]Неизвестная сфера emoji={raw!r} в '
                f'(x:{item["x"]}, y:{item["y"]}) — считаю Hidden.[/yellow]'
            )

    prefer_item = None
    if prefer_cell is not None:
        prefer_item = next(
            (item for item in buttons if (item['x'], item['y']) == prefer_cell),
            None,
        )

    if purple:
        target = random.choice(purple)
        reason = 'Purple — бесплатный дополнительный ход'
    elif prefer_item is not None:
        target = prefer_item
        reason = (
            f"трансформ {target['sphere']} "
            f"({sphere_values.get(target['sphere'], '?')}) после предыдущего клика"
        )
    elif valuable:
        target = max(valuable, key=lambda item: sphere_values[item['sphere']])
        reason = f"открытую {target['sphere']} ({sphere_values[target['sphere']]})"
    elif hidden or unknown:
        target = random.choice(hidden or unknown)
        reason = 'скрытую или неизвестную кнопку'
    elif visible:
        target = max(visible, key=lambda item: sphere_values[item['sphere']])
        reason = (
            f"лучшую из оставшихся дешёвых {target['sphere']} "
            f"({sphere_values[target['sphere']]})"
        )
    else:
        target = random.choice(buttons)
        reason = 'случайную доступную кнопку (тип сферы не распознан)'

    return target, reason


def _get_message(message_id):
    """Находит сообщение в доступной self-боту истории канала."""
    response = requests.get(url, headers=auth, params={'limit': 10}, timeout=5)
    response.raise_for_status()
    return next(
        (message for message in response.json() if str(message.get('id')) == str(message_id)),
        None,
    )


# В $oq цвет = сколько Purple среди 8 соседей. Всего 4 Purple; после 3 кликов 4-я → Red+.
_QUEST_PURPLE_COUNTS = {
    'Blue': 0,
    'Teal': 1,
    'Green': 2,
    'Yellow': 3,
    'Orange': 4,
}
_QUEST_PROBE_CELLS = ((1, 1), (1, 5), (5, 1), (5, 5), (3, 3))
_QUEST_TOTAL_PURPLES = 4
_QUEST_JACKPOT = frozenset({'Red', 'Rainbow', 'Light'})


def _wait_for_minigame_message(after_message_id, timeout=10):
    """Ожидает ответ Mudae на $oh/$oc/$oq: сетку 5x5 либо текст о кулдауне/исчерпании."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = requests.get(url, headers=auth, params={'limit': 25}, timeout=5)
            response.raise_for_status()
            for message in response.json():
                rows = message.get('components', [])
                if (
                    str(message.get('author', {}).get('id')) == botID
                    and int(message.get('id', 0)) > int(after_message_id)
                ):
                    if len(rows) == 5 and all(len(row.get('components', [])) == 5 for row in rows):
                        return message

                    content = message.get('content', '').casefold()
                    if "don't have enough" in content or "don't have any" in content:
                        return message
        except requests.RequestException as error:
            _mg_log(f'[yellow]Ошибка ожидания сетки: {error}[/yellow]')
        human_sleep(0.35, 0.75)
    return None


def _parse_cooldown_seconds(message):
    """Извлекает кулдаун миниигры из ответа Mudae (секунды) или None."""
    content = re.sub(r'[*_`~]', '', message.get('content', ''))
    lowered = content.casefold()

    # Попыток нет совсем (без refill-таймера) — ждём сутки.
    if "don't have any" in lowered:
        return 86400

    match = re.search(
        r"don't have enough\s+\$\w+.*?refill:\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*min)?",
        content,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None

    return (int(match.group(1) or 0) * 60 + int(match.group(2) or 0)) * 60


def _sphere_at(message, x, y):
    """Цвет сферы в клетке (x, y), включая уже нажатые (disabled) кнопки."""
    rows = message.get('components', [])
    if y < 1 or y > len(rows):
        return None
    buttons = rows[y - 1].get('components', [])
    if x < 1 or x > len(buttons):
        return None
    return _sphere_name(buttons[x - 1])


def _filter_possible_reds(possible_reds, cx, cy, color):
    """Сужает кандидатов Red по правилу открытого цвета в (cx, cy)."""
    if color == 'Red':
        return [(cx, cy)]

    kept = []
    for rx, ry in possible_reds:
        if (rx, ry) == (cx, cy):
            continue
        dx = abs(rx - cx)
        dy = abs(ry - cy)
        if color == 'Blue':
            if not (dx == 0 or dy == 0 or dx == dy):
                kept.append((rx, ry))
        elif color == 'Teal':
            if dx == 0 or dy == 0 or dx == dy:
                kept.append((rx, ry))
        elif color == 'Green':
            if dx == 0 or dy == 0:
                kept.append((rx, ry))
        elif color == 'Yellow':
            if dx == dy and dx > 0:
                kept.append((rx, ry))
        elif color == 'Orange':
            if max(dx, dy) == 1:
                kept.append((rx, ry))
        else:
            # Неизвестный/не-подсказочный цвет — кандидат не отбрасываем.
            kept.append((rx, ry))
    return kept


def _pick_chest_target(buttons, possible_reds, move_index):
    """Выбирает клетку для хода Ourochest (поиск Red)."""
    cells = {(item['x'], item['y']): item for item in buttons}

    if len(possible_reds) == 1:
        only = possible_reds[0]
        if only in cells:
            return cells[only], f'единственного кандидата Red {only}'

    if move_index == 0:
        corner = (1, 1)
        if corner in cells:
            return cells[corner], 'угловую клетку для первой подсказки'

    for coord in possible_reds:
        item = cells.get(coord)
        if item is not None and item['sphere'] in (None, 'Hidden'):
            return item, f'кандидата Red {coord}'

    for coord in possible_reds:
        if coord in cells:
            return cells[coord], f'доступного кандидата {coord}'

    hidden = [item for item in buttons if item['sphere'] in (None, 'Hidden')]
    if hidden:
        target = random.choice(hidden)
        return target, 'скрытую клетку (кандидаты недоступны)'

    return random.choice(buttons), 'случайную доступную кнопку'


def _chest_bonus_rank(rx, ry, x, y):
    """Приоритет клетки после найденной Red (меньше = лучше).

    Orange — ортогонально рядом, Yellow — диагональ, Green — ряд/столбец,
    Teal — ряд/столбец/диагональ дальше, иначе Blue (не берём).
    """
    dx = abs(rx - x)
    dy = abs(ry - y)
    if dx == 0 and dy == 0:
        return 99
    if (dx == 0 and dy == 1) or (dx == 1 and dy == 0):
        return 0  # Orange
    if dx == 1 and dy == 1:
        return 1  # Yellow
    if dx == 0 or dy == 0:
        return 2  # Green
    if dx == dy:
        return 3  # Teal (дальняя диагональ)
    return 50  # Blue — вне ряда/столбца/диагонали


_CHEST_BONUS_LABELS = {
    0: 'Orange (рядом с Red)',
    1: 'Yellow (диагональ к Red)',
    2: 'Green (ряд/столбец Red)',
    3: 'Teal (линия/диагональ Red)',
}


def _pick_chest_target_after_red(buttons, red_pos):
    """После Red: следующие ходы — лучшие сферы по геометрии (Orange → …)."""
    rx, ry = red_pos
    ranked = []
    for item in buttons:
        rank = _chest_bonus_rank(rx, ry, item['x'], item['y'])
        if rank >= 50:
            continue
        ranked.append((rank, item))

    if ranked:
        ranked.sort(key=lambda pair: (pair[0], pair[1]['y'], pair[1]['x']))
        rank, target = ranked[0]
        label = _CHEST_BONUS_LABELS.get(rank, 'бонусную клетку')
        return target, f'кандидат на {label}'

    hidden = [item for item in buttons if item['sphere'] in (None, 'Hidden')]
    if hidden:
        target = random.choice(hidden)
        return target, 'скрытую клетку (бонусных кандидатов не осталось)'

    return random.choice(buttons), 'случайную доступную кнопку'


def _neighbors(x, y):
    """Восемь соседних клеток в пределах поля 5x5."""
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            nx, ny = x + dx, y + dy
            if 1 <= nx <= 5 and 1 <= ny <= 5:
                yield (nx, ny)


def _board_map(message):
    """Полная карта поля {(x, y): sphere}, включая disabled-кнопки."""
    result = {}
    for y, row in enumerate(message.get('components', []), start=1):
        for x, button in enumerate(row.get('components', []), start=1):
            result[(x, y)] = _sphere_name(button)
    return result


def _log_full_board(message, title='Текущее поле Ourospheres'):
    """Печатает все 25 клеток текущего сообщения (и открытые, и Hidden)."""
    board = _board_map(message)
    _mg_log(f'[bold cyan]{title}:[/bold cyan]')
    for y in range(1, 6):
        row = []
        for x in range(1, 6):
            sphere = board.get((x, y)) or '?'
            row.append(f'{sphere[:10]:^12}')
        _mg_log(' | '.join(row))


def _analyze_quest_field(board, known_empty, known_purple):
    """Выводит пустые клетки и Purple по числам соседей (как сапёр, но цель — Purple)."""
    for coord, color in board.items():
        if color == 'Purple' or color in _QUEST_JACKPOT:
            known_purple.add(coord)
            known_empty.discard(coord)
        elif color in _QUEST_PURPLE_COUNTS or color == 'Dark':
            known_empty.add(coord)
            known_purple.discard(coord)

    while True:
        before_empty = len(known_empty)
        before_purple = len(known_purple)

        for (x, y), color in board.items():
            if color not in _QUEST_PURPLE_COUNTS:
                continue
            value = _QUEST_PURPLE_COUNTS[color]
            neigh = list(_neighbors(x, y))

            purple_neighbors = [
                c for c in neigh
                if c in known_purple or board.get(c) == 'Purple' or board.get(c) in _QUEST_JACKPOT
            ]
            hidden_neighbors = [
                c for c in neigh
                if board.get(c) == 'Hidden'
                and c not in known_empty
                and c not in known_purple
            ]

            # Все Purple вокруг уже учтены → оставшиеся Hidden пустые.
            if value == len(purple_neighbors):
                known_empty.update(hidden_neighbors)

            # Недостающие Purple обязаны быть среди Hidden-соседей.
            if value == len(hidden_neighbors) + len(purple_neighbors):
                known_purple.update(hidden_neighbors)
                known_empty.difference_update(hidden_neighbors)

        # 3 Purple известны → единственная оставшаяся Hidden не-empty — 4-я.
        if len(known_purple) == _QUEST_TOTAL_PURPLES - 1:
            candidates = [
                coord for coord, color in board.items()
                if color == 'Hidden'
                and coord not in known_purple
                and coord not in known_empty
            ]
            if len(candidates) == 1:
                known_purple.add(candidates[0])
                known_empty.discard(candidates[0])

        if len(known_empty) == before_empty and len(known_purple) == before_purple:
            break


def _quest_known_purple_cells(board, known_purple):
    """Клетки, которые уже точно Purple (открытые, джекпот или выведенные)."""
    confirmed = set(known_purple)
    for coord, color in board.items():
        if color == 'Purple' or color in _QUEST_JACKPOT:
            confirmed.add(coord)
    return confirmed


def _iter_valid_quest_purple_layouts(board, known_empty, known_purple):
    """Все расстановки оставшихся Purple, совместимые с открытыми цифрами.

    Yields: кортеж координат Hidden-клеток, куда ставится оставшийся Purple.
    Всего на поле ровно 4 Purple.
    """
    confirmed = _quest_known_purple_cells(board, known_purple)
    remaining = _QUEST_TOTAL_PURPLES - len(confirmed)
    if remaining < 0:
        return

    hidden_unknown = [
        coord for coord, color in board.items()
        if color == 'Hidden'
        and coord not in known_empty
        and coord not in confirmed
    ]
    if remaining > len(hidden_unknown):
        return

    constraints = []
    for (x, y), color in board.items():
        if color not in _QUEST_PURPLE_COUNTS:
            continue
        need = _QUEST_PURPLE_COUNTS[color]
        neigh = list(_neighbors(x, y))
        already = sum(1 for cell in neigh if cell in confirmed)
        unknown_near = [cell for cell in neigh if cell in hidden_unknown]
        still_need = need - already
        if still_need < 0 or still_need > len(unknown_near):
            return
        constraints.append((unknown_near, still_need))

    for combo in itertools.combinations(hidden_unknown, remaining):
        placed = set(combo)
        valid = True
        for unknown_near, still_need in constraints:
            got = sum(1 for cell in unknown_near if cell in placed)
            if got != still_need:
                valid = False
                break
        if valid:
            yield combo


def _quest_purple_probabilities(board, known_empty, known_purple):
    """Частота Purple в каждой скрытой клетке по всем валидным расстановкам.

    Returns:
        counts: {(x, y): сколько раз клетка была Purple}
        total: число валидных расстановок (0 — доска противоречива)
    """
    counts = {}
    total = 0
    for combo in _iter_valid_quest_purple_layouts(board, known_empty, known_purple):
        total += 1
        for coord in combo:
            counts[coord] = counts.get(coord, 0) + 1
    return counts, total


def _pick_quest_target(buttons, board, known_empty, known_purple, purple_clicked, clicked_cells=None):
    """Ищем Purple (3 штуки), затем 4-ю клетку — она станет Red+."""
    clicked_cells = clicked_cells or set()
    remaining = [
        item for item in buttons
        if (item['x'], item['y']) not in clicked_cells
    ]
    # Уже кликнутые, но всё ещё активные — только если других ходов не осталось.
    buttons = remaining or buttons
    cells = {(item['x'], item['y']): item for item in buttons}

    # Уже открытый джекпот на поле — забираем сразу.
    for item in buttons:
        if item['sphere'] in _QUEST_JACKPOT:
            return item, f"джекпот {item['sphere']} (x:{item['x']}, y:{item['y']})"

    # Видимая Purple, которую ещё не кликнули.
    for item in buttons:
        if item['sphere'] == 'Purple':
            return item, f"видимую Purple (x:{item['x']}, y:{item['y']})"

    # Вычисленные Purple / 4-я (Red+): кликаем их, а не «безопасные» пустые.
    deduced = [
        coord for coord in sorted(known_purple)
        if board.get(coord) == 'Hidden' and coord in cells
    ]
    if deduced:
        coord = deduced[0]
        if purple_clicked >= 3:
            return cells[coord], f'4-ю Purple → Red+ {coord}'
        return cells[coord], f'вычисленную Purple {coord}'

    possible = [
        item for item in buttons
        if item['sphere'] == 'Hidden'
        and (item['x'], item['y']) not in known_empty
        and (item['x'], item['y']) not in known_purple
    ]
    if not possible:
        hidden = [item for item in buttons if item['sphere'] == 'Hidden']
        if hidden:
            target = random.choice(hidden)
            return target, f"Hidden (x:{target['x']}, y:{target['y']}) — кандидатов не осталось"
        return random.choice(buttons), 'случайную кнопку (вариантов не осталось)'

    counts, total = _quest_purple_probabilities(board, known_empty, known_purple)
    if total > 0:
        ranked = []
        for item in possible:
            coord = (item['x'], item['y'])
            count = counts.get(coord, 0)
            if count <= 0:
                continue
            ranked.append((
                -count,
                0 if coord in _QUEST_PROBE_CELLS else 1,
                item['y'],
                item['x'],
                item,
            ))
        if ranked:
            ranked.sort()
            best_item = ranked[0][4]
            best_count = counts[(best_item['x'], best_item['y'])]
            chance = round(100 * best_count / total)
            return best_item, f'Монте-Карло: шанс Purple {chance}%'

    probes = [
        item for item in possible
        if (item['x'], item['y']) in _QUEST_PROBE_CELLS
    ]
    if probes:
        target = random.choice(probes)
        return (
            target,
            f"пробную Hidden (x:{target['x']}, y:{target['y']}) — угол/центр",
        )

    target = random.choice(possible)
    return target, f"возможную Purple (x:{target['x']}, y:{target['y']})"


def _wait_for_render(message_id, previous_components, timeout=8):
    """Ожидает фактического обновления components после interaction-клика."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        human_sleep(0.35, 0.7)
        try:
            message = _get_message(message_id)
            if message is not None and _components_snapshot(message) != previous_components:
                return message
        except requests.RequestException as error:
            _mg_log(f'[yellow]Ошибка polling сообщения: {error}[/yellow]')
    return None


def _drain_minigame(play_once, label):
    """Крутит одну миниигру, пока Mudae не вернёт кулдаун/исчерпание или ошибку."""
    while True:
        result = play_once()
        if result.get('status') != 'completed':
            return result
        _mg_log(
            f'[cyan]{label}: раунд завершён, пробую ещё раз '
            f'(пока есть попытки)...[/cyan]'
        )
        human_sleep(
            float(getattr(Vars, 'minigame_gap_min', 1.5)),
            float(getattr(Vars, 'minigame_gap_max', 4.0)),
            kind='minigame',
        )


_MINIGAME_MAX_CLICKS = 30  # предохранитель, если Discord не пометит кнопки disabled


def _play_ouroharvest_once():
    """Один раунд $oh: кликает, пока на поле есть активные (не disabled) кнопки."""

    _mg_log('[bold cyan]Ouroharvest: отправляю $oh.[/bold cyan]')
    try:
        action_pause()
        response = requests.post(url, headers=auth, json={'content': '$oh'}, timeout=5)
        response.raise_for_status()
    except requests.RequestException as error:
        _mg_log(f'[bold red]Не удалось отправить $oh: {error}[/bold red]')
        return {'status': 'error', 'retry_after': 300}

    command_message = response.json()
    message = _wait_for_minigame_message(command_message['id'])
    if message is None:
        _mg_log('[bold red]Сетка $oh не появилась за 10 секунд.[/bold red]')
        return {'status': 'error', 'retry_after': 300}

    cooldown_seconds = _parse_cooldown_seconds(message)
    if cooldown_seconds is not None:
        _mg_log(
            f'[yellow]$oh в кулдауне. Следующая попытка через '
            f'{cooldown_seconds // 3600}ч {(cooldown_seconds % 3600) // 60}м.[/yellow]'
        )
        return {'status': 'cooldown', 'retry_after': cooldown_seconds + 60}

    if not message.get('components'):
        _mg_log('[bold red]Mudae вернула неожиданный ответ на $oh.[/bold red]')
        return {'status': 'error', 'retry_after': 300}

    message_id = message['id']
    _mg_log(f'[green]Сетка $oh найдена: message_id={message_id}.[/green]')

    click_count = 0
    warned_unknown = set()
    prefer_cell = None

    while True:
        buttons = _clickable_buttons(message)
        if not buttons:
            _mg_log(
                '[yellow]Активных кнопок больше нет — раунд $oh завершён '
                '(все клетки disabled, как в Discord).[/yellow]'
            )
            break

        if click_count >= _MINIGAME_MAX_CLICKS:
            _mg_log(
                f'[yellow]Достигнут предел {_MINIGAME_MAX_CLICKS} кликов — '
                f'останавливаюсь (что-то пошло не так).[/yellow]'
            )
            break

        _log_full_board(message)
        target, reason = _pick_harvest_target(buttons, prefer_cell, warned_unknown)
        click_count += 1
        move_label = 'бесплатный' if target['sphere'] == 'Purple' else str(click_count)
        _mg_log(
            f"[bold]Клик {move_label}:[/bold] нажимаю {reason} "
            f"в [cyan](x:{target['x']}, y:{target['y']})[/cyan]. "
            f'Активных кнопок: [magenta]{len(buttons)}[/magenta].'
        )

        previous_components = _components_snapshot(message)
        previous_content = message.get('content') or ''
        try:
            action_pause()
            bot.click(
                botID,
                channelID=Vars.channelId,
                guildID=Vars.serverId,
                messageID=message_id,
                messageFlags=message.get('flags', 0),
                data={'component_type': 2, 'custom_id': target['button']['custom_id']},
            )
        except Exception as error:
            _mg_log(f'[bold red]Ошибка нажатия: {error}[/bold red]')
            return {'status': 'error', 'retry_after': 300}

        rendered = _wait_for_render(message_id, previous_components)
        if rendered is None:
            _mg_log(
                '[yellow]Discord не обновил поле за 8 секунд; '
                'завершаю во избежание повторного клика.[/yellow]'
            )
            return {'status': 'error', 'retry_after': 300}
        message = rendered

        delta = _parse_harvest_delta(
            _content_delta(previous_content, message.get('content') or '')
        )
        still_clickable = _cell_still_clickable(message, target['x'], target['y'])
        became = _sphere_at(message, target['x'], target['y'])
        transformed_to = delta.get('turns_into') or (
            became if still_clickable and became in _TRANSFORM_TARGETS else None
        )

        if delta.get('emojis') or transformed_to or delta.get('free') or delta.get('broke_down'):
            parts = []
            if transformed_to:
                parts.append(f'turns into {transformed_to}')
            if delta.get('broke_down'):
                parts.append('breaks down')
            if delta.get('free'):
                parts.append('FREE')
            if delta.get('emojis'):
                parts.append('emojis=' + ','.join(delta['emojis'][:8]))
            _mg_log(f'[cyan]Mudae после клика:[/cyan] {"; ".join(parts)}')

        if still_clickable and (
            became in _TRANSFORM_TARGETS or transformed_to in _TRANSFORM_TARGETS
        ):
            prefer_cell = (target['x'], target['y'])
            label = transformed_to or became
            _mg_log(
                f'[magenta]Клетка (x:{target["x"]}, y:{target["y"]}) всё ещё активна '
                f'({label}) — дожму следующим кликом.[/magenta]'
            )
        elif prefer_cell == (target['x'], target['y']):
            prefer_cell = None

        if delta.get('free'):
            _mg_log('[green]Бесплатный ход (Purple): клик не тратит лимит Mudae.[/green]')

        active_left = len(_clickable_buttons(message))
        _mg_log(
            f'[green]Поле обновилось.[/green] '
            f'Активных кнопок осталось: [magenta]{active_left}[/magenta].'
        )

    _mg_log(
        f'[bold green]Ouroharvest завершён[/bold green] '
        f'(всего кликов: {click_count}).'
    )
    return {'status': 'completed', 'retry_after': 24 * 60 * 60}


def play_ouroharvest():
    """Крутит $oh, пока есть попытки (до кулдауна или ошибки)."""
    return _drain_minigame(_play_ouroharvest_once, 'Ouroharvest')


def _play_ourochest_once():
    """Один раунд $oc: кликает, пока на поле есть активные (не disabled) кнопки."""
    _mg_log('[bold cyan]Ourochest: отправляю $oc.[/bold cyan]')
    try:
        action_pause()
        response = requests.post(url, headers=auth, json={'content': '$oc'}, timeout=5)
        response.raise_for_status()
    except requests.RequestException as error:
        _mg_log(f'[bold red]Не удалось отправить $oc: {error}[/bold red]')
        return {'status': 'error', 'retry_after': 300}

    command_message = response.json()
    message = _wait_for_minigame_message(command_message['id'])
    if message is None:
        _mg_log('[bold red]Сетка $oc не появилась за 10 секунд.[/bold red]')
        return {'status': 'error', 'retry_after': 300}

    cooldown_seconds = _parse_cooldown_seconds(message)
    if cooldown_seconds is not None:
        _mg_log(
            f'[yellow]$oc в кулдауне. Следующая попытка через '
            f'{cooldown_seconds // 3600}ч {(cooldown_seconds % 3600) // 60}м.[/yellow]'
        )
        return {'status': 'cooldown', 'retry_after': cooldown_seconds + 60}

    if not message.get('components'):
        _mg_log('[bold red]Mudae вернула неожиданный ответ на $oc.[/bold red]')
        return {'status': 'error', 'retry_after': 300}

    message_id = message['id']
    _mg_log(f'[green]Сетка $oc найдена: message_id={message_id}.[/green]')

    possible_reds = [
        (x, y) for x in range(1, 6) for y in range(1, 6) if (x, y) != (3, 3)
    ]
    _mg_log(
        f'[cyan]Кандидаты Red на старте:[/cyan] {len(possible_reds)} '
        f'(центр 3,3 исключён).'
    )

    found_red = False
    red_pos = None
    click_count = 0
    warned_no_reds = False

    while True:
        buttons = _clickable_buttons(message)
        if not buttons:
            _mg_log(
                '[yellow]Активных кнопок больше нет — раунд $oc завершён '
                '(все клетки disabled, как в Discord).[/yellow]'
            )
            break

        if click_count >= _MINIGAME_MAX_CLICKS:
            _mg_log(
                f'[yellow]Достигнут предел {_MINIGAME_MAX_CLICKS} кликов — '
                f'останавливаюсь (что-то пошло не так).[/yellow]'
            )
            break

        if red_pos is None and not possible_reds and not warned_no_reds:
            warned_no_reds = True
            _mg_log(
                '[yellow]Кандидаты Red исчерпаны — логика разошлась с полем. '
                'Продолжаю по оставшимся активным кнопкам.[/yellow]'
            )

        _log_full_board(message, title='Текущее поле Ourochest')
        if red_pos is not None:
            target, reason = _pick_chest_target_after_red(buttons, red_pos)
        else:
            target, reason = _pick_chest_target(buttons, possible_reds, click_count)
        cx, cy = target['x'], target['y']
        click_count += 1

        candidates_note = (
            f'Ищем бонус у Red {red_pos}.'
            if red_pos is not None
            else f'Кандидатов Red: [magenta]{len(possible_reds)}[/magenta].'
        )
        _mg_log(
            f"[bold]Ход {click_count}:[/bold] нажимаю {reason} "
            f"в [cyan](x:{cx}, y:{cy})[/cyan]. {candidates_note} "
            f'Активных кнопок: [magenta]{len(buttons)}[/magenta].'
        )

        previous_components = _components_snapshot(message)
        try:
            action_pause()
            bot.click(
                botID,
                channelID=Vars.channelId,
                guildID=Vars.serverId,
                messageID=message_id,
                messageFlags=message.get('flags', 0),
                data={'component_type': 2, 'custom_id': target['button']['custom_id']},
            )
        except Exception as error:
            _mg_log(f'[bold red]Ошибка нажатия: {error}[/bold red]')
            return {'status': 'error', 'retry_after': 300}

        rendered = _wait_for_render(message_id, previous_components)
        if rendered is None:
            _mg_log(
                '[yellow]Discord не обновил поле за 8 секунд; '
                'завершаю во избежание повторного клика.[/yellow]'
            )
            return {'status': 'error', 'retry_after': 300}
        message = rendered

        color = _sphere_at(message, cx, cy)
        color_label = color or 'неизвестно'
        value = sphere_values.get(color, '?')
        _mg_log(
            f'[green]Открыто:[/green] [bold]{color_label}[/bold] '
            f'(ценность {value}) в (x:{cx}, y:{cy}).'
        )

        if red_pos is None:
            if color is None:
                _mg_log('[yellow]Цвет клетки не распознан — кандидаты не сужены.[/yellow]')
            else:
                possible_reds = _filter_possible_reds(possible_reds, cx, cy, color)

            _mg_log(
                f'[cyan]Осталось кандидатов Red:[/cyan] '
                f'[bold magenta]{len(possible_reds)}[/bold magenta] → {possible_reds}'
            )

            if color == 'Red':
                found_red = True
                red_pos = (cx, cy)
                _mg_log(
                    f'[bold green]Red найдена в (x:{cx}, y:{cy})! '
                    f'Ценность {sphere_values["Red"]}. '
                    f'Продолжаю, пока кнопки активны: Orange → Yellow → Green…[/bold green]'
                )
        else:
            _mg_log(
                f'[cyan]После Red {red_pos}:[/cyan] собрали {color_label}, '
                f'ходы ещё есть — целимся в следующую лучшую сферу.'
            )

        active_left = len(_clickable_buttons(message))
        _mg_log(
            f'[green]Поле обновилось.[/green] '
            f'Активных кнопок осталось: [magenta]{active_left}[/magenta].'
        )

    if found_red:
        _mg_log(
            f'[bold green]Ourochest завершён: Red найдена, '
            f'ходов {click_count}.[/bold green]'
        )
        return {'status': 'completed', 'found_red': True, 'retry_after': 24 * 60 * 60}

    if len(possible_reds) == 1:
        _mg_log(
            f'[yellow]Ходы закончились. Вероятная Red: {possible_reds[0]} '
            f'(не успели кликнуть).[/yellow]'
        )
    else:
        _mg_log(
            f'[yellow]Ourochest завершён без явного Red. '
            f'Кандидаты: {possible_reds}[/yellow]'
        )
    return {'status': 'completed', 'found_red': False, 'retry_after': 24 * 60 * 60}


def play_ourochest():
    """Крутит $oc, пока есть попытки (до кулдауна или ошибки)."""
    return _drain_minigame(_play_ourochest_once, 'Ourochest')


def _play_ouroquest_once():
    """Один раунд $oq: ищет Purple/джекпот и кликает, пока кнопки не disabled."""
    _mg_log('[bold cyan]Ouroquest: отправляю $oq.[/bold cyan]')
    try:
        action_pause()
        response = requests.post(url, headers=auth, json={'content': '$oq'}, timeout=5)
        response.raise_for_status()
    except requests.RequestException as error:
        _mg_log(f'[bold red]Не удалось отправить $oq: {error}[/bold red]')
        return {'status': 'error', 'retry_after': 300}

    command_message = response.json()
    message = _wait_for_minigame_message(command_message['id'])
    if message is None:
        _mg_log('[bold red]Сетка $oq не появилась за 10 секунд.[/bold red]')
        return {'status': 'error', 'retry_after': 300}

    cooldown_seconds = _parse_cooldown_seconds(message)
    if cooldown_seconds is not None:
        _mg_log(
            f'[yellow]$oq в кулдауне. Следующая попытка через '
            f'{cooldown_seconds // 3600}ч {(cooldown_seconds % 3600) // 60}м.[/yellow]'
        )
        return {'status': 'cooldown', 'retry_after': cooldown_seconds + 60}

    if not message.get('components'):
        _mg_log('[bold red]Mudae вернула неожиданный ответ на $oq.[/bold red]')
        return {'status': 'error', 'retry_after': 300}

    message_id = message['id']
    _mg_log(f'[green]Сетка $oq найдена: message_id={message_id}.[/green]')

    known_empty = set()
    known_purple = set()
    purple_clicked = 0
    jackpot = None
    click_count = 0
    clicked_cells = set()

    while True:
        board = _board_map(message)
        _analyze_quest_field(board, known_empty, known_purple)

        buttons = _clickable_buttons(message)
        if not buttons:
            _mg_log(
                '[yellow]Активных кнопок больше нет — раунд $oq завершён '
                '(все клетки disabled, как в Discord).[/yellow]'
            )
            break

        if click_count >= _MINIGAME_MAX_CLICKS:
            _mg_log(
                f'[yellow]Достигнут предел {_MINIGAME_MAX_CLICKS} кликов — '
                f'останавливаюсь (что-то пошло не так).[/yellow]'
            )
            break

        _log_full_board(message, title='Текущее поле Ouroquest')
        _mg_log(
            f'[cyan]пустые (не Purple)[/cyan] ({len(known_empty)}): '
            f'[green]{sorted(known_empty)}[/green]'
        )
        _mg_log(
            f'[cyan]известные Purple[/cyan] ({len(known_purple)}): '
            f'[magenta]{sorted(known_purple)}[/magenta] | '
            f'Purple кликнуто: [bold]{purple_clicked}/3[/bold]'
        )

        target, reason = _pick_quest_target(
            buttons, board, known_empty, known_purple, purple_clicked, clicked_cells
        )
        cx, cy = target['x'], target['y']
        click_count += 1

        _mg_log(
            f'[bold]Ход {click_count}:[/bold] нажимаю {reason} '
            f'в [cyan](x:{cx}, y:{cy})[/cyan]. '
            f'Активных кнопок: [magenta]{len(buttons)}[/magenta].'
        )

        previous_components = _components_snapshot(message)
        try:
            action_pause()
            bot.click(
                botID,
                channelID=Vars.channelId,
                guildID=Vars.serverId,
                messageID=message_id,
                messageFlags=message.get('flags', 0),
                data={'component_type': 2, 'custom_id': target['button']['custom_id']},
            )
        except Exception as error:
            _mg_log(f'[bold red]Ошибка нажатия: {error}[/bold red]')
            return {'status': 'error', 'retry_after': 300}

        known_empty.discard((cx, cy))
        clicked_cells.add((cx, cy))

        rendered = _wait_for_render(message_id, previous_components)
        if rendered is None:
            _mg_log(
                '[yellow]Discord не обновил поле за 8 секунд; '
                'завершаю во избежание повторного клика.[/yellow]'
            )
            return {'status': 'error', 'retry_after': 300}
        message = rendered

        color = _sphere_at(message, cx, cy)
        color_label = color or 'неизвестно'
        value = sphere_values.get(color, '?')
        _mg_log(
            f'[green]Открыто:[/green] [bold]{color_label}[/bold] '
            f'(ценность {value}) в (x:{cx}, y:{cy}).'
        )

        if color == 'Purple':
            purple_clicked += 1
            known_purple.add((cx, cy))
            known_empty.discard((cx, cy))
            _mg_log(
                f'[magenta]Purple найдена![/magenta] Прогресс: '
                f'[bold]{purple_clicked}/3[/bold] '
                f'(после трёх 4-я станет Red+).'
            )
        elif color in _QUEST_JACKPOT:
            jackpot = color
            known_purple.add((cx, cy))
            known_empty.discard((cx, cy))
            _mg_log(
                f'[bold green]Джекпот! {color} '
                f'(ценность {sphere_values[color]}). '
                f'Продолжаю, пока кнопки активны.[/bold green]'
            )
        elif color in _QUEST_PURPLE_COUNTS:
            known_empty.add((cx, cy))
            known_purple.discard((cx, cy))
            _mg_log(
                f'[blue]Подсказка:[/blue] вокруг '
                f'{_QUEST_PURPLE_COUNTS[color]} Purple.'
            )

        board = _board_map(message)
        _analyze_quest_field(board, known_empty, known_purple)
        active_left = len(_clickable_buttons(message))
        _mg_log(
            f'[cyan]После хода → пустые[/cyan] '
            f'[green]{sorted(known_empty)}[/green] | '
            f'[cyan]Purple[/cyan] '
            f'[magenta]{sorted(known_purple)}[/magenta]'
        )
        _mg_log(
            f'[green]Поле обновилось.[/green] '
            f'Активных кнопок осталось: [magenta]{active_left}[/magenta].'
        )

    if jackpot:
        _mg_log(
            f'[bold green]Ouroquest завершён: джекпот {jackpot}, '
            f'ходов {click_count}.[/bold green]'
        )
        return {
            'status': 'completed',
            'jackpot': jackpot,
            'purple_clicked': purple_clicked,
            'retry_after': 24 * 60 * 60,
        }

    _mg_log(
        f'[yellow]Ouroquest завершён. Purple кликнуто: {purple_clicked}/3, '
        f'ходов {click_count}, известные Purple={sorted(known_purple)}.[/yellow]'
    )
    return {
        'status': 'completed',
        'jackpot': None,
        'purple_clicked': purple_clicked,
        'retry_after': 24 * 60 * 60,
    }


def play_ouroquest():
    """Крутит $oq, пока есть попытки (до кулдауна или ошибки)."""
    return _drain_minigame(_play_ouroquest_once, 'Ouroquest')