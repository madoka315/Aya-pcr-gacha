# -*- coding: utf-8 -*-
import json
import os
import re
import time
import sqlite3
from ast import literal_eval

import hoshino
from hoshino import aiorequests, priv
from nonebot import on_command, get_bot, scheduler

from .. import _pcr_data
from ..chara import download_chara_icon, roster, UNKNOWN

# 卡池更新是否通知管理员
NOTICE = True

# 是否自动更新缺失的角色数据并下载图标, 是否重载花名册
# 此选项会使您的仓库存在未提交的修改, 如果有影响请注意处理
PCRDATA_UPDATA = True

local_ver_path = os.path.join(os.path.dirname(__file__), 'local_ver.json')
local_pool_path = os.path.join(os.path.dirname(__file__), 'config.json')
local_pool_backup_path = os.path.join(os.path.dirname(__file__), 'backup.json')

# 卡池数据来源: autopcr 模块的国服 redive 库快照(官方资源服务器数据, 随游戏版本自动更新)
AUTOPCR_DB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'autopcr', 'cache', 'db'))
# 库快照超过多少天未更新时在日志中告警
DB_STALE_DAYS = 35

# 历代公主祭典初代限定(官方名字不带公主后缀, 号段规则覆盖不到)
FES_EXTRA_ROSTER_IDS = (1061, 1070, 1071)

# 这些1星单位是活动赠送/剧情单位, 不属于普通白金扭蛋的常驻1星池
NON_GACHA_1STAR_IDS = (1102, 1194, 1195, 1196, 1379)

online_pcr_data_url = 'https://raw.githubusercontent.com/Ice-Cirno/LandosolRoster/master/_pcr_data.py'


async def get_online_pcrdata():
    '''
    获取在线的角色数据信息, 并处理为json格式
    '''
    online_pcrdata = await aiorequests.get(url=online_pcr_data_url, timeout=10, verify=False)
    if online_pcrdata.status_code != 200:
        hoshino.logger.error(f'获取在线角色数据时发生错误{online_pcrdata.status_code}')
        return {}

    # 移除开头的'CHARA_NAME = ', 格式化为json便于处理
    online_pcrdata_text = await online_pcrdata.text
    # 处理兰德索尔花名册使其符合原先的gacha api数据
    pattern = r"CHARA_NAME\s*=\s*\{[^}]*\}"
    match = re.search(pattern, online_pcrdata_text)
    try:
        online_pcrdata_text = match.group(0).replace('CHARA_NAME = ', '')
    except:
        hoshino.logger.error('处理在线角色数据发生错误')
        return {}
    online_pcrdata_json = literal_eval(online_pcrdata_text)
    return online_pcrdata_json


async def update_pcrdata():
    '''
    对比本地和远程的_pcr_data.py, 自动补充本地没有的角色信息, 已有角色信息不受影响
    '''
    online_pcrdata = await get_online_pcrdata()

    hoshino.logger.info('开始对比角色数据')
    if online_pcrdata == {}:
        return -1
    for id in online_pcrdata:
        if id not in _pcr_data.CHARA_NAME and id != 9401:
            hoshino.logger.info(f'已开始更新角色{id}的数据和图标')
            # 由于返回数据可能出现全半角重复, 做一定程度的兼容性处理, 会将所有全角替换为半角, 并移除重复别称
            for i, name in enumerate(online_pcrdata[id]):
                name_format = name.replace('（', '(')
                name_format = name_format.replace('）', ')')
                online_pcrdata[id][i] = name_format

            # 转集合再转列表, 移除重复元素
            online_pcrdata[id] = list(set(online_pcrdata[id]))
            _pcr_data.chara_master.add_chara(id, online_pcrdata[id])
            await download_chara_icon(id, 6)
            await download_chara_icon(id, 3)
            await download_chara_icon(id, 1)
    # 重载花名册(不会引起全局reload)
    roster.update()


def find_latest_autopcr_db() -> str:
    '''
    找到 autopcr 目录下最新的 redive 库快照(按文件名取最大, 与 autopcr 自身取库的约定一致)
    返回完整路径, 找不到返回空字符串
    '''
    if not os.path.isdir(AUTOPCR_DB_DIR):
        return ''
    dbs = [f for f in os.listdir(AUTOPCR_DB_DIR) if f.endswith('.db')]
    if not dbs:
        return ''
    return os.path.join(AUTOPCR_DB_DIR, max(dbs))


def _parse_dbtime(s: str) -> tuple:
    '''
    库内日期格式混乱(斜杠/横杠混用、双空格、位数不齐), 统一解析为可比较的元组
    '''
    parts = [int(x) for x in re.split(r'[-/\s:]+', str(s).strip()) if x]
    return tuple((parts + [0] * 6)[:6])


