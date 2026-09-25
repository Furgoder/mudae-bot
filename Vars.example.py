# Конфиг бота. Редактируйте вручную или через вкладку «Настройки» в Web UI.
# После сохранения через UI файл перезаписывается и бот перезапускается.
#
# Инструкция: скопируйте этот файл как Vars.py и заполните своими данными.
#   copy Vars.example.py Vars.py

# --- Discord ---
token = 'YOUR_DISCORD_TOKEN_HERE'
channelId = 'YOUR_CHANNEL_ID_HERE'
serverId = 'YOUR_SERVER_ID_HERE'
username = ''

# --- Снайпинг ---
snipeEnabled = False
snipeChannelIds = []
snipe_roll_ignore_window = 2.5
# capabilities identify discum (меньше = легче; 253 ≈ сообщения без тяжёлых features)
gateway_capabilities = 253

# --- Миниигры ---
ouroMinigamesEnabled = True

# --- Роллы и клейм ---
rollCommand = 'mx'
desiredKakeras = ['kakeraP', 'kakeraY', 'kakeraO', 'kakeraR', 'kakeraW', 'kakeraL', 'kakeraD', 'kakeraC']
desiredSeries = []
min_power_threshold = 80
pokeRoll = False
repeatMinute = '38'
max_rolls_per_session = 150
kakera_max_clicks = 8
claim_ttl = 90

# --- Жадность порога ---
hunger_minutes_mid = 60
hunger_minutes_high = 120
hunger_multiplier = 1.5
hunger_multiplier_high = 2.5

# --- Джекпот ---
jackpot_multiplier = 3.0
jackpot_threshold_factor = 1.5

# --- Человечность (рандомные паузы) ---
# Делает тайминги менее «метрономными», чтобы активность была похожа на ручную.
humanize_enabled = True
# Пауза между роллами (сек)
roll_delay_min = 1.2
roll_delay_max = 2.8
# Шанс «отвлёкся» на 2–6 сек между роллами (0.0–1.0)
roll_distract_chance = 0.08
# Пауза перед кликом/реакцией (клейм, какера, миниигры)
action_delay_min = 0.35
action_delay_max = 1.1
# Пауза между минииграми / раундами
minigame_gap_min = 1.5
minigame_gap_max = 4.0
# Случайный сдвиг старта роллов после :repeatMinute (сек)
schedule_jitter_max = 90
# Джиттер поверх кулдауна миниигр (сек)
minigame_cd_jitter_max = 120
# Ночной простой: не крутить роллы и миниигры в интервале (через полночь ок)
sleep_mode_enabled = False
sleep_time_start = "02:00"
sleep_time_end = "09:00"
