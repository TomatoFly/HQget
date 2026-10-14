# -*- coding: utf-8 -*-
"""
plot_QuXian.py —— 利用离线数据，画出股票除权前后数据对比图

完全离线：
  - 不复权日线：直接读 VNPY 真实 database.db 的 dbbardata 表
  - 权息因子：TDXgbbqReader 离线硬解析 gbbq.zip（不联网）

交互模式（默认，优先 PySide6 原生控件）：
  - 窗口顶部为 Qt **原生**输入框/按钮/勾选框（输入零延迟，规避 matplotlib TextBox 大图卡顿），
    绘图区域仍为嵌入式 matplotlib 画布；无 Qt 时回退 matplotlib 控件版
  - 代码输入自动规范化：000985 / 000985.SSE / 000985sse / sh600000 等均可
  - 点击「绘制」按钮（或输入框回车）才绘制；**启动不自动绘制**
  - 上方：不复权 / 前复权 / 后复权 三条收盘价曲线
  - 下方：成交量副图，含 不复权 / 前复权 / 后复权 三种成交量（并排分组显示，sharex 与上方同步缩放）
  - 右侧勾选框可单独显示/隐藏「不复权 / 前复权 / 后复权」（价格曲线与对应成交量联动）
  - 工具栏支持缩放、平移、复位、保存（png）
  - 加载新股票时曲线自动适应窗体（自动缩放）

无显示环境 / --no-window：仅生成 PNG 到 --output（同样含成交量副图）
"""
import argparse
import importlib

import matplotlib

# 交互窗口：优先尝试可用的 GUI 后端；无显示环境回退 Agg（仅保存图片）。
# 注意：matplotlib.use 是延迟导入后端模块，必须真正 import 才能判断是否可用
# （例如本机无 tkinter 时 TkAgg 会因 ModuleNotFoundError 在创建图形时才暴露）。
_GUI_BACKENDS = ("QtAgg", "TkAgg", "Qt5Agg", "WXAgg", "GTK4Agg")
INTERACTIVE = False
for _be in _GUI_BACKENDS:
    try:
        importlib.import_module(f"matplotlib.backends.backend_{_be.lower()}")
        matplotlib.use(_be, force=True)
        INTERACTIVE = True
        break
    except Exception:
        continue
if not INTERACTIVE:
    matplotlib.use("Agg", force=True)

# 是否具备 Qt（PySide6/PyQt）交互能力：用于启用"原生控件 + matplotlib 画布"窗口，
# 原生 QLineEdit 输入零延迟，彻底规避 matplotlib TextBox 大图下的输入卡顿。
try:
    import matplotlib.backends.backend_qtagg as _qtagg_backend  # noqa: F401
    _HAS_QT = True
except Exception:
    _HAS_QT = False

import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import matplotlib.dates as mdates
from matplotlib.widgets import TextBox, CheckButtons, Button


class FastTextBox(TextBox):
    """修复输入框卡顿：原 TextBox 每次点击/输入字符都会 fig.canvas.draw() 重绘
    整张含数千根 K 线/成交量柱的大图，导致输入框长时间繁忙无法输入。
    改为只重绘输入框所在 axes（标签/文本/光标），不再重绘整图。"""

    def _rendercursor(self):
        fig = self.ax.get_figure(root=True)
        if fig._get_renderer() is None:
            fig.canvas.draw()
        text = self.text_disp.get_text()
        widthtext = text[:self.cursor_index]
        bb_text = self.text_disp.get_window_extent()
        self.text_disp.set_text(widthtext or ",")
        bb_widthtext = self.text_disp.get_window_extent()
        if bb_text.y0 == bb_text.y1:
            bb_text.y0 -= bb_widthtext.height / 2
            bb_text.y1 += bb_widthtext.height / 2
        elif not widthtext:
            bb_text.x1 = bb_text.x0
        else:
            bb_text.x1 = bb_text.x0 + bb_widthtext.width
        self.cursor.set(
            segments=[[(bb_text.x1, bb_text.y0), (bb_text.x1, bb_text.y1)]],
            visible=True,
        )
        self.text_disp.set_text(text)
        # 关键修复：只重绘输入框所在 axes，不再整图重绘
        self.ax.redraw_in_frame()


