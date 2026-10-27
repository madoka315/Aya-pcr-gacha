# -*- coding: utf-8 -*-
import json
import os
from datetime import datetime, timedelta

import hoshino

STATS_PATH = os.path.expanduser('~/.hoshino/gacha_stats.json')
_STATS = None


def _today_key():
    # 与游戏日保持一致：凌晨 5 点切换“今天”。服务器时区为 Asia/Shanghai。
    now = datetime.now()
    if now.hour < 5:
        now -= timedelta(days=1)
    return now.strftime('%Y-%m-%d')


def _load():
    try:
        with open(STATS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError('stats root is not an object')
        if not isinstance(data.get('users'), dict):
            data['users'] = {}
        return data
    except FileNotFoundError:
        return {'users': {}}
    except Exception:
        hoshino.logger.exception('读取 gacha_stats.json 失败，将从空统计开始')
        return {'users': {}}


def _save(data):
    directory = os.path.dirname(STATS_PATH)
    os.makedirs(directory, exist_ok=True)
    tmp_path = STATS_PATH + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, STATS_PATH)


def _get_stats():
    global _STATS
    if _STATS is None:
        _STATS = _load()
    return _STATS


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def record_draw(user_id, pool_name, pool_key, cost, chars, up_ids):
    data = _get_stats()
    users = data.setdefault('users', {})
    uid = str(user_id)
    user = users.get(uid)
    if not isinstance(user, dict):
        user = {}
        users[uid] = user

    today = _today_key()
    if user.get('day') != today:
        user['day'] = today
        user['daily_spent'] = 0
    user['daily_spent'] = _int(user.get('daily_spent')) + _int(cost)

    pools = user.get('pools')
    if not isinstance(pools, dict):
        pools = {}
        user['pools'] = pools

    if pool_name == 'FES':
        pool = pools.get('FES')
        if not isinstance(pool, dict):
            pool = {}
            pools['FES'] = pool
    else:
        pool = pools.get('BL')
        if not isinstance(pool, dict) or pool.get('banner') != pool_key:
            pool = {'banner': pool_key}
            pools['BL'] = pool

    for key in ('spent', 'star3', 'up'):
        pool[key] = _int(pool.get(key))
    pool['spent'] += _int(cost)

    up_ids = set(up_ids or ())
    for chara in chars or ():
        if getattr(chara, 'star', 0) == 3:
            pool['star3'] += 1
        if getattr(chara, 'id', None) in up_ids:
            pool['up'] += 1

    try:
        _save(data)
    except Exception:
        hoshino.logger.exception('写入 gacha_stats.json 失败')

    return {
        'daily_spent': user['daily_spent'],
        'spent': pool['spent'],
        'star3': pool['star3'],
        'up': pool['up'],
    }


def format_stats(stats):
    return (
        f'已获得3⭐️x{stats["star3"]}, UP角色x{stats["up"]}\n'
        f'今天已经花了{stats["daily_spent"]}宝石, 本卡池已累计{stats["spent"]}宝石'
    )
