# entropy-arb

**[English documentation / 英文文档 → README.md](README.md)**

开源双交易所永续合约套利机器人。主腿通过 `--primary` 选择，默认 **RH**
（`light-rh` / `lighter-rh`）；也可选 `lighter`、`entropy`（Hyperliquid 的
`io` dex）或 `tradexyz`。对冲腿通过 `--hedge` 选择，默认 **LIGHTER 主网**：

| `--hedge` | 交易所 | 计价货币 | 吃单费 | 协议 |
|---|---|---|---|---|
| `lighter` | Lighter 主网 | USDC | 0 bps | zkLighter ws（增量订单簿，异步结算） |
| `lighter-rh` | Lighter Robinhood 链 | **USDG** | 0 bps | zkLighter ws |
| `tradexyz` | Hyperliquid trade.xyz dex | USDC | ~1 bps | HL l2Book，IOC 同步结算 |

> **推荐链接** —— 通过以下链接注册即可支持本项目：
> - Entropy — Tier 4 推荐，100% 返佣：<https://entropy.io/?r=yourquantguy>
> - Lighter Robinhood 链：<https://robinhoodchain.lighter.xyz/?referral=QUANT>
> - trade.xyz（Hyperliquid）：<https://app.hyperliquid.xyz/join/QUANTGUY>

当同一品种在一边贵、另一边便宜时，机器人同时在贵的一边卖出、便宜的一边买入
（均为吃单），持有 delta 中性仓位，等溢价回归后反向平仓。所有交易决策使用的
价格都来自**将要实际成交的那个交易所的真实订单簿**——Hyperliquid 的盘口来自
官方 websocket（`wss://api.hyperliquid.xyz/ws`），Lighter 的盘口来自 Lighter
官方 websocket。

机器人运行期间（即使没有密钥、没有开策略）会自动把两边盘口记录成**分钟级
CSV 数据**，配套的分析工具可以直接把这些数据变成策略所需的三个核心参数。

## 信号逻辑

整个信号就是 `config.yaml` 里三个数字，由你根据采集的数据自己设定：

```
premium_bps =（Entropy 价格 / 对冲腿价格 − 1）× 10 000

                          ┌──────────────  卖出 Entropy + 买入对冲腿
midline + upper  ───────────────────────────────────────────────────
                                       ▲
midline          ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┼ ─ ─   溢价的长期中枢
                                       ▼
midline − lower  ───────────────────────────────────────────────────
                          └──────────────  买入 Entropy + 卖出对冲腿
```

- `midline_bps` —— 溢价的常态水平。跨所溢价几乎从不以零为中心（预言机不同、
  计价货币不同、新上市溢价等），零中心的带只会朝一个方向开仓、打满仓位上限、
  永远无法平仓。请实际测量溢价所在的位置，然后填入。
- `upper_bps` / `lower_bps` —— 中枢上下两侧的入场带宽。

两个方向的门槛都作用于**可实际成交的价格**（Entropy 买一 对 对冲腿卖一，
反之亦然），并且是**扣除双边吃单手续费之后的净门槛**——引擎会在阈值之上
另行叠加手续费。因此一次完整往返扣费后**净赚 ≥ upper + lower bps**，这是
结构上保证的。

有一点必须理解：当 `midline_bps: 5` 时，买入 Entropy 的门槛是
`lower − midline`，可能为**负数**。这是有意为之——如果 Entropy 长期贵 5 bps，
那么在溢价为 0 时买入它，相对其自身均衡水平就是便宜了 5 bps，这笔交易正是
此前在 `midline + upper` 处卖出的获利平仓。这同时意味着**中枢填错就是亏钱
策略**：若真实溢价中枢是 0 而你填了 5，机器人会整天以公允价买入 Entropy。
先测量、再交易——数据采集器和分析工具就是为此而生。

## 快速开始

```bash
git clone https://github.com/your-quantguy/entropy-arb.git && cd entropy-arb
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # 数据采集只需要这些

cp config.example.yaml config.yaml       # 策略配置（阈值、规模、风控）
cp .env.example .env                     # 密钥——交易必填
```

