# Mudae Bot

[English](#english) · [Русский](#русский)

A small personal [Mudae](https://mudae.net/) helper with a local web UI. Written with Cursor.

License: [MIT](LICENSE).

---

## English

### Warning

This is a **self-bot**: it uses a regular Discord user account, not a bot application. That [breaks Discord’s Terms of Service](https://discord.com/terms). The account can be banned. This repo is personal/educational code, not a product.

Never commit `Vars.py`, your token, or history files. A token is the password to the account.

### What it does

- rolls the main account, then each slave, one after another
- claims and kakera follow `Vars.py` (threshold, series, kakera types). Main takes the card when it can; otherwise the next account that still has a claim does
- every account whose kakera reaction is ready can click; spheres are clicked by the account that rolled
- starts the first roll pass immediately, then picks a random time after each Mudae reset so the session finishes before the next reset
- waits until the channel is quiet before rolling
- plays `$oh` / `$oc` / `$oq` on every account, each with its own cooldown
- emergency `$us 20` from the panel, including several batches in one click
- shows logs, the swarm, and settings in the browser

### Run it

You need **Python 3.10+**. Enable **Add Python to PATH** when installing. Docker is optional.

1. Clone the repo.
2. Run `setup.bat` to create `venv` and install dependencies.
3. Copy the example config:

```bat
copy Vars.example.py Vars.py
```

4. Fill in `Vars.py`:
   - `token`, `channelId`, `serverId` for the main account. `username` can stay empty.
   - `repeatMinute` is the **reset minute** of Mudae rolls in this server (for example `'38'` means the hour resets at :38). It is not the minute the bot starts rolling.
   - `slaves` is a list of extra accounts in the same channel. Leave `[]` if you only have one account:

```python
slaves = [
    {'token': 'SECOND_ACCOUNT_TOKEN', 'username': ''},
]
```

5. Run `start.bat`.
6. Open `http://127.0.0.1:5000`. The same address is printed in the console.

The first roll pass starts right away (unless sleep mode is on). Later passes start at a random moment between the reset and `next reset − last session length − roll_window_margin_sec` (default margin 90 seconds). Before the first measured session the bot assumes 5 minutes. If someone else is rolling, the bot waits `channel_quiet_min`–`channel_quiet_max` seconds of silence (default 25–50).

`Vars.py` is gitignored. Only `Vars.example.py` is in the repo.

#### Docker

`Vars.py` must already exist next to `compose.yml`. History and logs go to `./data`.

```bat
mkdir data
docker compose up -d --build
```

Panel: `http://127.0.0.1:5000`. Timezone in `compose.yml` is `Asia/Almaty`; change `TZ` if the reset minute should follow another clock. Edit `Vars.py` on the host (it is mounted into the container) and restart the container after a manual edit. Saving from the Settings tab rewrites `Vars.py` and restarts the process inside the container.

#### `$us` from the panel

Settings → emergency controls. The number is **how many rolls** you want, in steps of 20. `100` sends `$us 20` five times. Each command must get Mudae’s checkmark; if one does not, the bot stops and rolls only what was confirmed. The phrase about the roulette limit stops the slashes. This burst does not spend hourly `$rolls` and does not move the next scheduled window. It still stops at the next reset minute. Maximum in the field is 400 rolls.

### Config

| Field | Meaning |
|---|---|
| `token` | main account token, local only |
| `slaves` | extra accounts: `[{'token': '...', 'username': ''}]` |
| `channelId` / `serverId` | where to send commands |
| `rollCommand` | roll command (`wa`, `ha`, `mx`, …) |
| `repeatMinute` | Mudae roll **reset** minute |
| `roll_window_margin_sec` | spare time before the next reset |
| `channel_quiet_min` / `channel_quiet_max` | quiet seconds before a slash |
| `min_power_threshold` | minimum card value to claim |
| `desiredSeries` | series to always claim |
| `desiredKakeras` | kakera types to collect |
| `snipeEnabled` | react to other people’s rolls (main account) |
| `ouroMinigamesEnabled` | minigames for every account |
| `humanize_enabled` | random delays between actions |
| `sleep_mode_enabled` | night pause (rolls and minigames) |

Full list: `Vars.example.py` and the Settings tab.

### Layout

```
Bot.py              start, roll queue, minigame timer
Function.py         rolls, claims, kakera, $us
Accounts.py         main account and slaves
Swarm.py            who claims and who clicks kakera
Sniper.py           other people’s drops
Minigames.py        Ourospheres
WebDashboard.py     local web UI
Database.py         claim history and session length (SQLite)
Vars.example.py     example config
```

With Docker, `mudae_history.db` and logs live in `./data`. Without Docker they stay in the project folder. Neither is in git.

### Dependencies

See `requirements.txt`: `discum`, `requests`, `schedule`, `flask`, `flask-socketio`.

---

## Русский

Простой бот для личного пользования в [Mudae](https://mudae.net/). Управление через локальную веб-панель. Написан с помощью Cursor.

Лицензия: [MIT](LICENSE).

### Важно

Это **self-bot**: он ходит в Discord от обычного аккаунта, а не от приложения бота. Так делать [запрещено правилами Discord](https://discord.com/terms). Аккаунт могут заблокировать. Репозиторий — личный/учебный код, не готовый продукт.

Никогда не выкладывайте `Vars.py`, токен и файлы истории. Токен — это пароль от аккаунта.

### Что умеет

- крутит основной аккаунт, потом каждого слейва, по очереди, без наложения
- клейм и какера по правилам из `Vars.py`. Карту забирает основной, если у него есть клейм; иначе следующий аккаунт, у которого клейм ещё свободен
- какеру жмёт каждый, у кого реакция не на кулдауне. Сферы жмёт тот, кто крутил
- первый проход роллов сразу после запуска, дальше случайное время после ресета, чтобы сессия кончилась до следующего ресета
- ждёт тишину в канале, если крутит кто-то чужой
- играет в `$oh` / `$oc` / `$oq` каждым аккаунтом, кулдаун у каждого свой
- с панели можно отдать пачку `$us 20` и прокрутить разовые роллы
- показывает логи, рой и настройки в браузере

### Как запустить

Нужен **Python 3.10+**. При установке включите **Add Python to PATH**. Docker не обязателен.

1. Склонируйте репозиторий.
2. Запустите `setup.bat`. Он создаст `venv` и поставит зависимости.
3. Скопируйте конфиг:

```bat
copy Vars.example.py Vars.py
```

4. Откройте `Vars.py` и заполните:
   - `token`, `channelId`, `serverId` основного аккаунта. `username` можно оставить пустым: имя подтянется само.
   - `repeatMinute` — это **минута ресета** роллов Mudae на сервере, а не минута старта бота. Если рулетка обновляется в :38, пишите `'38'`.
   - `slaves` — остальные аккаунты в том же канале. Один аккаунт — оставьте `[]`:

```python
slaves = [
    {'token': 'ТОКЕН_ВТОРОГО', 'username': ''},
]
```

5. Запустите `start.bat`.
6. Откройте `http://127.0.0.1:5000`. Тот же адрес пишется в консоли.

Первый проход роллов начинается сразу, если не включён ночной режим. Следующие старты выбираются случайно между ресетом и моментом `следующий ресет − длина прошлой сессии − roll_window_margin_sec` (запас по умолчанию 90 секунд). Пока длина ещё не измерена, бот считает сессию за 5 минут. Если в канале крутит чужой, бот ждёт тишину `channel_quiet_min`–`channel_quiet_max` секунд (по умолчанию 25–50) и не начинает ролл, который пересёк бы ресет.

`Vars.py` в git не попадает. В репозитории только `Vars.example.py`.

Настройки можно менять на вкладке «Настройки». Кнопка «Сохранить» перезаписывает `Vars.py` и перезапускает процесс.

#### Docker

Рядом с `compose.yml` уже должен лежать заполненный `Vars.py`. База и логи пишутся в папку `./data`.

```bat
mkdir data
docker compose up -d --build
```

Панель: `http://127.0.0.1:5000`. Часовой пояс в `compose.yml` — `Asia/Almaty`. Если ресет считается по другому времени, поменяйте `TZ` и пересоберите. Файл `Vars.py` смонтирован с диска: после ручной правки перезапустите контейнер (`docker compose restart`). Сохранение из панели само перезапускает процесс внутри контейнера.

#### `$us` с панели

Вкладка «Настройки», блок «Экстренное управление». Число — это **сколько роллов** нужно, шаг 20. `100` значит пять команд `$us 20`, кнопку жать один раз. На каждую команду бот ждёт галочку Mudae. Нет галочки — пачка останавливается, крутятся только уже подтверждённые. Фраза про лимит рулетки останавливает слэши. Эти роллы не тратят часовые `$rolls` и не сдвигают следующее окно. До ближайшего ресета пачка всё равно останавливается, чтобы не крутить через смену часа. В поле максимум 400 роллов.

Кнопка «Запуск Миниигр» сбрасывает кулдаун и прогоняет `$oh` / `$oc` / `$oq` у основного и у всех слейвов.

### Конфиг

| Поле | Смысл |
|---|---|
| `token` | токен основного аккаунта, только локально |
| `slaves` | доп. аккаунты: `[{'token': '...', 'username': ''}]` |
| `channelId` / `serverId` | куда писать команды |
| `rollCommand` | команда ролла (`wa`, `ha`, `mx` и т.д.) |
| `repeatMinute` | минута **ресета** роллов Mudae |
| `roll_window_margin_sec` | запас до следующего ресета |
| `channel_quiet_min` / `channel_quiet_max` | секунды тишины перед слэшем |
| `min_power_threshold` | минимальная цена карты для клейма |
| `desiredSeries` | серии, которые клеймить всегда |
| `desiredKakeras` | какие какеры собирать |
| `snipeEnabled` | реагировать на чужие роллы (основной аккаунт) |
| `ouroMinigamesEnabled` | миниигры у всех аккаунтов |
| `humanize_enabled` | случайные паузы между действиями |
| `sleep_mode_enabled` | ночной простой (роллы и миниигры) |

Полный список — в `Vars.example.py` и на вкладке «Настройки».

### Структура

```
Bot.py              запуск, очередь роллов, таймер миниигр
Function.py         роллы, клейм, какера, $us
Accounts.py         основной аккаунт и слейвы
Swarm.py            кто клеймит и кто жмёт какеру
Sniper.py           реакция на чужие дропы
Minigames.py        Ourospheres
WebDashboard.py     локальный веб-интерфейс
Database.py         история клеймов и длина сессии (SQLite)
Vars.example.py     пример конфига
```

В Docker база `mudae_history.db` и логи лежат в `./data`. Без Docker — в папке проекта. В git они не входят.

### Зависимости

См. `requirements.txt`: `discum`, `requests`, `schedule`, `flask`, `flask-socketio`.