import numpy as np
import pandas as pd

# 让 matplotlib 支持中文（Windows 自带 SimHei / Microsoft YaHei 等）
for _cn in ("SimHei", "Microsoft YaHei", "Microsoft YaHei UI", "Heiti SC", "Arial Unicode MS"):
    if any(_cn.lower() in f.name.lower() for f in fm.fontManager.ttflist):
        plt.rcParams["font.sans-serif"] = [_cn]
        break
plt.rcParams["axes.unicode_minus"] = False  # 正常显示负号

from adjust_factor_builder import (
    load_xdxr_events,
    _read_unadjusted,
    build_adjusted_bars,
)
from vnpy_sqlite.sqlite_database import DbBarData


def _decimate(xn, vals, x0, x1, max_pts=1200):
    """按可视范围 [x0, x1] 抽稀：点数超过 max_pts 时等宽分 bin、每 bin 取 max。"""
    m = (xn >= x0) & (xn <= x1)
    x, y = xn[m], vals[m]
    if x.size == 0 or x.size <= max_pts:
        return x, y
    edges = np.linspace(x[0], x[-1], max_pts + 1)
    idx = np.clip(np.searchsorted(edges, x, side="right") - 1, 0, max_pts - 1)
    out_y = np.full(max_pts, -np.inf)
    np.maximum.at(out_y, idx, y)
    keep = out_y > -np.inf
    out_x = (edges[:-1] + edges[1:]) / 2.0
    return out_x[keep], np.maximum(out_y[keep], 0.0)


def get_exchange(symbol: str) -> str:
    """从库里查该 symbol 的 exchange（避免硬编码上/深交所）。"""
    rows = list(
        DbBarData.select(DbBarData.exchange)
        .where((DbBarData.symbol == symbol) & (DbBarData.interval == "d"))
        .distinct()
    )
    return rows[0].exchange if rows else "SSE"


# 交易所后缀/前缀别名 → VNPY Exchange.value
_EXCH_ALIAS = {
    "SSE": "SSE", "SH": "SSE", "SHA": "SSE", "SHSE": "SSE",
    "SZSE": "SZSE", "SZ": "SZSE", "SZA": "SZSE",
    "BJSE": "BJSE", "BJ": "BJSE", "BSE": "BJSE",
}


def normalize_symbol(text: str):
    """把用户输入规范化为 (6 位代码, exchange 或 None)。

    支持写法（大小写、点号/连写/前缀皆可）：
      '000985'、'000985.SSE'、'000985sse'、'000985.SH'、'000985SH'、
      'sh600000'、'SHSE600000'、'sz000001' 等。
    exchange 返回 VNPY 约定值（SSE/SZSE/BJSE）；无法判定时返回 None（交由库内查询）。
    """
    s = (text or "").strip().replace(" ", "")
    if not s:
        return "", None
    code, ex = s, None
    if "." in s:
        left, right = s.split(".", 1)
        code, ex = left, _EXCH_ALIAS.get(right.upper())
    else:
        up = s.upper()
        for pre in ("SHSE", "SZSE", "BJSE", "SSE", "SH", "SZ", "BJ"):
            if up.startswith(pre) and up[len(pre):].isdigit():
                code, ex = s[len(pre):], _EXCH_ALIAS.get(pre)
                break
        else:
            for suf in ("SHSE", "SZSE", "BJSE", "SSE", "SH", "SZ", "BJ"):
                if up.endswith(suf) and up[:-len(suf)].isdigit():
                    code, ex = s[:-len(suf)], _EXCH_ALIAS.get(suf)
                    break
    code = "".join(ch for ch in code if ch.isdigit())   # 仅保留数字
    if len(code) > 6:
        code = code[-6:]                                # 去掉多余前缀数字（如 1/6 市场位）
    return code, ex