`--symbol` 必填；主腿默认读取 `.env` 中的 `PRIMARY_VENUE=light-rh`，
命令行 `--primary` 优先；`--hedge` 默认 `lighter`。两条腿不能选同一市场。

```bash
# 默认 RH 主腿 + LIGHTER 主网对冲腿
python3 main.py --record-only --symbol LIT
# 原 Entropy + RH 组合需显式指定主腿
python3 main.py --record-only --symbol SNDK --primary entropy --hedge lighter-rh
```

更换交易组合后，需要重新采集数据并设定溢价中枢，不能直接沿用旧组合的阈值。
文档沿用 Entropy 作为信号示例；运行时公式中的 Entropy 实际指所选主腿。

本机器人**没有模拟盘**——要么采集数据（`--record-only`），要么实盘交易。
请用采集的数据和最小的仓位上限来验证策略，而不是模拟成交。

**第一步：先采集数据**（不需要任何密钥）：

```bash
python3 main.py --record-only --symbol SNDK --primary entropy --hedge lighter-rh
```

至少运行几个小时（最好一整天——溢价存在日内规律），数据写入
`logs/{symbol}/minutes.csv`。

**第二步：分析数据、设定阈值：**

```bash
python3 tools/analyze.py --csv logs/sndk/minutes.csv
```

它会输出溢价分布、各档带宽的历史触发频率，以及可直接粘贴进
`config.yaml` 的 `thresholds:` 配置块。

**第三步：实盘** —— 填写 `.env`，安装签名 SDK，仓位上限从刚好满足
交易所最小名义的水平开始：

```bash
pip install -r requirements-live.txt
python3 main.py --symbol SNDK --primary entropy --hedge lighter-rh
```

不带 `--record-only` 运行时，只要两边行情就绪且溢价越过带宽，就会立即
发送真实订单。

**仪表盘。** 在终端运行时会显示实时 Rich 仪表盘：两边盘口（含数据龄/点差）、
持仓与上限、账户权益与本次会话盈亏、两个方向的可成交溢价对比完整门槛
（已含手续费与库存加价，● 表示已武装）、数据采集进度、最近成交，以及日志
尾部（完整日志写入 `logging.file`，默认 `logs/{symbol}/engine.log`）。`--record-only`
模式同样可用。加 `--cn` 参数可使仪表盘全部以中文显示。`--no-dashboard`
可切换为纯日志输出（nohup/systemd 等非终端环境会自动退回纯日志），也可
设置 `logging.dashboard: false`。

## 数据采集与分析

采集器在所有模式下自动运行（`recorder.enabled: true`）：每秒采样一次两边
的真实盘口，每分钟写一行：

| 列 | 含义 |
|---|---|
| `minute_ts`, `time_utc` | 分钟起点（epoch 秒 / ISO UTC） |
| `entropy_bid/ask`, `hedge_bid/ask` | 该分钟最后一次有效盘口 |
| `premium_open/high/low/close/mean/std_bps` | Entropy 相对对冲腿的中间价溢价 |
| `sell_edge_mean/max_bps` | 卖出 Entropy 方向的可成交溢价（Entropy 买一 / 对冲腿卖一 − 1） |
| `buy_edge_mean/max_bps` | 买入 Entropy 方向的可成交溢价（对冲腿买一 / Entropy 卖一 − 1） |
| `samples` | 该分钟约 60 秒中两边盘口同时有效的秒数 |

采集的 edge 为费前口径；分析工具在统计触发频率前会先扣除 `--fees-bps`
（请传入**两边吃单费之和**——零费交易所默认 0.0，对冲腿为 `tradexyz` 时
约为 1.0），因此其表格与建议值可直接填入配置。`--hours 24`
可只分析最近数据；溢价中枢会漂移，请定期重新分析并更新 `config.yaml`。

## 配置说明

策略在 `config.yaml`（严格校验——未知键名直接报错），密钥在 `.env`。
交易市场由 `--symbol`、`--primary`、`--hedge` 指定。主腿参数可放在
`primary:` 段；选择 Entropy 时兼容 `entropy:` 段，`primary:` 同名项优先。完整的双语注释参考：
[config.example.yaml](config.example.yaml)。核心项：

