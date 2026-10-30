# -*- coding: utf-8 -*-
import json
import os
import re
import time
import sqlite3

import hoshino
from hoshino import aiorequests, priv, util
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

# 卡池选择优先级(越靠前越优先): 精选扭蛋是新角色池, 公主庆典其次, 白金扭蛋是常驻池
BANNER_PRIORITY = ('精选扭蛋', '公主庆典', '白金扭蛋')
# 付费必得池与自选附奖池不参与模拟
BANNER_EXCLUDE = ('必得', '自选附奖')
# gacha_data.pick_up_chara_text 中表示“无精选”的占位文本
NO_PICKUP_TEXT = '没有精选角色'

# 官方公示概率(千分制): 常规卡池★3为3%, 公主庆典期间★3出率提升为2倍(6%)
S3_PROB_NORMAL = 30
S3_PROB_FES = 60

online_pcr_data_url = 'https://raw.githubusercontent.com/Ice-Cirno/LandosolRoster/master/chara_name.json'


def _name_owner_map() -> dict:
    '''归一化别名 -> 角色id, 用于保证合并后花名册无重名'''
    return {util.normalize_str(n): cid
            for cid, names in _pcr_data.CHARA_NAME.items()
            for n in names}


async def get_online_pcrdata() -> dict:
    '''
    获取在线的角色数据信息(chara_name.json), 返回 {id: [别名...]}
    '''
    try:
        rsp = await aiorequests.get(url=online_pcr_data_url, timeout=10, verify=False)
        if rsp.status_code != 200:
            hoshino.logger.error(f'获取在线角色数据时发生错误{rsp.status_code}')
            return {}
        data = json.loads(await rsp.text)
        online = {}
        for cid, names in data.items():
            names = [str(n).strip() for n in names if str(n).strip()]
            if names:
                online[int(cid)] = names
        return online
    except Exception as e:
        hoshino.logger.exception(f'解析在线角色数据失败: {repr(e)}')
        return {}


async def update_pcrdata() -> int:
    '''
    与远程花名册做并集合并, 返回本次新增的 id 与别名数量
    - 新角色: 新增 id 并下载图标
    - 已有角色: 补齐缺失别名; 显示名排序由 chara_name_fix.py 定时处理
    - 归一化后与已有别名冲突的条目一律跳过, 保证花名册无重名
    - 本地独有的角色/别名不会被删除
    '''
    online_pcrdata = await get_online_pcrdata()
    if not online_pcrdata:
        return -1

    hoshino.logger.info('开始对比角色数据')
    owner = _name_owner_map()
    added_ids = added_names = 0
    changed = False

    for cid, names in online_pcrdata.items():
        if cid == 9401:
            continue

        # 1) 新角色: 整条新增
        if cid not in _pcr_data.CHARA_NAME:
            picked = []
            for n in names:
                key = util.normalize_str(n)
                if key in owner:
                    continue
                picked.append(n)
                owner[key] = cid
            if not picked:
                n = f'{names[0]}({cid})'
                picked = [n]
                owner[util.normalize_str(n)] = cid
            hoshino.logger.info(f'已开始更新角色{cid}的数据和图标')
            _pcr_data.CHARA_NAME[cid] = picked
            changed = True
            added_ids += 1
            for star in (6, 3, 1):
                try:
                    await download_chara_icon(cid, star)
                except Exception as e:
                    hoshino.logger.error(f'下载角色{cid} {star}星头像失败: {repr(e)}')
            continue

        cur = _pcr_data.CHARA_NAME[cid]

        # 2) 已有角色: 补齐缺失别名
        merged = list(cur)
        seen = {util.normalize_str(n) for n in cur}
        for n in names:
            key = util.normalize_str(n)
            if key in seen or (key in owner and owner[key] != cid):
                continue
            merged.append(n)
            seen.add(key)
            owner[key] = cid
            added_names += 1
        if len(merged) != len(cur):
            _pcr_data.CHARA_NAME[cid] = merged
            changed = True

    if changed:
        _pcr_data.chara_master.save_name()
    # 重载花名册(不会引起全局reload)
    roster.update()
    hoshino.logger.info(f'角色数据同步完成: 新增id {added_ids}, 新增别名 {added_names}')
    return added_ids + added_names


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