def plot_adjusted(symbol: str, tdx_dir: str, output: str, verbose: bool = True) -> bool:
    """对单只股票绘制（上）收盘价对比 +（下）成交量对比 两张子图并保存。
    返回 True 表示成功生成；False 表示无日线数据（已跳过）。
    同一进程内多次调用只解析一次 gbbq.zip（adjust_factor_builder 模块级缓存）。"""
    symbol, ex_hint = normalize_symbol(symbol)
    exchange = ex_hint or get_exchange(symbol)
    if verbose:
        print(f"[{symbol}] exchange={exchange}")

    bars = _read_unadjusted(symbol, exchange)
    if verbose:
        print(f"  不复权日线条数: {len(bars)}")
    if not bars:
        if verbose:
            print("  未找到日线数据，跳过")
        return False

    # 离线权息事件（TDXgbbqReader 硬解析 gbbq.zip）
    events = load_xdxr_events(symbol, tdx_dir, source="offline", exchange=exchange)
    if verbose:
        print(f"  离线权息事件数: {len(events)}")
        for e in events:
            print(f"    {e['date']}  fh={e['fh']} sg={e['sg']} pg={e['pg']} pgj={e['pgj']}")

    fwd, bwd = build_adjusted_bars(bars, events)

    dates = [b["datetime"] for b in bars]
    raw_close = [b["close"] for b in bars]
    fwd_close = [b["close"] for b in fwd]
    bwd_close = [b["close"] for b in bwd]
    raw_vol = [b["volume"] for b in bars]
    fwd_vol = [b["volume"] for b in fwd]      # 前复权成交量 = 不复权量 / 前复权因子
    bwd_vol = [b["volume"] for b in bwd]      # 后复权成交量 = 不复权量 / 后复权因子

    # 上下两子图共享 x 轴（价格 + 成交量）
    fig, (ax, axv) = plt.subplots(
        2, 1, sharex=True, figsize=(15, 9), constrained_layout=True,
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.08},
    )

    ax.plot(dates, raw_close, label="不复权", lw=1.0, color="gray", alpha=0.9)
    ax.plot(dates, fwd_close, label="前复权", lw=1.2, color="#d62728")
    ax.plot(dates, bwd_close, label="后复权", lw=1.2, color="#1f77b4")

    # 性能关键：成交量用 vlines（每组 1 个 LineCollection）代替数千根独立 bar 矩形，
    # 缩放/平移整图重绘开销骤降，交互不再卡顿
    _voff = pd.Timedelta(days=0.28)   # 三种成交量并排分组，避免重叠
    axv.vlines([d - _voff for d in dates], 0, raw_vol, colors="gray",
               linewidths=2.5, alpha=0.85, label="不复权成交量")
    axv.vlines(dates, 0, fwd_vol, colors="#d62728",
               linewidths=2.5, alpha=0.75, label="前复权成交量")
    axv.vlines([d + _voff for d in dates], 0, bwd_vol, colors="#1f77b4",
               linewidths=2.5, alpha=0.75, label="后复权成交量")
    vmax = max((v for v in (raw_vol + fwd_vol + bwd_vol) if v), default=1)
    axv.set_ylim(0, vmax * 1.05)   # LineCollection 不参与 autoscale，量轴手动设置

    # 标注每次除权除息日（仅价格图）
    for e in events:
        try:
            d = pd.to_datetime(str(e["date"]), format="%Y%m%d")
        except Exception:
            continue
        ax.axvline(d, color="green", ls="--", lw=0.7, alpha=0.35)

    ax.set_title(
        f"{symbol} 不复权 / 前复权 / 后复权 收盘价对比（下：成交量）\n"
        f"（绿色虚线 = 除权除息日，共 {len(events)} 次；离线 gbbq 解析）"
    )
    ax.set_ylabel("收盘价")
    axv.set_ylabel("成交量")
    axv.set_xlabel("日期")
    ax.legend(loc="best")
    axv.legend(loc="best")
    ax.grid(True, alpha=0.3)
    axv.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    axv.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    fig.autofmt_xdate()
    fig.savefig(output, dpi=120)
    plt.close(fig)
    if verbose:
        print(f"\n图片已保存: {output}")
    return True