def _ensure_unique_name(cid: int, base_name: str) -> str:
    '''
    当花名册中没有任何别名能唯一指向该角色时, 补一个带ID的唯一别名。
    仅用于UP/FES角色, 避免因重名/缺名导致当期卡池无法推导。
    '''
    base = (base_name or '').strip() or f'unit_{cid}'
    for candidate in (base, f'{base}({cid})', f'{base}#{cid}'):
        if not candidate:
            continue
        mapped = roster.get_id(candidate)
        if mapped == cid:
            return candidate
        if mapped == UNKNOWN:
            try:
                if cid in _pcr_data.CHARA_NAME:
                    _pcr_data.chara_master.add_nickname(cid, candidate)
                else:
                    _pcr_data.chara_master.add_chara(cid, [candidate])
                roster.update()
            except Exception:
                hoshino.logger.exception(f'为角色{cid}补充别名{candidate}失败')
                return ''
            if roster.get_id(candidate) == cid:
                hoshino.logger.warning(f'为角色{cid}补充唯一别名: {candidate}')
                return candidate
    return ''


def derive_pool_from_db(db_path: str) -> dict:
    '''
    从 autopcr 的国服 redive 库快照推导当前卡池数据
    up:       当前白金扭蛋(国服常规UP池)的UP角色名列表
    fes_up:   公主庆典UP角色名列表
    star3/2/1: 普通白金扭蛋的常驻3/2/1星角色名列表
    '''
    def unit2name(unit_id: int, fallback_name: str = '', ensure: bool = False) -> str:
        cid = unit_id // 100
        names = _pcr_data.CHARA_NAME.get(cid) or ()
        for name in names:
            # 只返回能唯一映射回该角色ID的名字, 避免重名/别名串号
            if name and roster.get_id(name) == cid:
                return name
        if ensure:
            return _ensure_unique_name(cid, fallback_name)
        return ''

    con = sqlite3.connect('file:' + db_path.replace('\\', '/') + '?mode=ro', uri=True)
    try:
        now = _parse_dbtime(time.strftime('%Y-%m-%d %H:%M:%S'))
        banners = con.execute(
            "SELECT gacha_id, exchange_id, start_time, end_time FROM gacha_data "
            "WHERE gacha_name LIKE '%白金扭蛋%' AND gacha_id < 100000").fetchall()
        active = [b for b in banners if _parse_dbtime(b[2]) <= now < _parse_dbtime(b[3])]
        if not active:
            raise ValueError('库快照中没有进行中的白金扭蛋')
        gacha_id, exchange_id, _, _ = max(active, key=lambda b: _parse_dbtime(b[2]))

        up_rows = con.execute(
            'SELECT DISTINCT l.unit_id, u.unit_name FROM gacha_exchange_lineup l '
            'LEFT JOIN unit_data u ON u.unit_id = l.unit_id '
            'WHERE l.exchange_id = ? AND l.rarity = 3',
            (exchange_id,)).fetchall()
        up = []
        for u, db_name in up_rows:
            name = unit2name(u, db_name, ensure=True)
            if name:
                if name not in up:
                    up.append(name)
            else:
                hoshino.logger.warning(f'白金扭蛋UP角色 unit_id={u} 无法映射为角色名, 已跳过')
        if not up:
            raise ValueError(f'当前白金扭蛋(gacha_id={gacha_id})没有可用的UP角色')

        # FES(公主庆典)常驻限定角色: 花名册1800号段的公主/组合系列 + 初代三先驱
        fes_rows = con.execute(
            'SELECT DISTINCT l.unit_id, u.unit_name FROM gacha_exchange_lineup l '
            'LEFT JOIN unit_data u ON u.unit_id = l.unit_id '
            'WHERE l.rarity = 3 AND l.exchange_id IN '
            "(SELECT exchange_id FROM gacha_data WHERE gacha_name = '公主庆典')").fetchall()
        fes_up = []
        for u, db_name in sorted(fes_rows):
            cid = u // 100
            if 1800 <= cid < 1900 or cid in FES_EXTRA_ROSTER_IDS:
                name = unit2name(u, db_name, ensure=True)
                if name:
                    if name not in fes_up:
                        fes_up.append(name)
                else:
                    hoshino.logger.warning(f'FES角色 unit_id={u} 无法映射为角色名, 已跳过')
        if not fes_up:
            hoshino.logger.warning('未从库中推导出FES角色, FES池将保持不变')

        # 普通白金扭蛋的常驻池: 排除限定、联动、活动赠送、NPC与1600以上的特殊号段
        permanent = {1: set(), 2: set(), 3: set()}
        rows = con.execute(
            'SELECT unit_id, rarity FROM unit_data '
            'WHERE is_limited = 0 AND is_collab = 0 AND unit_id < 160000 '
            'AND rarity IN (1, 2, 3)').fetchall()
        for unit_id, rarity in rows:
            cid = unit_id // 100
            if rarity == 1 and cid in NON_GACHA_1STAR_IDS:
                continue
            name = unit2name(unit_id)
            if name:
                permanent[rarity].add((cid, name))

        star3 = [name for _, name in sorted(permanent[3])]
        star2 = [name for _, name in sorted(permanent[2])]
        star1 = [name for _, name in sorted(permanent[1])]
        if not star3 or not star2 or not star1:
            raise ValueError('未能从库中推导出完整的常驻1/2/3星池')

        ver = int(re.sub(r'\D', '', os.path.basename(db_path)) or '0')
        return {
            'ver': ver,
            'gacha_id': gacha_id,
            'up': up,
            'fes_up': fes_up,
            'star3': star3,
            'star2': star2,
            'star1': star1,
        }
    finally:
        con.close()


