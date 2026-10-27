import os
import random
from collections import defaultdict

from hoshino import Service
from hoshino.typing import *
from hoshino.util import concat_pic, pic2b64

from .. import chara
from .gacha import Gacha
from .update import *
from .stats import record_draw, format_stats

try:
    import ujson as json
except ImportError:
    import json

sv_help = '''
[@Bot十连] 转蛋模拟
[@Bot来发单抽] 转蛋模拟
[@Bot来一井] 200抽（30000宝石）
[查看卡池] 模拟卡池&出率
[切换卡池] 在B服池/公主庆典池之间切换
'''.strip()
sv = Service('PCR模拟抽卡', help_=sv_help, bundle='pcr娱乐')

JEWEL_PER_PULL = 150
SPARK_PULLS = 200

# 仅保留 B服(实时卡池)与 FES(公主庆典)两个模拟池
DEFAULT_POOL = 'BL'

_pool_config_file = os.path.expanduser('~/.hoshino/group_pool_config.json')
_group_pool = {}
try:
    with open(_pool_config_file, encoding='utf8') as f:
        _group_pool = json.load(f)
except FileNotFoundError as e:
    sv.logger.warning('group_pool_config.json not found, will create when needed.')
_group_pool = defaultdict(lambda: DEFAULT_POOL, _group_pool)


def dump_pool_config():
    with open(_pool_config_file, 'w', encoding='utf8') as f:
        json.dump(_group_pool, f, ensure_ascii=False)


# 旧版本可能保存了已移除的卡池(JP/TW/MIX/旧限定池), 迁移到B服池
_migrated = False
for _gid, _p in list(_group_pool.items()):
    if _p not in ('BL', 'FES'):
        _group_pool[_gid] = 'BL'
        _migrated = True
        sv.logger.info(f'群{_gid}的卡池{_p}已不存在, 已迁移为BL池')
if _migrated:
    dump_pool_config()


gacha_10_aliases = ('抽十连', '十连', '十连！', '十连抽', '来个十连', '来发十连', '来次十连', '抽个十连', '抽发十连', '抽次十连', '十连扭蛋', '扭蛋十连',
                    '10连', '10连！', '10连抽', '来个10连')
gacha_1_aliases = ('单抽', '单抽！', '来发单抽', '来个单抽', '来次单抽', '扭蛋单抽', '单抽扭蛋')
gacha_200_aliases = ('抽一井', '来一井', '来发井', '抽发井', '天井扭蛋', '扭蛋天井')


def _up_ids(gacha: Gacha):
    ids = set()
    for name in gacha.up:
        cid = chara.name2id(name)
        if cid != chara.UNKNOWN:
            ids.add(cid)
    return ids


def _pool_key(pool_name: str, gacha: Gacha):
    if pool_name == 'FES':
        return 'FES'
    banner_id = getattr(gacha, 'banner_id', None)
    up_sig = '|'.join(sorted(gacha.up))
    return f'{banner_id}:{up_sig}' if banner_id else up_sig


def _record_and_format(user_id, pool_name, gacha, chars, pulls):
    stats = record_draw(
        user_id=user_id,
        pool_name=pool_name,
        pool_key=_pool_key(pool_name, gacha),
        cost=pulls * JEWEL_PER_PULL,
        chars=chars,
        up_ids=_up_ids(gacha),
    )
    return format_stats(stats)


@sv.on_fullmatch(('卡池资讯', '查看卡池', '看看卡池', '康康卡池','看看up', '看看UP'))
async def gacha_info(bot, ev: CQEvent):
    gid = str(ev.group_id)
    gacha = Gacha(_group_pool[gid])
    up_names = gacha.up
    if len(up_names) <= 2:
        # 少量UP时保留头像+名字的旧展示方式
        up_chara = [str(await chara.fromname(x, star=3).get_icon_cqcode()) + x for x in up_names]
        up_chara = '\n'.join(up_chara)
    else:
        # FES等多UP池只发文字名单, 避免一次刷出一大排头像
        up_chara = f'（共{len(up_names)}名）\n' + '、'.join(up_names)
    await bot.send(ev, f"本期卡池主打的角色：\n{up_chara}\nUP角色合计={(gacha.up_prob/10):.1f}% 3★出率={(gacha.s3_prob)/10:.1f}%\n")


@sv.on_prefix('切换卡池')
async def set_pool(bot, ev: CQEvent):
    gid = str(ev.group_id)
    current = _group_pool[gid]
    name = 'FES' if current == 'BL' else 'BL'
    _group_pool[gid] = name
    dump_pool_config()
    await bot.send(ev, f'卡池已切换为{name}池', at_sender=True)
    # 切换后仅在UP角色较少时展示详情; 多UP池可用“查看卡池”查看文字名单
    gacha = Gacha(name)
    if len(gacha.up) <= 2:
        await gacha_info(bot, ev)