def launch_interactive(default_symbol: str, tdx_dir: str, output: str):
    """弹出交互窗口：输入代码换股、勾选显示曲线（价格+成交量联动）、工具栏缩放平移、
    自动适应窗体。上方收盘价 + 下方成交量（sharex 同步）。"""
    fig = plt.figure(figsize=(13, 9))

    # 上方：收盘价主图
    ax = fig.add_axes([0.08, 0.40, 0.74, 0.48])
    ax.set_ylabel("收盘价")
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    # 下方：成交量副图（与上方共享 x 轴，缩放/平移同步）
    axv = fig.add_axes([0.08, 0.10, 0.74, 0.26], sharex=ax)
    axv.set_xlabel("日期")
    axv.set_ylabel("成交量")
    axv.grid(True, alpha=0.3)
    axv.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    # 顶部：股票代码输入框
    ax_box = fig.add_axes([0.08, 0.93, 0.20, 0.045])
    text_box = FastTextBox(ax_box, "代码 ", initial=default_symbol)

    # 顶部：保存按钮
    ax_save = fig.add_axes([0.30, 0.93, 0.12, 0.045])
    save_btn = Button(ax_save, "保存图片")

    # 顶部：绘制按钮（点击才绘制；不点击不绘制，启动时为空图）
    ax_draw = fig.add_axes([0.44, 0.93, 0.12, 0.045])
    draw_btn = Button(ax_draw, "绘制")

    # 右侧：曲线显示勾选框（价格曲线与对应成交量联动）
    ax_chk = fig.add_axes([0.86, 0.62, 0.13, 0.20])
    labels = ["不复权", "前复权", "后复权"]
    chk = CheckButtons(ax_chk, labels, [True, True, True])

    # 三条价格曲线（数据稍后填充）
    (line_raw,) = ax.plot([], [], label="不复权", color="gray", lw=1.0, alpha=0.9)
    (line_fwd,) = ax.plot([], [], label="前复权", color="#d62728", lw=1.2)
    (line_bwd,) = ax.plot([], [], label="后复权", color="#1f77b4", lw=1.2)
    lines = [line_raw, line_fwd, line_bwd]

    vline_objs = []          # 除权除息日竖线（价格图）
    vol_objs = [None, None, None]   # 成交量：不复权(灰)/前复权(红)/后复权(蓝) LineCollection，与勾选框一一对应
    leg_ref = {"price": None, "vol": None}   # 图例引用，供勾选时同步显隐
    visible_states = [True, True, True]      # 与勾选框同步
    current_symbol = {"s": default_symbol}

    # ---- 成交量抽稀刷新（性能核心）----
    MAX_BARS = 1200      # 可视柱数上限；超出按等宽 bin 取 max 抽稀
    _VOFF = 0.28         # 三组柱并排偏移（天，与 date2num 同单位）
    vol_data = {}        # 全量成交量（x=date num + raw/fwd/bwd 三组量值）

    def refresh_vols(_evt=None):
        """按主图当前 x 可视范围抽稀并更新三组成交量柱（xlim_changed 回调）。
        全视图下 4825 根柱挤在约 900px 内本来就画不清，抽稀后每帧渲染从
        ~2.7s(bar)/~0.7s(vlines全量) 降到 ~0.2s，平移缩放流畅。"""
        if "x" not in vol_data:
            return
        x0, x1 = ax.get_xlim()
        xs = vol_data["x"]
        offs = (-_VOFF, 0.0, _VOFF)   # 不复权左移 / 前复权居中 / 后复权右移
        for i, key in enumerate(("raw", "fwd", "bwd")):
            if vol_objs[i] is None:
                continue
            x2, y2 = _decimate(xs, vol_data[key], x0, x1, MAX_BARS)
            o = offs[i]
            vol_objs[i].set_segments(
                [[(xx + o, 0.0), (xx + o, yy)] for xx, yy in zip(x2, y2)])

    def apply_visibility():
        """按 visible_states 同步价格曲线、成交量柱与图例条目的显隐。"""
        for i, ln in enumerate(lines):
            ln.set_visible(visible_states[i])
        for i, vo in enumerate(vol_objs):
            if vo is not None:
                vo.set_visible(visible_states[i])   # vlines→LineCollection，直接切显隐

        def _set_handle_visible(h, v):
            try:
                h.set_visible(v)
            except Exception:
                for _p in getattr(h, "patches", []) or []:
                    _p.set_visible(v)

        leg = leg_ref["price"]
        if leg is not None:
            for i, h in enumerate(leg.legend_handles):
                _set_handle_visible(h, visible_states[i])
            for i, t in enumerate(leg.get_texts()):
                t.set_visible(visible_states[i])
        legv = leg_ref["vol"]
        if legv is not None:
            # 成交量图例顺序 = [不复权, 前复权, 后复权] → 对应 labels 下标 0 / 1 / 2
            vmap = {0: 0, 1: 1, 2: 2}
            for li, hi in vmap.items():
                if hi < len(legv.legend_handles):
                    _set_handle_visible(legv.legend_handles[hi], visible_states[li])
                if hi < len(legv.get_texts()):
                    legv.get_texts()[hi].set_visible(visible_states[li])

    def load(symbol: str):
        """读取数据并刷新价格曲线与成交量柱；新股票时自动适应窗体。"""
        symbol, ex_hint = normalize_symbol(symbol)
        if not symbol:
            return
        current_symbol["s"] = symbol
        exchange = ex_hint or get_exchange(symbol)
        bars = _read_unadjusted(symbol, exchange)
        if not bars:
            ax.set_title(f"{symbol}（{exchange}）未找到日线数据，请确认代码或先导入日线")
            for ln in lines:
                ln.set_data([], [])
            for i, vo in enumerate(vol_objs):
                if vo is not None:
                    vo.remove()
                vol_objs[i] = None
            vol_data.clear()
            for o in vline_objs:
                o.remove()
            vline_objs.clear()
            fig.canvas.draw_idle()
            return

        events = load_xdxr_events(symbol, tdx_dir, source="offline", exchange=exchange)
        fwd, bwd = build_adjusted_bars(bars, events)

        dates = [b["datetime"] for b in bars]
        line_raw.set_data(dates, [b["close"] for b in bars])
        line_fwd.set_data(dates, [b["close"] for b in fwd])
        line_bwd.set_data(dates, [b["close"] for b in bwd])

        # 刷新成交量（先删旧的）：vlines 每组 1 个 LineCollection，
        # 实际 segments 由 refresh_vols() 按当前可视范围抽稀填充
        for vo in vol_objs:
            if vo is not None:
                try:
                    vo.remove()
                except Exception:
                    pass
        raw_vol = [b["volume"] for b in bars]
        fwd_vol = [b["volume"] for b in fwd]
        bwd_vol = [b["volume"] for b in bwd]
        vol_data["x"] = np.asarray(mdates.date2num(dates))
        vol_data["raw"] = np.asarray(raw_vol, dtype=float)
        vol_data["fwd"] = np.asarray(fwd_vol, dtype=float)
        vol_data["bwd"] = np.asarray(bwd_vol, dtype=float)
        for i, (c, lab) in enumerate((("gray", "不复权成交量"),
                                      ("#d62728", "前复权成交量"),
                                      ("#1f77b4", "后复权成交量"))):
            vol_objs[i] = axv.vlines([], 0, [], colors=c, linewidths=1.5, label=lab)
        refresh_vols()

        # 刷新除权除息竖线
        for o in vline_objs:
            o.remove()
        vline_objs.clear()
        for e in events:
            try:
                d = pd.to_datetime(str(e["date"]), format="%Y%m%d")
            except Exception:
                continue
            vline_objs.append(ax.axvline(d, color="green", ls="--", lw=0.7, alpha=0.35))

        ax.set_title(
            f"{symbol}（{exchange}）不复权 / 前复权 / 后复权 收盘价对比（下：成交量）\n"
            f"（绿色虚线 = 除权除息日，共 {len(events)} 次；离线 gbbq 解析）"
        )

        # 曲线自动适应窗体（仅在换股时重设视图，保留用户缩放/平移）
        ax.relim()
        ax.autoscale_view()
        # LineCollection 不参与 relim/autoscale，量轴手动自适应；x 由上方共享
        vmax = max((v for v in (raw_vol + fwd_vol + bwd_vol) if v), default=1)
        axv.set_ylim(0, vmax * 1.05)

        # 重建图例并按当前勾选同步显隐
        leg_ref["price"] = ax.legend(loc="best")
        leg_ref["vol"] = axv.legend(loc="best")
        apply_visibility()
        fig.canvas.draw_idle()

    def toggle(label):
        """勾选框：单独显示/隐藏对应曲线（价格+成交量联动，不改变当前缩放范围）。"""
        idx = labels.index(label)
        visible_states[idx] = not visible_states[idx]
        apply_visibility()
        fig.canvas.draw_idle()

    def do_save(_event):
        out = output.format(symbol=current_symbol["s"])
        fig.savefig(out, dpi=120)
        ax.set_title(ax.get_title() + f"\n已保存: {out}")
        fig.canvas.draw_idle()

    def _on_code(s):
        """输入框回车：仅记录代码，不绘制（绘制需点击「绘制」按钮）。"""
        current_symbol["s"] = (s or "").strip()

    def do_draw(_event):
        """点击「绘制」按钮：按输入框当前代码绘制对比图（不点击不绘制）。"""
        load(text_box.text)

    text_box.on_submit(_on_code)
    chk.on_clicked(toggle)
    save_btn.on_clicked(do_save)
    draw_btn.on_clicked(do_draw)
    ax.callbacks.connect("xlim_changed", refresh_vols)   # 平移/缩放时抽稀刷新成交量

    # 启动时不自动绘制：空图，提示用户点击「绘制」按钮
    ax.set_title("输入 6 位代码后点击「绘制」按钮（离线 gbbq 解析，不点击不绘制）")
    plt.show()