| 键 | 含义 | 默认值 |
|---|---|---|
| `thresholds.midline_bps` | 溢价中枢（必须实测！） | — |
| `thresholds.upper_bps` / `lower_bps` | 入场带宽（> 0） | — |
| `entropy.dex` | Entropy 在 Hyperliquid 上的 dex 名 | `io` |
| `*.taker_fee_bps` | 各所吃单费 | 0.0（tradexyz 对冲腿：1.0） |
| `*.max_position_usd` | 各所持仓上限 | 1000 |
| `*.max_orders_per_min` | 各所每分钟下单预算（滑动 60 秒） | 120；Lighter 对冲腿 30 |
| `sizing.take_fraction` | 吃掉可套利深度的比例 | 0.5 |
| `sizing.max_order_notional_usd` | 单笔名义上限 | 500 |
| `inventory.scale_bps` / `floor_frac` | 库存阶梯（仓位超过上限的 `floor_frac` 后额外加价） | 10 / 0.5 |
| `execution.premium_persist_sec` | 信号需持续多久才触发 | 0.3 |
| `execution.lead_venue` / `lead_slippage_bps` | 顺序执行：哪条腿先用紧限价 IOC 下单 | `none` / 0 |
| `execution.*` | 滑点保护、超时、对账周期等 | 见配置文件 |
| `recorder.*` | 分钟数据采集器 | 开启，`logs/{symbol}/minutes.csv` |
| `logging.dashboard` / `logging.file` | 终端仪表盘；所有模式都持续写入日志文件 | 开启，`logs/{symbol}/engine.log` |

日志自动按 `--symbol` 的小写名称建目录。例如 `--symbol LIT` 会写入
`logs/lit/minutes.csv`、`logs/lit/trades.csv` 和 `logs/lit/engine.log`；
切换成 BTC 则写入 `logs/btc/` 目录，同币种重启会继续追加；分钟采集器会避免重启时重复写入同一个未完成分钟。
可在 `recorder.csv`、`logging.trades_csv`、`logging.file` 中使用 `{symbol}`
自定义路径，例如 `logs/{symbol}/trades.csv`。旧的固定路径也会自动插入小写币种目录，
例如 `logs/trades.csv` 会变成 `logs/btc/trades.csv`；已有日志不会被改名或迁移。分析时用 `--csv` 指定对应币种的分钟文件。
同币种的不同交易所组合或多个进程仍会共享路径，需要隔离时使用不同的目录配置。

## 密钥配置（`.env`，仅实盘需要）

- **Entropy / tradexyz（Hyperliquid）** —— 在
  <https://app.hyperliquid.xyz/API> 创建 API（agent）钱包。`HL_PRIVATE_KEY`
  填 **agent 钱包私钥**，`HL_ACCOUNT_ADDRESS` 填主账户地址。当
  `--hedge tradexyz` 时两条腿默认共用该账户（内部自动共享 nonce 序列）；
  如需分开，设置 `HL_PRIVATE_KEY_XYZ` / `HL_ACCOUNT_ADDRESS_XYZ`。注意给
  所交易的各 dex 分别充入保证金。