@sv.on_prefix(gacha_1_aliases, only_to_me=True)
async def gacha_1(bot, ev: CQEvent):
    gid = str(ev.group_id)
    pool_name = _group_pool[gid]
    gacha = Gacha(pool_name)
    result_chara, _ = gacha.gacha_one(gacha.up_prob, gacha.s3_prob, gacha.s2_prob)
    stats_text = _record_and_format(ev.user_id, pool_name, gacha, [result_chara], 1)

    res = f'{result_chara.name} {"★"*result_chara.star}'
    # 原icon属性对1-5星均取3星头像，此处固定3星保持原显示
    cq = await result_chara.get_icon_cqcode(3)
    res = f'{cq} {res}'

    await bot.send(ev, f'素敵な仲間が増えますよ！\n{res}\n{stats_text}', at_sender=True)


@sv.on_prefix(gacha_10_aliases, only_to_me=True)
async def gacha_10(bot, ev: CQEvent):
    SUPER_LUCKY_LINE = 170

    gid = str(ev.group_id)
    pool_name = _group_pool[gid]
    gacha = Gacha(pool_name)
    result, hiishi = gacha.gacha_ten()
    stats_text = _record_and_format(ev.user_id, pool_name, gacha, result, 10)

    res1 = await chara.gen_team_pic(result[:5], star_slot_verbose=False)
    res2 = await chara.gen_team_pic(result[5:], star_slot_verbose=False)
    res = concat_pic([res1, res2])
    res = pic2b64(res)
    res = MessageSegment.image(res)
    result_text = [f'{c.name}{"★"*c.star}' for c in result]
    res1_text = ' '.join(result_text[0:5])
    res2_text = ' '.join(result_text[5:])
    res = f'{res}\n{res1_text}\n{res2_text}\n{stats_text}'

    if hiishi >= SUPER_LUCKY_LINE:
        await bot.send(ev, '恭喜海豹！おめでとうございます！')
    await bot.send(ev, f'素敵な仲間が増えますよ！\n{res}\n', at_sender=True)


@sv.on_prefix(gacha_200_aliases, only_to_me=True)
async def gacha_200(bot, ev: CQEvent):
    gid = str(ev.group_id)
    pool_name = _group_pool[gid]
    gacha = Gacha(pool_name)
    result = gacha.gacha_tenjou()
    stars = [*(result['up']), *(result['s3'])]
    stats_text = _record_and_format(ev.user_id, pool_name, gacha, stars, SPARK_PULLS)

    up = len(result['up'])
    s3 = len(result['s3'])
    s2 = len(result['s2'])
    s1 = len(result['s1'])

    res = [*stars]
    random.shuffle(res)
    lenth = len(res)
    if lenth <= 0:
        res = "竟...竟然没有3★？！"
    else:
        step = 4
        pics = []
        for i in range(0, lenth, step):
            j = min(lenth, i + step)
            pics.append(await chara.gen_team_pic(res[i:j], star_slot_verbose=False))
        res = concat_pic(pics)
        res = pic2b64(res)
        res = MessageSegment.image(res)

    msg = [
        f"\n素敵な仲間が増えますよ！ {res}",
        f"★★★×{up+s3} ★★×{s2} ★×{s1}",
        f"获得记忆碎片×{gacha.memo_pieces*up}与女神秘石×{50*(up+s3) + 10*s2 + s1}！\n第{result['first_up_pos']}抽首次获得up角色" if up else f"获得女神秘石{50*(up+s3) + 10*s2 + s1}个！"
    ]

    if up == 0 and s3 == 0:
        msg.append("太惨了，咱们还是退款删游吧...")
    elif up == 0 and s3 > 7:
        msg.append("up呢？我的up呢？")
    elif up == 0 and s3 <= 3:
        msg.append("这位酋长，梦幻包考虑一下？")
    elif up == 0:
        msg.append("据说天井的概率只有" + gacha.tenjou_rate)
    elif up <= 2:
        if result['first_up_pos'] < 50:
            msg.append("你的喜悦我收到了，滚去喂鲨鱼吧！")
        elif result['first_up_pos'] < 100:
            msg.append("已经可以了，您已经很欧了")
        elif result['first_up_pos'] > gacha.tenjou_line - 10:
            msg.append("标 准 结 局")
        elif result['first_up_pos'] > gacha.tenjou_line - 50:
            msg.append("补井还是不补井，这是一个问题...")
        else:
            msg.append("期望之内，亚洲水平")
    elif up == 3:
        msg.append("抽井母五一气呵成！多出30等专武～")
    elif up >= 4:
        msg.append("记忆碎片一大堆！您是托吧？")

    msg.append(stats_text)
    await bot.send(ev, '\n'.join(msg), at_sender=True)