def _create_qt_window(default_symbol: str, tdx_dir: str, output: str):
    """构建并返回 PySide6 原生交互窗口的类（不创建 QApplication、不进入事件循环）。

    输入框/按钮/勾选框均为 Qt 控件（QLineEdit 输入零延迟），
    绘图区域为嵌入式 matplotlib 画布（不复权 / 前复权 / 后复权 收盘价 + 成交量）。
    点击「绘制」按钮才绘图（不点击不绘制）；工具栏支持缩放 / 平移 / 复位 / 保存。

    之所以改用 Qt：matplotlib 的 TextBox 在大 K 线图上逐字符重绘仍会卡顿，
    原生 QLineEdit 不触发任何重绘，彻底解决输入延迟。绘图逻辑（抽稀 / vlines）不变。"""
    from PySide6 import QtWidgets
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
    from matplotlib.figure import Figure

    class _Window(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("复权对比图（不复权 / 前复权 / 后复权）")
            self._tdx_dir = tdx_dir
            self._output = output
            self._cur = default_symbol
            self._vol_data = {}
            self._vol_objs = [None, None, None]
            self._vlines = []
            self._visible = [True, True, True]
            self._leg = {"price": None, "vol": None}
            self._MAX_BARS = 1200
            self._VOFF = 0.28

            # ---- 画布与坐标轴 ----
            self.fig = Figure(figsize=(13, 9))
            self.canvas = FigureCanvasQTAgg(self.fig)
            gs = self.fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.08,
                                       left=0.07, right=0.88, top=0.93, bottom=0.08)
            self.ax = self.fig.add_subplot(gs[0])
            self.axv = self.fig.add_subplot(gs[1], sharex=self.ax)
            self.ax.set_ylabel("收盘价"); self.ax.grid(True, alpha=0.3)
            self.axv.set_xlabel("日期"); self.axv.set_ylabel("成交量"); self.axv.grid(True, alpha=0.3)
            self.ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
            self.axv.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

            (self.line_raw,) = self.ax.plot([], [], label="不复权", color="gray", lw=1.0, alpha=0.9)
            (self.line_fwd,) = self.ax.plot([], [], label="前复权", color="#d62728", lw=1.2)
            (self.line_bwd,) = self.ax.plot([], [], label="后复权", color="#1f77b4", lw=1.2)
            self._lines = [self.line_raw, self.line_fwd, self.line_bwd]
            self.ax.callbacks.connect("xlim_changed", self._refresh_vols)

            # ---- 顶部控件（原生 Qt）----
            self.code_edit = QtWidgets.QLineEdit(default_symbol)
            self.code_edit.setMinimumWidth(170)
            self.code_edit.setPlaceholderText("代码，如 000985 / 000985.SSE / sh600000")
            btn_draw = QtWidgets.QPushButton("绘制")
            btn_save = QtWidgets.QPushButton("保存图片")
            btn_draw.clicked.connect(self._on_draw)
            btn_save.clicked.connect(self._on_save)
            self.code_edit.returnPressed.connect(self._on_draw)   # 回车亦可绘制

            top = QtWidgets.QHBoxLayout()
            top.addWidget(QtWidgets.QLabel("代码"))
            top.addWidget(self.code_edit)
            top.addWidget(btn_draw)
            top.addWidget(btn_save)
            top.addStretch(1)

            # ---- 右侧勾选框 ----
            self._checks = []
            side = QtWidgets.QVBoxLayout()
            side.addWidget(QtWidgets.QLabel("显示"))
            for i, lab in enumerate(("不复权", "前复权", "后复权")):
                cb = QtWidgets.QCheckBox(lab)
                cb.setChecked(True)
                cb.stateChanged.connect(lambda _s, idx=i: self._on_toggle(idx))
                self._checks.append(cb)
                side.addWidget(cb)
            side.addStretch(1)
            side_w = QtWidgets.QWidget(); side_w.setLayout(side); side_w.setFixedWidth(110)

            toolbar = NavigationToolbar2QT(self.canvas, self)

            body = QtWidgets.QHBoxLayout()
            body.addWidget(self.canvas, 1)
            body.addWidget(side_w)

            root = QtWidgets.QVBoxLayout()
            root.addLayout(top)
            root.addWidget(toolbar)
            root.addLayout(body)
            central = QtWidgets.QWidget(); central.setLayout(root)
            self.setCentralWidget(central)

            self.statusBar().showMessage(f"就绪（当前 {default_symbol}）"
                                         f"，输入代码后点击「绘制」")
            self.ax.set_title("输入代码后点击「绘制」按钮（离线 gbbq 解析，不点击不绘制）")
            self.resize(1250, 900)

        # ------- 成交量抽稀刷新（性能核心，逻辑同回退版）-------
        def _refresh_vols(self, _evt=None):
            if "x" not in self._vol_data:
                return
            x0, x1 = self.ax.get_xlim()
            xs = self._vol_data["x"]
            offs = (-self._VOFF, 0.0, self._VOFF)
            for i, key in enumerate(("raw", "fwd", "bwd")):
                if self._vol_objs[i] is None:
                    continue
                x2, y2 = _decimate(xs, self._vol_data[key], x0, x1, self._MAX_BARS)
                o = offs[i]
                self._vol_objs[i].set_segments(
                    [[(xx + o, 0.0), (xx + o, yy)] for xx, yy in zip(x2, y2)])

        def _apply_visibility(self):
            for i, ln in enumerate(self._lines):
                ln.set_visible(self._visible[i])
            for i, vo in enumerate(self._vol_objs):
                if vo is not None:
                    vo.set_visible(self._visible[i])
            for leg in (self._leg["price"], self._leg["vol"]):
                if leg is None:
                    continue
                for i, h in enumerate(leg.legend_handles):
                    try:
                        h.set_visible(self._visible[i])
                    except Exception:
                        for _p in getattr(h, "patches", []) or []:
                            _p.set_visible(self._visible[i])
                for i, t in enumerate(leg.get_texts()):
                    t.set_visible(self._visible[i])

        def _on_toggle(self, idx):
            self._visible[idx] = not self._visible[idx]
            self._apply_visibility()
            self.canvas.draw_idle()

        def _on_draw(self):
            """按输入框当前代码绘制对比图（代码先规范化，支持 .SSE/sse/SH 前缀后缀）。"""
            sym, ex = normalize_symbol(self.code_edit.text())
            if not sym:
                self.ax.set_title("请输入 6 位股票代码（如 000985 / 000985.SSE）")
                self.canvas.draw_idle()
                return
            if ex is None:
                ex = get_exchange(sym)
            self._cur = sym
            bars = _read_unadjusted(sym, ex)
            if not bars:
                for ln in self._lines:
                    ln.set_data([], [])
                for i, vo in enumerate(self._vol_objs):
                    if vo is not None:
                        vo.remove()
                    self._vol_objs[i] = None
                self._vol_data.clear()
                for o in self._vlines:
                    o.remove()
                self._vlines.clear()
                self._leg = {"price": None, "vol": None}
                self.ax.set_title(f"{sym}（{ex}）未找到日线数据，请确认代码或先导入日线")
                self.statusBar().showMessage(f"未找到 {sym}.{ex} 的日线数据")
                self.canvas.draw_idle()
                return

            events = load_xdxr_events(sym, self._tdx_dir, source="offline", exchange=ex)
            fwd, bwd = build_adjusted_bars(bars, events)

            dates = [b["datetime"] for b in bars]
            self.line_raw.set_data(dates, [b["close"] for b in bars])
            self.line_fwd.set_data(dates, [b["close"] for b in fwd])
            self.line_bwd.set_data(dates, [b["close"] for b in bwd])

            for vo in self._vol_objs:
                if vo is not None:
                    try:
                        vo.remove()
                    except Exception:
                        pass
            raw_vol = [b["volume"] for b in bars]
            fwd_vol = [b["volume"] for b in fwd]
            bwd_vol = [b["volume"] for b in bwd]
            self._vol_data["x"] = np.asarray(mdates.date2num(dates))
            self._vol_data["raw"] = np.asarray(raw_vol, dtype=float)
            self._vol_data["fwd"] = np.asarray(fwd_vol, dtype=float)
            self._vol_data["bwd"] = np.asarray(bwd_vol, dtype=float)
            for i, (c, lab) in enumerate((("gray", "不复权成交量"),
                                          ("#d62728", "前复权成交量"),
                                          ("#1f77b4", "后复权成交量"))):
                self._vol_objs[i] = self.axv.vlines([], 0, [], colors=c, linewidths=1.5, label=lab)

            for o in self._vlines:
                o.remove()
            self._vlines.clear()
            for e in events:
                try:
                    d = pd.to_datetime(str(e["date"]), format="%Y%m%d")
                except Exception:
                    continue
                self._vlines.append(self.ax.axvline(d, color="green", ls="--", lw=0.7, alpha=0.35))

            self.ax.set_title(
                f"{sym}（{ex}）不复权 / 前复权 / 后复权 收盘价对比（下：成交量）\n"
                f"（绿色虚线 = 除权除息日，共 {len(events)} 次；离线 gbbq 解析）")

            self.ax.relim()
            self.ax.autoscale_view()
            vmax = max((v for v in (raw_vol + fwd_vol + bwd_vol) if v), default=1)
            self.axv.set_ylim(0, vmax * 1.05)

            self._leg["price"] = self.ax.legend(loc="best")
            self._leg["vol"] = self.axv.legend(loc="best")
            self._apply_visibility()
            self._refresh_vols()
            self.canvas.draw_idle()
            self.statusBar().showMessage(
                f"已绘制 {sym}.{ex}（{len(bars)} 根日线，{len(events)} 次除权除息）")

        def _on_save(self):
            out = self._output.format(symbol=self._cur)
            self.fig.savefig(out, dpi=120)
            self.statusBar().showMessage(f"已保存: {out}")

    return _Window