def get_local_ver() -> int:
    '''
    获取本地版本号
    '''
    if os.path.exists(local_ver_path):
        with open(local_ver_path, 'r') as lv:
            lvj = json.load(lv)
            local_ver = int(lvj["ver"])
    else:
        local_ver = 0
    hoshino.logger.info(f'检查卡池更新, 本地卡池版本{local_ver}')
    return local_ver


def update_local_ver(ver: int) -> None:
    '''
    修改本地版本号
    '''
    local_ver_json = {"ver": str(ver)}
    hoshino.logger.info(f'写入本地版本号文件')
    with open(local_ver_path, 'w+', encoding='utf-8') as lvf:
        json.dump(local_ver_json, lvf, indent=4, ensure_ascii=False)


def update_local_pool(derived: dict) -> None:
    '''
    用库快照推导结果更新本地卡池文件, 并备份原卡池
    仅保留BL/FES两池: BL更新UP角色, FES刷新UP名单与常驻底池, 其余池(MIX/旧限定池)清除
    '''
    # 读取本地卡池
    with open(local_pool_path, 'r', encoding='utf-8') as lf:
        local_pool = json.load(lf)

    # 备份本地卡池
    hoshino.logger.info(f'开始备份本地卡池')
    with open(local_pool_backup_path, 'w', encoding='utf-8') as backup:
        json.dump(local_pool, backup, indent=4, ensure_ascii=False)

    # 池子已精简为BL/FES, 更新时顺带清掉残留的其余池(含MIX/旧JP/TW/旧限定池)
    for key in list(local_pool.keys()):
        if key not in ('BL', 'FES'):
            del local_pool[key]

    bl = local_pool.get('BL')
    if not isinstance(bl, dict):
        bl = {}
        local_pool['BL'] = bl
    bl['up'] = list(derived['up'])
    if derived.get('gacha_id') is not None:
        bl['banner_id'] = str(derived['gacha_id'])
    # 每次都用库快照重建常驻池, 不再做单向删除, 避免常驻角色永久消失
    bl['star3'] = list(derived['star3'])
    bl['star2'] = list(derived['star2'])
    bl['star1'] = list(derived['star1'])
    # 当前UP角色从BL的普通3星池中剔除; 换池后重建会自动加回
    for name in derived['up']:
        if name in bl['star3']:
            bl['star3'].remove(name)

    fes = local_pool.get('FES')
    if not isinstance(fes, dict):
        fes = {}
        local_pool['FES'] = fes
    if derived.get('fes_up'):
        fes['up'] = list(derived['fes_up'])
    fes_up_names = set(fes.get('up') or ())
    # FES的普通3星=常驻池; 明确排除FES UP, 不再直接复制BL的star3
    fes['star3'] = [name for name in derived['star3'] if name not in fes_up_names]
    fes['star2'] = list(derived['star2'])
    fes['star1'] = list(derived['star1'])

    # 将新卡池写入文件
    hoshino.logger.info(f'开始写入本地卡池文件')
    with open(local_pool_path, 'w+', encoding='utf-8') as lf:
        json.dump(local_pool, lf, indent=4, ensure_ascii=False)