- **Lighter** —— `LIGHTER_ACCOUNT_INDEX`、`LIGHTER_API_KEY_INDEX`、
  `LIGHTER_API_PRIVATE_KEY` 用于主网；RH 使用独立的 `LIGHTER_RH_ACCOUNT_INDEX`、
  `LIGHTER_RH_API_KEY_INDEX`、`LIGHTER_RH_API_PRIVATE_KEY`。默认组合必须填写两组。
  原先只有 RH 一条 Lighter 腿时仍兼容旧 `LIGHTER_*` 配置；主网与 RH 同时使用时
  不会共用密钥（参见
  [lighter-python](https://github.com/elliottech/lighter-python)）。

## Telegram 成交通知

在 `.env` 同时填写 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID` 后启用；两项留空则关闭。
先在 Telegram 私聊启动 bot，或把 bot 加入目标群。通知使用
[Telegram sendMessage API](https://core.telegram.org/bots/api#sendmessage)。

每次双腿执行有成交或结果待确认时，按中文模板推送品种、状态、`sell_primary` / `buy_primary`、
买卖交易所及成交量、计划数量、双边成交额、本次和累计盈利、对冲笔数、两所可用余额与权益、
账户权益变化。部分成交、异常和待确认不会显示为“完成”；未知数据显示 `—`。
通知在后台异步发送，失败只记日志，不影响下单和补对冲；退出时最多等待 10 秒发送队列。
发送失败不自动重试，断网或进程被强制结束时可能丢失通知。

统计沿用现有引擎：盈利是两腿**匹配成交量的价差减配置手续费**（`fill_edge`），
不包含资金费、未匹配敞口的盈亏或补对冲成本；累计值从本次启动开始计算。
成交价未知时现有累计成交额仍可能按计划限价估算；当前通知不会把未知的本次金额、盈利填成零。
余额及账户权益变化使用现有每 30 秒轮询的最新快照，并非逐笔刷新；对冲笔数沿用已有补对冲尝试计数。
原 CSV 列名及内部 `sell_entropy` / `buy_entropy` 标识保留兼容，现在对应所选主腿。

交易 CSV 现在还记录实际平均成交价、手续费率和手续费金额、客户端及交易所订单 ID、
下单时的买卖一档盘口及盘口年龄，以及每条腿的 `submit_latency_ms`、
`confirm_latency_ms`、`total_latency_ms` 和并发双腿总耗时。这里的耗时是客户端观测值；
`confirm_latency_ms` 包含交易所结算和网络返回，不能直接等同于纯撮合耗时。
补对冲单单独写入 `logging.hedges_csv`（默认 `logs/{symbol}/hedges.csv`），包括实际价格、
订单 ID、耗时、剩余敞口，以及在原始成交价已知时计算的单腿平仓 PnL。PnL 为空表示该仓位
不能完全归因到本进程观察到的成交。
可用 `python3 tools/analyze_trades.py --csv logs/{symbol}/trades.csv` 输出每条腿和双腿总耗时的
均值、P50、P95、P99，以及低于 350ms 目标的比例。

## 全市场只读筛选与网页

独立脚本扫描 RH 与 LIGHTER 共同活跃永续市场，不需要 `.env` 或私钥，不会下单。

```bash
python3 tools/market_screener.py collect --hours 72 --notional 300
# 另一个终端启动网页：http://127.0.0.1:8765
python3 tools/market_screener.py serve --input logs/screener --port 8765
```

默认先训练 24 小时，再冻结参数检验后续数据；网页可立即看实时深度报价，分析默认每小时更新。
影子往返不是实际成交收益，资金费和合约可比性仍需核验。
详见 [使用方法与模型限制](docs/market_screener.zh-CN.md)。

## 价差寿命测量（只读）

`tools/edge_lifetime.py` 用与机器人相同的配置、手续费、阈值和下单规模观察
同样的两个盘口，记录每一次可成交价差越过阈值的事件：持续多少毫秒、由哪个
交易所的更新开启和结束、在 50 ms 到 2 s 各时间点还剩多少超额价差。report
会给出订单落地时价差还在的比例，以及加持续性闸门后的效果。不读取密钥、不下单。
请在运行机器人的同一台服务器上运行——它测的正是那台机器的网络位置。

```bash
python3 tools/edge_lifetime.py collect --symbol SNDK --config sndk.yaml
python3 tools/edge_lifetime.py report --csv logs/sndk/edge_episodes.csv
```

## 挂单影子模拟（只读）

`tools/maker_shadow.py` 用来验证"在 RH 挂单"这条路：以对冲所中间价加溢价
中心（固定值，或 `--center-bps auto` 自动跟随）为基准，在 RH 模拟挂双边单，
按交易所的挂单/撤单延迟和每分钟操作上限维护报价，用 RH 公开成交判断是否
会被吃到，再按对冲所盘口模拟吃单对冲。可同时测试多个半价差。report 给出
每小时成交数、相对溢价中心的盈利、按盘口计价的总盈亏、成交后 1/5/30 秒
价格走势（为负说明被挑单）以及吃单方是谁。不下任何单，请在机器人所在的
服务器上运行。

```bash
python3 tools/maker_shadow.py collect --symbol SNDK --config sndk.yaml --half-spread-bps 2,3,4,6
python3 tools/maker_shadow.py report --dir logs/sndk
```

## 执行机制

- 两条腿**同时发出吃单**：Lighter 用带均价保护的市价单，在鉴权 websocket
  上异步确认成交；Hyperliquid 用 IOC 限价单同步结算（结果未知时轮询
  orderStatus 兜底）。
- **顺序执行**（`lead_venue: primary` 或 `hedge`）：先手腿以计划价格（加
  `lead_slippage_bps`）发 IOC 限价单，只有价差仍在时才会成交；另一条腿只按
  先手腿的实际成交量下单，先手腿没成交就什么都不发。适合某个交易所成交价
  经常远离盘口的情况。代价是后手腿多一次下单往返的延迟。
- 两条腿成交量不一致计为一次执行异常，累计到 `max_consecutive_errors`；
  成交笔数和预期边际只按两边都成交的数量统计。
- **持续性闸门**（`premium_persist_sec`）：信号先"武装"，持续存在才触发，
  过滤单 tick 的假信号。
- **库存阶梯**：仓位超过上限的 `floor_frac` 后，同方向加仓需要线性递增的
  额外溢价，满仓时最高加 `scale_bps`。
- **净敞口对冲**：两腿成交不对等时立即用 reduce-only 单（带滑点保护）
  削减敞口，并每 `reconcile_sec` 与链上仓位对账。
- **故障隔离**：被限频的交易所短暂暂停；交易所不可达（如例行维护）时暂停
  交易并每 `venue_probe_sec` 探测直至恢复；连续 `max_consecutive_errors`
  次执行异常则整体停机。
- **仅实盘**：没有模拟成交模式。`--record-only` 是唯一无风险的运行方式，
  其余都是真金白银。

## 目录结构

```
main.py                  入口（--record-only，默认即实盘）
entropy_arb/config.py    YAML + .env 配置契约与校验
entropy_arb/book.py      订单簿 + 含手续费的套利规模计算
entropy_arb/feeds.py     官方 HL ws + zkLighter ws 行情
entropy_arb/venue_hl.py  Hyperliquid dex 适配器（Entropy、tradexyz）
entropy_arb/venue_lighter.py  zkLighter 适配器（主网、Robinhood 链）
entropy_arb/engine.py    双交易所策略主循环
entropy_arb/dashboard.py Rich 终端仪表盘
entropy_arb/recorder.py  分钟级盘口数据采集
tools/analyze.py         minutes.csv -> 阈值建议
tools/edge_lifetime.py   价差能持续多久（只读）
tools/maker_shadow.py    RH 挂单影子模拟（只读）
tests/                   python3 -m pytest tests/
```

## 已知风险

- **中枢填错就是亏钱策略。** 溢价中枢会漂移，请定期重新测量并保持
  `config.yaml` 与市场同步。
- **USDG 基差**（`lighter-rh`）：对冲腿以 USDG 计价，持续溢价中有
  一部分是稳定币本身的基差；midline 吸收其水平，但 USDG 的*变动*是真实盈亏。
- **资金费**：两个交易所、两套独立的资金费率，持仓成本未建模——仓位上限
  请设小一些。
- **薄盘口**：Entropy 深度可能很小；`take_fraction` 与名义上限控制单笔规模，
  但部分成交后对冲腿的滑点是真实存在的。
- **交易时段**：股票类永续（如 SNDK）盘后各所预言机行为不同，建议加宽带宽
  或避开盘后。
- **单腿风险**：一条腿成交后另一条可能失败。机器人会自动对冲并对账，但
  仍需人工关注。

风险自负。本软件直接操作真实资金，本文档不构成任何投资建议。请从最小的
仓位上限开始。

## 开源协议

[MIT](LICENSE)