def _pickup_names(text: str) -> list:
    '''拆解 gacha_data.pick_up_chara_text，返回当期精选角色名'''
    return [n.strip() for n in re.split(r'[\n、，,／/]', text or '')
            if n.strip() and n.strip() != NO_PICKUP_TEXT]


def _canonical_name(name: str, ensure: bool = False) -> str:
    '''把官方角色名映射为花名册中可唯一反查的别名'''
    cid = roster.get_id(name)
    if cid == UNKNOWN:
        return ''
    for alias in _pcr_data.CHARA_NAME.get(cid, ()):
        if roster.get_id(alias) == cid:
            return alias
    return _ensure_unique_name(cid, name) if ensure else ''


def _active_banners(con, now: tuple) -> list:
    '''返回进行中、可用于模拟的卡池，按优先级与开始时间排序'''
    rows = con.execute(
        'SELECT gacha_id, gacha_name, exchange_id, start_time, end_time, pick_up_chara_text '
        'FROM gacha_data WHERE gacha_id < 100000').fetchall()
    active = [r for r in rows
              if _parse_dbtime(r[3]) <= now < _parse_dbtime(r[4])
              and not any(k in r[1] for k in BANNER_EXCLUDE)
              and _pickup_names(r[5])]
    priority = {key: len(BANNER_PRIORITY) - i for i, key in enumerate(BANNER_PRIORITY)}

    def _rank(row):
        # 同期多个精选池并存时取最新开启的那个(游戏内主推池), 同开始时间再按类型优先级
        name = row[1]
        score = next((v for k, v in priority.items() if k in name), 0)
        return _parse_dbtime(row[3]), score
    return sorted(active, key=_rank, reverse=True)


def derive_pool_from_db(db_path: str) -> dict:
    '''
    从 autopcr 的国服 redive 库快照推导当前卡池数据
    up:       当期主打卡池(精选/公主庆典/白金)的精选角色名列表
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
        # 当期UP取自 gacha_data.pick_up_chara_text(官方精选角色), 不再用天井兑换列表:
        # 兑换列表会把当期所有可兑换限定角色都算成UP, 单个角色出率被稀释到几百分之一
        banner = _active_banners(con, now)
        if not banner:
            raise ValueError('库快照中没有带精选角色的进行中卡池')
        banner = banner[0]
        gacha_id, gacha_name, exchange_id, _, _, pickup_text = banner

        up, up_cids = [], set()
        for name in _pickup_names(pickup_text):
            canonical = _canonical_name(name, ensure=True)
            if not canonical:
                hoshino.logger.warning(f'{gacha_name}UP角色“{name}”无法映射到花名册, 已跳过')
                continue
            cid = roster.get_id(canonical)
            if cid != UNKNOWN and cid not in up_cids:
                up_cids.add(cid)
                up.append(canonical)
        if not up:
            raise ValueError(f'{gacha_name}(gacha_id={gacha_id})的UP角色无法映射为角色名')

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
            'banner_name': gacha_name,
            's3_prob': S3_PROB_FES if '公主庆典' in gacha_name else S3_PROB_NORMAL,
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
    if derived.get('s3_prob'):
        bl['s3_prob'] = derived['s3_prob']
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

    hoshino.logger.info(
        f"库快照{os.path.basename(db_path)}推导出当前卡池: {derived.get('banner_name')}"
        f"(gacha_id={derived['gacha_id']}) UP: {'、'.join(derived['up'])}")

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
            or local_bl.get('s3_prob') != derived.get('s3_prob')
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
