# HQdata - VNPY 通达信数据导入工具

基于开源项目 [veighna (vnpy) V4.3.0](https://github.com/vnpy/vnpy) 的通达信本地数据导入工具，解决免费日K线行情数据源问题。

## 功能特性

### 已实现 ✅
- **TDX `.day` 文件解析**：本地解析通达信 `.day` 格式日线数据文件
- **在线 API 数据获取**：通过 `pytdx` 连接通达信服务器在线获取 K 线
- **VNPY BarData 转换**：将通达信数据转为 vnpy 标准 `BarData` 格式
- **数据库全量/增量写入**：
  - 覆盖模式：直接写（冲突时数据库 `REPLACE`）
  - 增量模式：自动对比 `db.get_bar_overviews()`，只写入范围外新数据
- **数据库更新范围计算**：`getOverViewsUpdataInfo()` 给出每只股票下次需拉取的起止日期
- **前/后复权数据生成**：离线 gbbq 权息硬解析 → 独立表 `dbbardata_fwd` / `dbbardata_bwd`（原 `dbbardata` 永不被改动）
- **复权对比交互可视化**：`test1.py` 输入代码即时绘制 不复权/前复权/后复权 收盘价 + 成交量对比
- **基本面数据入库**：`runJBM20260727.py` 解析 `jbmDataV1` 日快照 CSV（含 zip/rar 内 CSV）→ `fundamental.db`

### 待开发 🚧
- 除权除息数据**明文**导入（✅ 已完成：`TDXgbbqReader` 用 TEA/Feistel 算法硬解密 gbbq，字段已解析，并经 mootdx 在线 xdxr 逐字段验证）
- 分钟线数据支持
- 财报告警数据

## 文件说明

| 文件 | 用途 |
|------|------|
| [TDXdataRead.py](file:///d:/HQdata/TDXdataRead.py) | TDX 数据读取：本地文件解析 + 在线API获取 |
| [VNPYdataWriter.py](file:///d:/HQdata/VNPYdataWriter.py) | `class VNPYdataWriter`：数据转换 + 数据库写入 |
| [runTDXQuanLiang.py](file:///d:/HQdata/runTDXQuanLiang.py) | 主程序：遍历所有 `.day` 文件全量/增量入库 |
| [runTDXZhengLiang.py](file:///d:/HQdata/runTDXZhengLiang.py) | 在线接口测试 + `getOverViewsUpdataInfo` 示例 |
| [TDXgbbqReader.py](file:///d:/HQdata/TDXgbbqReader.py) | 股本变更/权息(gbbq)文件**硬解析器**：TEA/Feistel 离线解密 + 字段解析（date/category/分红/送转/配股/配股价） |
| [runTDXchuquan.py](file:///d:/HQdata/runTDXchuquan.py) | 复权数据入口：确保复权表存在 + 全量/单只生成前/后复权数据 |
| [adjust_factor_builder.py](file:///d:/HQdata/adjust_factor_builder.py) | 复权核心：读日线 + 离线权息 → 计算前/后复权 → 写 `dbbardata_fwd/_bwd` |
| [vnpy_adj_database.py](file:///d:/HQdata/vnpy_adj_database.py) | `AdjustedSqliteDatabase`：回测按 `mode` 读取 fwd/bwd/none |
| [test1.py](file:///d:/HQdata/test1.py) | 交互式绘图：输入代码即时对比 不复权/前复权/后复权 |
| [plot_QuXian.py](file:///d:/HQdata/plot_QuXian.py) | 同款交互对比图：交互控件改用 **PySide6 原生**（输入零延迟），绘图仍用 matplotlib；代码支持 `000985.SSE`/`000985sse`/`sh600000` 等写法；「绘制」按钮/回车触发 |
| [runIndexSupplement.py](file:///d:/HQdata/runIndexSupplement.py) | 东方财富指数日线补全（直写 `dbbardata`；`--auto` 审计全部停更指数段标的） |
| [runMainGetDatas.py](file:///d:/HQdata/runMainGetDatas.py) | **每日更新主流程**：检查/下载 hsjday+gbbq → 解压 → 本地入库 → 指数补全 → 复权，一键串联 |
| [runJBM20260727.py](file:///d:/HQdata/runJBM20260727.py) | 基本面数据入库：解析 `jbmDataV1` CSV（含 zip/rar）→ `fundamental.db` 两表 |

## 股本变更/权息 (gbbq) 解析

`TDXgbbqReader.py` 可解析通达信权息文件 `tdxData/gbbq.zip`：

```python
from TDXgbbqReader import TDXgbbqReader

reader = TDXgbbqReader()          # 默认读取 D:\HQdata\tdxData\gbbq.zip
reader.parse()
print(reader.summary())
records = reader.get_records_by_stock("000001")  # 按代码取权息记录
```

- 文件头 4 字节为记录总数；每条记录 29 字节（8 字节股票 ID + 21 字节数据）。
- 股票代码顺序由同包 `gbbq.map` 决定（按 gbk 读取），但解析器**以解密出的内嵌代码为权威身份**（`TDXgbbqReader` 已验证：本 zip 的 `gbbq.map` 偶发把真实 601113 的数据块标成 601111，解密代码可纠正）。
- **解密已实装**：采用社区逆向的 TEA/Feistel 算法（`_gbbq_decrypt_record`，密钥块 `_GBBQ_KEY_HEX`），每条记录前 24 字节（3×8）解密、后 5 字节明文。解密后字段：
  - `market`(1B,0=深/1=沪)、`code`(7B ASCII)、`date`(uint32,YYYYMMDD)、`category`(1B,1=除权除息/2=送配股上市/5=股本变化…)
  - 对 `category==1`：`fen_hong`=分红、`pei_price`=配股价、`song_gu`=送转股、`pei_gu`=配股（均为每 10 股口径）
- **已用 mootdx 在线 xdxr 验证**：`000001`(平安银行) 28 个共有 t=1 事件中 27 个字段完全一致；`601111`(中国国航) 12 个 t=1 事件分红全部一致。仅约 1% 记录因公开密钥块个别字节瑕疵呈乱码（属已知边缘情况）。

## 使用流程

> **一键每日更新**：`python runMainGetDatas.py`（详见下文「每日数据更新主流程」，自动完成下面的准备/解压/入库/复权全流程）。

### 一、准备数据

1. **手动下载数据包**：
   ```
   https://data.tdx.com.cn/vipdoc/hsjday.zip
   ```
2. 解压到本地数据目录（项目约定放在 `D:\HQdata\tdxData\` 下，例如 `tdxData/hsjday20260708/sh/lday/sh*.day`、`tdxData/hsjday20260708/sz/lday/sz*.day`），
   或放到 `D:\HQdata\hsjday\sh\lday\`、`D:\HQdata\hsjday\sz\lday\`（`TDXdataRead.get_file_path` 默认路径）：
   ```
   D:\HQdata\hsjday\sh\lday\sh*.day
   D:\HQdata\hsjday\sz\lday\sz*.day
   ```
   > `runTDXQuanLiang.py` 会递归 `D:\HQdata` 下所有 `.day` 文件，因此放在 `tdxData/` 子目录亦可。
3. 权息数据（可选）：将 `gbbq.zip` 放到 `D:\HQdata\tdxData\`，用 `TDXgbbqReader` 解析。

### 二、配置数据库

在 [VNPYdataWriter.py](file:///d:/HQdata/VNPYdataWriter.py#L12-L20) 中调整：
```python
# SQLite （默认）
SETTINGS["database.driver"] = "sqlite"
SETTINGS["database.database"] = "database.db"

# MySQL
# SETTINGS["database.driver"] = "mysql"
# SETTINGS["database.host"] = "localhost"
# SETTINGS["database.port"] = 3306
```

### 三、执行导入

```bash
python runTDXQuanLiang.py
```

逻辑：
```python
writer = VNPYdataWriter.VNPYdataWriter()
for file in *.day:
    bars = writer.convertToVNPYbarData(stock_data)
    writer.write_tdx_day(bars, overwrite=False)  # 增量判断
```

### 四、在线增量更新示例

```python
import VNPYdataWriter as VNPYdataWriterModule
import TDXdataRead as TdxRead

vnpWrite = VNPYdataWriterModule.VNPYdataWriter()

# 1) 拿到所有股票的需更新日期范围
for ov in vnpWrite.getOverViewsUpdataInfo():
    # ov.start: 数据库 end+1
    # ov.end:   今天

    # 2) 通达信在线API拉取
    bars_data = TdxRead.read_tdx_day_online(
        f"{symbol}",
        f"{ov.start.strftime('%Y-%m-%d')}",
        f"{ov.end.strftime('%Y-%m-%d')}"
    )

    # 3) 增量入库
    bars = vnpWrite.convertToVNPYbarData(bars_data)
    vnpWrite.write_tdx_day(bars, overwrite=False)
```

## 依赖安装

```bash
pip install pytdx       # 在线API
pip install vnpy        # veighna 量化框架 4.3.0+
```

## 注意事项

- 免费通达信服务器单批最多返回 800 条 K 线
- 若在线拉取时间跨度大于 800 个交易日需多批次翻页
- `RunTDXZhengLiang.py` 可直接运行验证环境

---

# 复权数据方案（前复权 / 后复权 / 不复权）

> 选型结论：**方案 A（独立复权表 + 自定义 Database 子类）**。
> 决策项：**成交量随复权调整**；**权息因子来源 = TDXgbbqReader 离线硬解析 gbbq（已与 mootdx 在线 xdxr 验证一致）**。

## 核心约束（决定方案边界）

- VNPY 的 K 线表 `dbbardata` 唯一键为 `(symbol, exchange, interval, datetime)`，`save_bar_data` 使用 `on_conflict_replace()`。
  **若用相同主键写回复权数据会直接覆盖不复权原数据**，破坏回测。→ 复权数据绝不复用该主键组合。
- 回测读数据路径：`BacktestingEngine.load_data()` → `database.load_bar_data(symbol, exchange, interval, start, end)`。
- 当前库（`.vntrader/database.db`）约 2928 万行，几乎全为日线（`interval='d'`，约 2922 万行；`1m` 仅 5.8 万行）。
  **复权仅对日线有意义**，分钟及其他周期一律原样返回不复权。

## 架构（方案 A）

```
dbbardata          ← 不复权（VNPY 原表，唯一真相源，只读、永不改）
dbbardata_fwd      ← 前复权（独立表、独立主键，结构同 dbbardata）
dbbardata_bwd      ← 后复权（独立表、独立主键）

AdjustedSqliteDatabase(SqliteDatabase)   # 重写 load_bar_data
  ├─ mode='none'    → 调父类原逻辑（与 VNPY 行为完全一致，原回测结果不变）
  ├─ mode='forward' → 读 dbbardata_fwd
  └─ mode='backward'→ 读 dbbardata_bwd
```

- 不复权原表 `dbbardata` 始终作为唯一真相源；复权数据完全由「它 + 权息因子」派生，出错可删表重算，**原回测数据零风险**。
- 默认 `mode='none'` 时，`load_bar_data` 行为 = VNPY 原生，保证既有回测结果 byte 级不变。

## 复权规则

对每個除权除息日 `ex`，单事件价格因子（基于除权前收盘价 `P_before`）：
```
f_e = (P_before − 分红 + 配股价×配股比例) / (P_before × (1 + 送股比例 + 转增比例 + 配股比例))
```
累计因子：
- `q = Π_{ex>bar_date} f_e`（bar 日**之后**所有事件因子连乘）
- `h = Π_{ex≤bar_date} (1/f_e)`（bar 日**及之前**所有事件因子倒数连乘）

> ⚠️ **本项目采用「与标准中文口径相反」的约定（用户定义，2026-07 修正）**：
- **前复权 `fwd`（锚定首日）**：`adj_price = raw × h`；`adj_volume = raw / h`。**首日价格 = 不复权实际价**，其后按累计因子缩放。
- **后复权 `bwd`（锚定最新价）**：`adj_price = raw × q`；`adj_volume = raw / q`。**最新日价格 = 不复权实际价**，其前按累计因子缩放。
- **成交量随复权调整**（已确认）：价格 × 因子、成交量 ÷ 同一因子，使成交额近似守恒。
- 成交额（`turnover`）沿用不复权原值。
- open/high/low 与 close 同乘价格因子。

> 注：早期版本曾把 `q`/`h` 写反（`fwd` 用 `q`、`bwd` 用 `h`），导致后复权几乎等于不复权价；已交换赋值修复，并以 `dbbardata_fwd/bwd` 全量重建验证。

## 权息因子来源（离线硬解析已可用 ✅ + 在线兜底）

**离线（推荐，已验证正确）**：`TDXgbbqReader.py` 用社区逆向的 TEA/Feistel 算法硬解密 `tdxData/gbbq.zip`，解析出 `date/category/分红/送转/配股/配股价`，并经 **mootdx 在线 xdxr 逐字段验证一致**（`000001` 27/28、`601111` 12/12 分红完全吻合）。

> ⚠️ 离线解析两点已知边界：
> - 约 **1% 记录**因公开密钥块个别字节瑕疵呈乱码（如 `000001` 的 20130620、前 200 只中 23 只代码全乱），属边缘情况；
> - 个别 zip 的 `gbbq.map` 标签与二进制内嵌代码错位（本 zip 把真实 `601113` 标成 `601111`），解析器已**以解密出的代码为权威身份**自动纠正。

**在线（兜底/交叉校验）**：`mootdx` 在线 `Quotes.factory(market='std').xdxr(symbol=...)`，返回 `year/month/day/category/fenhong/songzhuangu/peigu/peigujia`。仅取 `category==1`（除权除息）；`fenhong/songzhuangu/peigu` 为每 10 股口径。

**跨市场同码防护**：gbbq 只按 6 位代码存记录、不带交易所，沪深同码会互串（如 `000985.SSE` 是**指数**（中证全指），会错拿到深市个股大庆华科 `000985.SZSE` 的分红事件被错误复权；`000001.SSE` 上证指数同理撞车平安银行）。防护规则：
- `is_index_symbol(symbol, exchange)`：指数代码（SSE `000xxx/88xxxx/999xxx`、SZSE `399xxx`、BSE `899xxx`）直接返回空事件（指数无除权除息）；
- `load_xdxr_events(..., exchange=)` 按 gbbq 记录的 market（sh/sz/bj）过滤，且「该代码只存在于其他市场」时**不回退在线**（在线 xdxr 同样只按代码查询，会拿到错市场权息）；
- 落库（`runTDXchuquan.py` → `sync_adjusted`）与实时绘图（`plot_QuXian.py`）两条路径均已接入。

除权单事件因子（通达信标准，每 10 股口径）：
```
f_e = (P_before*10 − fenhong + peigu×peigujia) / (P_before × (10 + songzhuangu + peigu))
```

## 文件

| 文件 | 用途 |
|------|------|
| [vnpy_adj_database.py](file:///d:/HQdata/vnpy_adj_database.py) | `AdjustedSqliteDatabase` 子类 + 复权模式开关 + `use_adjusted_database()` 注册函数 |
| [adjust_factor_builder.py](file:///d:/HQdata/adjust_factor_builder.py) | 从 `dbbardata` 读日线 + **默认离线 `TDXgbbqReader` 取权息**（在线 xdxr 兜底，`--source` 可切换）→ 计算前/后复权 → 写入 `dbbardata_fwd/_bwd` |
| [runTDXchuquan.py](file:///d:/HQdata/runTDXchuquan.py) | 方案 A 入口：第 1 步确保 `dbbardata_fwd/_bwd` 存在，第 2 步按 `dbbardata` + gbbq 计算并 upsert 复权数据（`--symbol` 单只 / 缺省全市场） |
| [selftest_adj.py](file:///d:/HQdata/selftest_adj.py) | 临时库自测：验证数学正确 + `none` 模式与原数据一致（不碰真实库） |

## 使用步骤

```bash
pip install mootdx          # 权息因子：离线用 TDXgbbqReader 硬解析；mootdx 在线 xdxr 作交叉校验
```

1. **生成复权数据**（写入真实库的两个独立表，原 `dbbardata` 不变）：

   ```bash
   # 推荐入口：先对单只验证，再全市场（自动确保复权表存在）
   python runTDXchuquan.py --symbol 601111        # 单只（秒级）
   python runTDXchuquan.py                         # 全市场（约 1.2 万合约，耗时较长，建议后台运行）

   # 或直接用底层 builder
   # 先对单只验证（已验证 601111：raw/fwd/bwd 各 4761 条，前复权锚定首日、后复权锚定最新日）
   python adjust_factor_builder.py --symbol 601111
   # 全市场（约 1.2 万只；默认离线 gbbq 硬解析，gbbq.zip 仅解析一次，无需联网）
   python adjust_factor_builder.py --build
   # 如需强制走在线 xdxr（交叉校验用）
   python adjust_factor_builder.py --symbol 601111 --source online
   ```

   > 全市场重建耗时较长（约 4 小时）。命令行超时可用后台方式：
   > `Start-Process python -ArgumentList 'runTDXchuquan.py' -RedirectStandardOutput _build.log`，
   > 再轮询 `Get-Content -Encoding utf8 _build.log -Tail 15` 看进度，日志末尾出现 `全部完成。` 即结束。

2. **回测选择复权模式**（在回测脚本最前面、任何 `get_database()` 之前）：
   ```python
   from vnpy_adj_database import use_adjusted_database
   use_adjusted_database(mode="forward")   # 'none' / 'forward' / 'backward'
   # 之后照常 import vnpy / 启动 CTA Backtester
   ```
   `mode='none'` 时与原 VNPY 回测结果完全一致。

## 关键保证

- 原 `dbbardata` 永不被复权逻辑修改/覆盖；原回测数据可完整复现。
- 复权表独立、可随时删除重建（派生自不复权 + 权息因子）。
- 自测：`python selftest_adj.py`（在临时 SQLite 上验证，不影响真实数据库）。

---

# 复权对比交互可视化（test1.py）

离线绘制单只股票的 **不复权 / 前复权 / 后复权** 对比图（上方收盘价曲线 + 下方成交量柱，右侧勾选框联动显隐，绿色虚线标注除权除息日）。

```bash
python test1.py                 # 弹窗交互：顶部输入 6 位代码回车即换股（默认 002061）
python test1.py --symbol 601111 # 指定初始代码
python test1.py --no-window     # 无显示环境：仅生成 PNG 到 --output
```

- 依赖：读取真实库 `dbbardata` 日线 + 离线 `gbbq.zip` 权息（`adjust_factor_builder`）；
- 输入框用 `FastTextBox`（重写 `matplotlib.widgets.TextBox._rendercursor`，只重绘输入框 axes 而非整图，避免大图下输入卡顿）；
- ⚠️ 交互窗口须用终端 `python test1.py` 直接运行；**勿用 debugpy 调试器**，否则 Qt 事件循环冲突会导致窗口「未响应」。

## plot_QuXian.py（PySide6 原生交互 + matplotlib 绘图）

`plot_QuXian.py` 是 `test1.py` 的同源脚本，交互层用 **PySide6 原生控件**、绘图仍用 **matplotlib**：

- **交互用 Qt 原生控件（解决输入卡顿）**：顶部输入框/按钮、右侧勾选框均为 Qt 控件，绘图区为嵌入式 `FigureCanvasQTAgg` 画布。**原生 `QLineEdit` 输入不触发任何重绘**，彻底规避 matplotlib `TextBox` 在大 K 线图下逐字符重绘导致的「输入十秒才响应」。无 Qt 环境时自动回退 matplotlib 控件版。
- **代码输入自动规范化**：支持 `000985`、`000985.SSE`、`000985sse`、`000985.SH`、`000985SZ`、`sh600000` 等写法（后缀/前缀/大小写均可，未知后缀时回退库内查询），解决「输入 `000985.SSE` 提示未找到日线数据」。
- **「绘制」按钮 / 回车触发**：启动不自动绘制（空图），点击「绘制」按钮或输入框回车才按当前代码绘图；保存按钮按当前代码导出 PNG。
- **性能优化（与 test1.py 一致）**：成交量副图用 `vlines`（`LineCollection`）+ 按可视范围**抽稀**（上限 1200 根，`xlim_changed` 回调刷新），平移/缩放流畅。

```bash
pip install PySide6                                  # 原生交互需要（vnpy 环境通常已自带）
python plot_QuXian.py                                # 弹窗：输入代码后点「绘制」（默认 002061）
python plot_QuXian.py --symbol 601111                # 指定初始代码
python plot_QuXian.py --no-window --symbol 601111    # 仅生成 PNG（无显示环境）
```

> 注：`--no-window`（保存 PNG）路径仍按 `--symbol` 直接出图，不受「不点击不绘制」限制；该路径同样支持上述代码写法。

---

# 基本面数据入库（runJBM20260727.py）

将 `D:\HQdata\jbmDataV1\` 下的**基本面日快照 CSV**（文件名=日期）解析写入
`C:\Users\gdl_01\.vntrader\fundamental.db` 的两张表。

```bash
python runJBM20260727.py
```

- **数据源**：递归扫描 `jbmDataV1` 下 `*.csv`，以及 `.zip` / `.rar` 压缩包内的 `*.csv`
  （`.rar` 用 `7z.exe` 解压，支持 Rar5；若未装 7z 则回退 `rarfile`+unrar，均无则跳过并提示）。
- **CSV 列**（30 列）：`日期,代码(.SH/.SZ/.BJ),名称,行业,城市,上市日期,总股本(亿),流通股本(亿),总资产(亿),流动资产(亿),固定资产(亿),每股净资产,每股收益,市盈率(动),市净率,未分配利润,每股未分配利润,公积金,每股公积金,毛利率%,净利润率%,收入同比%,利润同比%,股东人数,内盘(手),外盘(手),振幅,强弱度%,活跃度%,攻击波%`
- **目标表与字段**（英文字段名）：
  - `jbmbaseA202607`：`symbol, exchange, datetime, name, industry, city, list_date, total_shares, float_shares, total_assets, current_assets, fixed_assets, bvps, eps, pb, undistributed_profit, udpps, reserve, rpps, gross_margin, net_margin, revenue_yoy, profit_yoy, shareholders`
  - `jbmbaseB202607`：`symbol, exchange, datetime, pe_ttm, inner_volume, outer_volume, amplitude, strength, activity, attack_wave`
- **结构约定**（与 VNPY `database.db` 一致）：`symbol` / `exchange` 为 `TEXT`（`exchange` 取 VNPY `Exchange.value`：`.SH→SSE`、`.SZ→SZSE`、`.BJ→BJSE`）；`datetime` 为 `TEXT`，存 `'YYYY-MM-DD HH:MM:SS'`；主键 `(symbol, exchange, datetime)`。
- **类型推断**：首次建表按解析数据推断数值列类型（含小数点→`REAL`，否则→`INTEGER`）；表已存在则用 `PRAGMA table_info` 读取。
- **写入规则**：
  - 同只股票**相邻时间点仅写入「有变化」的记录**（A、B 表均启用变化过滤）；
  - 重复 `(symbol, exchange, datetime)` 默认**不覆盖**（`insert_record(..., overwrite=False)`，并额外捕获 UNIQUE 兜底），终端打印重复信息（限流前 20 条）；
  - 启动时把已有主键载入内存集合去重，避免逐行 `SELECT` 造成 SQLite 锁竞争。

> 依赖：标准库 + 可选 `7z`（解压 rar）/ `rarfile`（回退）。CSV 若带多重 BOM 会被自动剔除。

---

# 每日数据更新主流程（runMainGetDatas.py）

一条命令完成「下载 → 解压 → 入库 → 补全 → 复权」：

```bash
python runMainGetDatas.py                            # 全流程
python runMainGetDatas.py --supplement-args "--auto" # 指数补全改用自动审计模式
```

**流程与规则**：

1. **检查/下载 `tdxData/hsjday.zip`（约 550MB）与 `tdxData/gbbq.zip`（约 6MB）**：
   - 最新性 = 本地 mtime ≥ 远端 `Last-Modified`（HEAD 探测，多镜像依次尝试）；远端不可达时退化为「文件日期=今天」→ 未更新就不重复下载；
   - 非最新/缺失/损坏 → **先下载到 `.part` 并校验，成功后**才把旧文件改名 `stem+YYYYMMDD.zip` 移入 `tdxData/bak/`（同名追加 `_HHMMSS`），失败则旧文件原地保留、**弹窗提示并终止**；
   - 校验：zip CRC 完整 + hsjday 含 `.day`（顺带打印包内 sh000001 最新数据日）+ gbbq 含 `gbbq`/`gbbq.map`。
2. **解压** hsjday.zip 到 `tdxData/`（内部结构 `sh/sz/bj/lday/*.day`，原地覆盖）。
3. **本地入库**：运行 `runTDXQuanLiang.py`（不加 `--online`，纯本地 .day 解析）。
4. **指数补全**：运行 `runIndexSupplement.py`（缺省补 `000985.SSE`；`--supplement-args` 可透传 `--auto` 等）。
5. **除权处理**：运行 `runTDXchuquan.py`（增量生成/更新 `dbbardata_fwd`/`dbbardata_bwd`）。

任一步失败：弹窗提示、退出码非 0、后续步骤不执行（已完成步骤的结果保留）。

> 下载源（已实测，按顺序回退）：hsjday → `data.tdx.com.cn/vipdoc/hsjday.zip`；gbbq → `www.tdx.com.cn/products/data/data/dbf/gbbq.zip`、`cnc.tdx.com.cn/dbf/gbbq.zip`、两个 IP 镜像兜底。

---

HQdata veighna vnpy data importer | 2026
