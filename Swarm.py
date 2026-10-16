"""Кто забирает карту и кто жмёт какеру. Клейм один, реакция — у каждого со своей силой."""

import Accounts


def pick_claimer(roller_key, *, allow_rt, claims_used):
    """Основной, если может. Иначе крутящий. Иначе следующий слейв.

    $rt считается силой только когда allow_rt (вишлист и джекпот).
    """
    used = set(claims_used or ())

    def ready(account):
        if account is None or account.key in used:
            return False
        if account.profile.get('can_claim'):
            return True
        return bool(allow_rt and account.profile.get('rt_ready'))

    main = Accounts.MAIN
    if ready(main):
        return main
    roller = Accounts.by_key(roller_key)
    if ready(roller):
        return roller
    for slave in Accounts.slaves():
        if ready(slave):
            return slave
    return None


def reaction_accounts():
    """Аккаунты, у которых реакция какеры сейчас не в откате."""
    import Function
    ready = []
    for account in Accounts.all_accounts():
        if Function.account_reaction_ready(account):
            ready.append(account)
    return ready
