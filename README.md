# Aya-pcr-gacha

适用于 [HoshinoBot](https://github.com/Ice-Cirno/HoshinoBot) 的公主连结模拟抽卡插件，保留 BL（B服实时卡池）与 FES（公主庆典）两个模拟池。

本仓库是 [pcrbot/gacha](https://github.com/pcrbot/gacha) 的修改版，卡池数据改为从 autopcr 的国服 redive 库快照推导。

## 功能

- BL / FES 双卡池，`切换卡池` 在两个池之间直接互切
- 单抽、十连、200 抽（来一井）
- 不限制每日抽卡次数，也不再有氪金指令
- 按抽卡记录宝石消耗：
  - 单抽 150 宝石
  - 十连 1500 宝石
  - 来一井 200 抽，30000 宝石
- 统计每位用户的：
  - 当日累计消耗宝石（每天 5:00 自动轮换）
  - 当前卡池累计消耗宝石
  - 当前卡池累计获得 3★ 数量、UP 角色数量
- BL 的累计统计随卡池轮换自动重置；FES 的统计永久累计

## 依赖

- `hoshino/modules/autopcr` 的 `cache/db/*.db` 国服 redive 库快照
- Python 3.8+
- HoshinoBot/priconne 已有的 Pillow、pygtrie、fuzzywuzzy 等依赖

## 安装

将本仓库放到：

```text
hoshino/modules/priconne/gacha/
```

确保 `priconne` 与 `autopcr` 已启用，并让 autopcr 至少生成一份 `cache/db/*.db` 快照。

## 指令

| 指令 | 说明 |
|---|---|
| `查看卡池` / `看看UP` | 查看当前卡池 UP 和出率；多 UP 池只发文字名单，避免刷屏 |
| `切换卡池` | 在 BL / FES 之间切换 |
| `单抽` | 消耗 150 宝石 |
| `十连` | 消耗 1500 宝石，第 10 抽保底 2★ 以上 |
| `来一井` | 200 抽，消耗 30000 宝石 |
| `更新卡池` | 超级管理员可用，从 autopcr 库快照更新 |
| `强制更新卡池` | 超级管理员可用，忽略版本强制更新 |

## 数据更新

- 定时任务每小时第 5 分钟执行一次
- 优先从 autopcr 最新库快照推导：
  - 当前白金扭蛋 UP
  - 公主庆典 UP
  - 常驻 1/2/3 星池
- 如果 autopcr 库快照中没有进行中的白金扭蛋，BL 卡池会保持上一次结果，并记录错误
- `update.py` 会尝试从 GitHub raw 更新角色花名册；不可达时继续使用本地 `CHARA_NAME.json`

## 配置

`config.json` 保存两个池的概率、UP 名单和常驻名单。更新时会重写 UP 与常驻名单，并保留概率配置。

## 许可

GPL-3.0。原项目及代码作者信息见 [LICENSE](LICENSE) 与上游项目说明。

- 原模块基于 0皆无0 的“PCR姬器人：可可萝·Android”，由 IceCoffee 重构并移植至 nonebot。
- 上游项目：[pcrbot/gacha](https://github.com/pcrbot/gacha)
