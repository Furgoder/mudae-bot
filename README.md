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

- rolls on a schedule
- claims cards by kakera value and series list
- collects kakera
- plays Ourospheres minigames
- shows logs and settings in the browser

### Setup

You need **Python 3.10+**. Enable **Add Python to PATH** when installing.

1. Clone the repo.
2. Run `setup.bat` to create `venv` and install dependencies.
3. Copy the example config:

```bat
copy Vars.example.py Vars.py
```

4. Fill in `Vars.py` (`token`, `channelId`, `serverId`). `username` can stay empty.
5. Run `start.bat`.
6. Open the web UI (the address is printed in the console). Settings can be changed there too.

`Vars.py` is gitignored. Only `Vars.example.py` with placeholders is in the repo.

### Config

Main fields in `Vars.example.py`:

| Field | Meaning |
|---|---|
| `token` | account token, local only |
| `channelId` / `serverId` | where to send commands |
| `rollCommand` | roll command (`wa`, `ha`, `mx`, …) |
| `min_power_threshold` | minimum card value to claim |
| `desiredSeries` | series to always claim |
| `desiredKakeras` | kakera types to collect |
| `snipeEnabled` | react to other people’s rolls |
| `ouroMinigamesEnabled` | minigames |
| `humanize_enabled` | random delays between actions |
| `sleep_mode_enabled` | night pause |

Full list: `Vars.example.py` and the Settings tab in the web UI.

### Layout

```
Bot.py              start and schedule
Function.py         rolls, claims, kakera
Sniper.py           other people’s drops
Minigames.py        Ourospheres
WebDashboard.py     local web UI
Database.py         claim history (local SQLite)
Vars.example.py     example config
```

`mudae_history.db` and `claim_history.json` are created on your machine and are not in git.

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

- крутить рулетку по расписанию
- клеймить карты по порогу цены и списку серий
- собирать какеру
- играть в миниигры Ourospheres
- показывать логи и настройки в браузере

### Установка

Нужен **Python 3.10+**. При установке включите **Add Python to PATH**.

1. Склонируйте репозиторий.
2. Запустите `setup.bat` — создастся `venv` и поставятся зависимости.
3. Скопируйте пример конфига:

```bat
copy Vars.example.py Vars.py
```

4. Заполните `Vars.py` своими данными (`token`, `channelId`, `serverId`). Имя в `username` можно оставить пустым.
5. Запустите `start.bat`.
6. Откройте веб-панель (адрес появится в консоли). Настройки можно менять и там.

Файл `Vars.py` в git не попадает. В репозитории только `Vars.example.py` с заглушками.

### Конфиг

Основные поля в `Vars.example.py`:

| Поле | Смысл |
|---|---|
| `token` | токен аккаунта, только локально |
| `channelId` / `serverId` | куда писать команды |
| `rollCommand` | команда ролла (`wa`, `ha`, `mx` и т.д.) |
| `min_power_threshold` | минимальная цена карты для клейма |
| `desiredSeries` | серии, которые клеймить всегда |
| `desiredKakeras` | какие какеры собирать |
| `snipeEnabled` | реагировать на чужие роллы |
| `ouroMinigamesEnabled` | миниигры |
| `humanize_enabled` | случайные паузы между действиями |
| `sleep_mode_enabled` | ночной простой |

Полный список — в `Vars.example.py` и на вкладке «Настройки» в веб-панели.

### Структура

```
Bot.py              запуск и расписание
Function.py         роллы, клейм, какера
Sniper.py           реакция на чужие дропы
Minigames.py        Ourospheres
WebDashboard.py     локальный веб-интерфейс
Database.py         история клеймов (локальный SQLite)
Vars.example.py     пример конфига
```

`mudae_history.db` и `claim_history.json` создаются у вас на диске и в git не входят.

### Зависимости

См. `requirements.txt`: `discum`, `requests`, `schedule`, `flask`, `flask-socketio`.