def launch_qt(default_symbol: str, tdx_dir: str, output: str):
    """创建并显示 Qt 交互窗口（阻塞至关闭）。"""
    from PySide6 import QtWidgets
    Window = _create_qt_window(default_symbol, tdx_dir, output)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = Window()
    win.show()
    app.exec()


def main():
    ap = argparse.ArgumentParser(description="离线绘制除权前后对比图（交互窗口 / 保存 PNG）")
    ap.add_argument("--symbol", default="002061", help="6 位代码，默认 002061")
    ap.add_argument("--tdx-dir", default=r"D:\HQdata\tdxData", help="含 gbbq.zip 的 TDX 目录")
    ap.add_argument("--output", default=r"d:\HQdata\test1_{symbol}_adj.png",
                    help="输出图片路径，{symbol} 会被替换")
    ap.add_argument("--no-window", action="store_true",
                    help="不弹窗，仅用 --output 生成 PNG（无显示/服务器环境）")
    args = ap.parse_args()

    out = args.output.format(symbol=args.symbol)

    if INTERACTIVE and not args.no_window:
        if _HAS_QT:
            launch_qt(args.symbol, args.tdx_dir, args.output)   # PySide6 原生控件（输入零延迟）
        else:
            launch_interactive(args.symbol, args.tdx_dir, args.output)
    else:
        # 无显示环境 / 显式 --no-window：强制 Agg 仅保存 PNG
        matplotlib.use("Agg", force=True)
        ok = plot_adjusted(args.symbol, args.tdx_dir, out)
        if not ok:
            print("未找到日线数据，请先导入日线到 database.db")


if __name__ == "__main__":
    main()
