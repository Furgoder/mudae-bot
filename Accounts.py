"""Аккаунты роя. Gateway поднимает только основной, слейвы шлют команды своим токеном."""

import threading

import discum
import requests

import Vars

MUDAE_BOT_ID = '432610292342587392'

ACCOUNTS = []
MAIN = None


def empty_profile(account_key):
    return {
        'account_key': account_key,
        'can_claim': False,
        'rt_ready': False,
        'claim_minutes_left': 0,
        'rolls_left': 0,
        'rolls_resets': 0,
        'current_threshold': float(getattr(Vars, 'min_power_threshold', 80)),
        'kakera_power': 0,
        'kakera_cost': 0,
        'kakera_cost_keys': 0,
        'kakera_power_full': False,
        'kakera_ready': True,
    }


class Account:
    def __init__(self, key, role, token, username=''):
        self.key = key
        self.role = role
        self.token = token or ''
        self.configured_username = (username or '').strip()
        self.user_id = ''
        self.display_name = ''
        self.login = ''
        self.client = None
        self.auth = {'authorization': self.token}
        self.profile = empty_profile(key)
        self.kakera_cooldown_until = 0.0
        self.rolls_reset_used_until = 0.0
        self.next_roll_at = None
        self.last_session_seconds = None
        self.lock = threading.Lock()

    @property
    def is_main(self):
        return self.role == 'main'

    @property
    def label(self):
        return self.display_name or self.configured_username or self.login or self.key

    def names(self):
        found = []
        seen = set()
        for name in (self.configured_username, self.display_name, self.login):
            text = (name or '').strip()
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                found.append(text)
        return found


def ensure_runtime_vars():
    """Ключи, которых ещё нет в старом Vars.py, чтобы панель и код их видели."""
    defaults = {
        'slaves': [],
        'roll_window_margin_sec': 90,
        'channel_quiet_min': 25,
        'channel_quiet_max': 50,
    }
    for key, value in defaults.items():
        if not hasattr(Vars, key):
            setattr(Vars, key, value)


def _make_client(token, *, gateway):
    client = discum.Client(token=token, log=False)
    if not gateway:
        return client
    client.gateway.auth['presence'] = {
        'status': 'online',
        'since': 0,
        'activities': [],
        'afk': False,
    }
    client.gateway.auth['capabilities'] = int(getattr(Vars, 'gateway_capabilities', 253))
    return client


def _fetch_identity(account):
    if not account.token or account.token.startswith('YOUR_'):
        return
    try:
        response = requests.get(
            'https://discord.com/api/v9/users/@me',
            headers=account.auth,
            timeout=8,
        )
    except requests.RequestException:
        return
    if response.status_code != 200:
        return
    data = response.json()
    account.user_id = str(data.get('id') or '')
    account.display_name = (data.get('global_name') or '').strip()
    account.login = (data.get('username') or '').strip()


def _slave_entries():
    raw = getattr(Vars, 'slaves', None) or []
    if not isinstance(raw, list):
        return
    for item in raw:
        if isinstance(item, str):
            token, username = item.strip(), ''
        elif isinstance(item, dict):
            token = str(item.get('token') or '').strip()
            username = str(item.get('username') or '').strip()
        else:
            continue
        if not token or token.startswith('YOUR_'):
            continue
        yield token, username


def all_accounts():
    return list(ACCOUNTS)


def slaves():
    return [account for account in ACCOUNTS if not account.is_main]


def by_key(key):
    if not key:
        return None
    for account in ACCOUNTS:
        if account.key == key:
            return account
    return None


_identity_retried = False


def _ensure_identities():
    global _identity_retried
    if _identity_retried:
        return
    if not any(not account.user_id for account in ACCOUNTS):
        return
    _identity_retried = True
    for account in ACCOUNTS:
        if not account.user_id:
            _fetch_identity(account)


def by_user_id(user_id):
    uid = str(user_id or '')
    if not uid:
        return None
    for account in ACCOUNTS:
        if account.user_id and account.user_id == uid:
            return account
    _ensure_identities()
    for account in ACCOUNTS:
        if account.user_id and account.user_id == uid:
            return account
    return None


def by_name(candidate):
    text = (candidate or '').strip()
    if not text:
        return None
    needle = text.casefold()
    last = needle.split()[-1] if needle.split() else needle
    for account in ACCOUNTS:
        for name in account.names():
            current = name.casefold()
            if needle == current or last == current:
                return account
    return None


def resolve(account=None):
    if isinstance(account, Account):
        return account
    return by_key(account) or MAIN


def snapshot():
    import time
    now = time.time()
    rows = []
    for account in ACCOUNTS:
        left = max(0, int(account.kakera_cooldown_until - now))
        ready_flag = account.profile.get('kakera_ready')
        rows.append({
            'key': account.key,
            'label': account.label,
            'role': account.role,
            'can_claim': bool(account.profile.get('can_claim')),
            'rt_ready': bool(account.profile.get('rt_ready')),
            'kakera_ready': left <= 0 and ready_flag is not False,
            'kakera_seconds': left,
            'rolls_left': int(account.profile.get('rolls_left') or 0),
            'next_roll_at': account.next_roll_at,
            'last_session_seconds': account.last_session_seconds,
        })
    return rows


def load_saved_durations():
    import Database
    Database.init_db()
    saved = Database.load_roll_durations()
    for account in ACCOUNTS:
        seconds = saved.get(account.key)
        if seconds:
            account.last_session_seconds = float(seconds)


def load():
    global ACCOUNTS, MAIN
    ensure_runtime_vars()
    main = Account('main', 'main', getattr(Vars, 'token', '') or '', getattr(Vars, 'username', '') or '')
    main.client = _make_client(main.token, gateway=True)
    _fetch_identity(main)
    accounts = [main]
    index = 0
    for token, username in _slave_entries():
        index += 1
        slave = Account(f'slave-{index}', 'slave', token, username)
        slave.client = _make_client(token, gateway=False)
        _fetch_identity(slave)
        accounts.append(slave)
    ACCOUNTS = accounts
    MAIN = main


load()