async def update_pool(force=False) -> int:
    '''
    从 autopcr 的国服库快照推导当前卡池并覆盖本地的卡池
    1, 备份原卡池到backup.json
    2, 推导当前白金扭蛋的UP角色、FES UP与常驻1/2/3星池, 修改本地卡池数据
    3, 以库快照文件名为版本号, 覆盖到本地
    指定force为true, 则不会比较本地版本号是否最新
    '''
    if PCRDATA_UPDATA:
        try:
            await update_pcrdata()
        except Exception as e:
            hoshino.logger.exception(f'更新角色数据失败, 继续更新卡池: {repr(e)}')

    db_path = find_latest_autopcr_db()
    if not db_path:
        hoshino.logger.error(f'未找到autopcr库快照, 请检查目录: {AUTOPCR_DB_DIR}')
        return -1
    stale_days = (time.time() - os.path.getmtime(db_path)) / 86400
    if stale_days > DB_STALE_DAYS:
        hoshino.logger.warning(f'autopcr库快照已{stale_days:.0f}天未更新, 卡池数据可能过期: {os.path.basename(db_path)}')

    try:
        derived = derive_pool_from_db(db_path)
    except Exception as e:
        hoshino.logger.exception(f'从库快照推导卡池失败: {repr(e)}')
        return -2

    hoshino.logger.info(f"库快照{os.path.basename(db_path)}推导出当前UP角色: {'、'.join(derived['up'])} (gacha_id={derived['gacha_id']})")

    # 比较本地版本号
    local_ver = get_local_ver()
    if force:
        # 指定强制更新
        local_ver = 0
    elif local_ver > 10000000000000:
        # 旧版在线API的17位版本号与新版本号(12位)无法比较, 直接视为过期
        local_ver = 0

    # 语义变化不依赖数据库文件名版本: UP、轮换ID、常驻池任一变化都立即落盘
    try:
        with open(local_pool_path, encoding='utf-8') as lf:
            local_pool_now = json.load(lf)
        local_bl = local_pool_now.get('BL') or {}
        local_fes = local_pool_now.get('FES') or {}

        bl_up = list(derived['up'])
        bl_star3 = list(derived['star3'])
        for name in bl_up:
            if name in bl_star3:
                bl_star3.remove(name)
        fes_up = derived.get('fes_up') or list(local_fes.get('up') or ())
        fes_up_names = set(fes_up)
        fes_star3 = [name for name in derived['star3'] if name not in fes_up_names]

        need_rewrite = (
            set(local_pool_now.keys()) != {'BL', 'FES'}
            or local_bl.get('up') != bl_up
            or (derived.get('gacha_id') is not None
                and str(local_bl.get('banner_id')) != str(derived.get('gacha_id')))
            or local_bl.get('star3') != bl_star3
            or local_bl.get('star2') != derived['star2']
            or local_bl.get('star1') != derived['star1']
            or (bool(derived.get('fes_up')) and local_fes.get('up') != derived['fes_up'])
            or local_fes.get('star3') != fes_star3
            or local_fes.get('star2') != derived['star2']
            or local_fes.get('star1') != derived['star1']
        )
    except Exception:
        need_rewrite = True

    if derived['ver'] > local_ver or need_rewrite:
        # 修改本地卡池
        update_local_pool(derived)
        # 覆盖本地版本号
        update_local_ver(derived['ver'])
        return derived['ver']
    return 0


@on_command('更新卡池', only_to_me=False)
async def update_pool_chat(session):
    '''
    手动更新卡池时试用此命令
    '''
    if not priv.check_priv(session.event, priv.SUPERUSER):
        return
    status = await update_pool()
    if status == 0:
        await session.finish('已是最新版本, 仍要更新卡池请使用【强制更新卡池】命令')
    elif status < 1000:
        await session.finish(f'发生错误{status}')
    else:
        await session.finish(f'更新完成, 当前卡池版本{status}')


@on_command('强制更新卡池', only_to_me=False)
async def update_pool_force_chat(session):
    '''
    强制更新卡池
    '''
    if not priv.check_priv(session.event, priv.SUPERUSER):
        return
    status = await update_pool(force=True)
    if status == 0:
        await session.finish(f'状态{status}')
    elif status < 1000:
        await session.finish(f'发生错误{status}')
    else:
        await session.finish(f'更新完成, 当前卡池版本{status}')


# 连续同一种错误只通知一次, 恢复后再次失败才重新通知
_last_error_status = None


@scheduler.scheduled_job('cron', minute='05')
async def update_pool_sdj():
    global _last_error_status
    bot = get_bot()
    master_id = hoshino.config.SUPERUSERS[0]

    status = await update_pool()
    if status == 0:
        _last_error_status = None
        return
    if not NOTICE:
        return
    if status < 1000:
        if status == _last_error_status:
            return
        _last_error_status = status
        msg = f'自动更新卡池时发生错误{status}'
    else:
        _last_error_status = None
        msg = f'已自动更新卡池，当前版本{status}'
    await bot.send_private_msg(user_id=master_id, message=msg)
