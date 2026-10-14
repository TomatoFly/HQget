# -*- coding: utf-8 -*-
"""
runIndexSupplement.py —— 指数日线补全工具（东方财富历史K线，直写 sqlite）

背景
----
TDX 官方 hsjday 全量包里有一批指数文件已停止更新（2026-10-12 实测，三个不同日期的
全量包一致）：
  - 中证全指 sh000985.day 只到 2016-06-13；SSE 000xxx 段共 144 个中证系列指数停更
    （大量与 000985 同日停在 2016-06-13，其余停在 2018-12-25 / 2019-01-30 /
    2021-02-04 等）；SZSE 399xxx 段 109 个停更；
  - runTDXQuanLiang.py（本地 .day 导入）只会忠实导入缺口；runTDXZhengLiang.py
    （pytdx 在线）此前对指数用错 API（get_security_bars 对指数返回 0 条）且限制
    「起始年必须为今年」，也补不了——两者已修复，但 pytdx 服务器不稳，
    本工具改用东方财富历史 K 线接口（push2his，公开免鉴权）。
  - 已验证与库内 TDX 数据在衔接日逐字段一致（开高低收完全相同，量差 <1e-4，额=元）。

写库方式：直接写 sqlite（dbbardata + dbbaroverview），不经 vnpy/peewee——
批量 executemany 远快于 vnpy 保存路径（实测 2500 条 60s+ vs <0.5s）：
  - 只追加「库内最大日期之后」的新 bar（INSERT OR REPLACE，幂等可重跑）；
  - 同一事务内维护 dbbaroverview 的 count/start/end；
  - 写库前做衔接校验：东财在「库内最后一天」的收盘价与库内偏差 >0.5% 视为
    数据可疑（可能代码对应了不同证券），跳过不写；量纲差（10 的整次幂）自动换算。

用法
----
  python runIndexSupplement.py                          # 默认补 000985.SSE 中证全指
  python runIndexSupplement.py --symbols 000985.SSE,000906.SSE
  python runIndexSupplement.py --auto                   # 自动审计库内全部停更指数并补全
  python runIndexSupplement.py --auto --dry-run         # 只列待补清单，不写库

补完 dbbardata 后，运行复权同步（指数为恒等复制，增量秒级）：
  python runTDXchuquan.py                               # 全市场增量

说明：
  - secid 市场号：SSE=1，SZSE/BSE=0；本工具面向指数（股票数据仍以 TDX 全量包为准）；
  - 88xxxx/999xxx 是通达信自编/别名指数，东方财富无此数据源，--auto 不处理；
  - 东财个别指数历史起点较晚（如 000985 只从 2011-08-02 起），更早的缺口补不到，
    只能补「东财最早可得日期 ~ 现在」；
  - 运行日志里会打印每个指数的东财名称，便于人工复核代码是否对应正确指数。
"""
import argparse
import json
import math
import sqlite3
import sys
import time
import urllib.request

DEFAULT_DB = r"C:\Users\gdl_01\.vntrader\database.db"
EM_KLINE_URL = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
                "secid={secid}&fields1=f1,f2,f3,f4,f5&"
                "fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=0"
                "&beg={beg}&end={end}")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
DEFAULT_SYMBOLS = "000985.SSE"
BATCH_DELAY_SEC = 0.3      # 相邻请求间隔，避免触发限流
PRICE_TOL = 0.005          # 衔接日收盘价相对偏差容忍度（0.5%）


# ---------------- 东方财富 ----------------
def em_secid(symbol: str, exchange: str) -> str:
    market = "1" if exchange.upper() == "SSE" else "0"
    return f"{market}.{symbol}"


def fetch_em_daily(symbol, exchange, start="19900101", end="20500101", retries=3):
    """东财日K（不复权）-> TDXdataRead 同构记录列表（升序）。返回 (名称, rows)。

    fields2: f51=日期 f52=开 f53=收 f54=高 f55=低 f56=成交量 f57=成交额
    """
    secid = em_secid(symbol, exchange)
    url = EM_KLINE_URL.format(secid=secid, beg=start, end=end)
    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
            d = data.get("data") or {}
            klines = d.get("klines") or []
            out = []
            for row in klines:
                p = row.split(",")
                out.append({
                    "date": p[0],
                    "exchange": exchange.upper(),
                    "symbol": symbol,
                    "open": float(p[1]),
                    "high": float(p[3]),
                    "low": float(p[4]),
                    "close": float(p[2]),
                    "amount": float(p[6]) if len(p) > 6 and p[6] else 0.0,
                    "volume": float(p[5]) if len(p) > 5 and p[5] else 0.0,
                })
            return d.get("name", ""), out
        except Exception as e:            # 网络抖动重试
            last_err = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"东方财富请求失败（{secid}）: {last_err}")


def parse_symbol(s: str):
    """'000985.SSE' / '000985' -> (symbol, exchange)。裸 6 位代码：399 开头默认深市，其余沪市。"""
    s = s.strip()
    if "." in s:
        sym, ex = s.split(".", 1)
        return sym.strip(), ex.strip().upper()
    return s, ("SZSE" if s.startswith("399") else "SSE")


# ---------------- 直写 vnpy sqlite 库 ----------------
class BarDbWriter:
    """只追加库内末日期之后的新 bar，并维护 dbbaroverview（幂等可重跑）。"""

    def __init__(self, db_path=DEFAULT_DB):
        self.conn = sqlite3.connect(db_path, timeout=60)
        self.conn.execute("PRAGMA busy_timeout=30000")

    def db_last(self, symbol, exchange):
        row = self.conn.execute(
            "SELECT MAX(datetime) FROM dbbardata WHERE symbol=? AND exchange=? AND interval='d'",
            (symbol, exchange)).fetchone()
        return row[0] if row else None

    def append_bars(self, symbol, exchange, rows):
        """rows 升序；只写 datetime 严格大于库内最大日期的行。返回新增条数。"""
        last = self.db_last(symbol, exchange)
        new = [r for r in rows if last is None or (r["date"] + " 00:00:00") > last]
        if not new:
            return 0
        data = [(symbol, exchange, r["date"] + " 00:00:00", "d",
                 float(r["volume"]), float(r["amount"]), 0.0,
                 float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
                for r in new]
        with self.conn:                      # 单事务：bar + overview 原子更新
            self.conn.executemany(
                "INSERT OR REPLACE INTO dbbardata (symbol, exchange, datetime, interval, "
                "volume, turnover, open_interest, open_price, high_price, low_price, "
                "close_price) VALUES (?,?,?,?,?,?,?,?,?,?,?)", data)
            self._refresh_overview(symbol, exchange)
        return len(new)

    def _refresh_overview(self, symbol, exchange):
        cnt, st, ed = self.conn.execute(
            "SELECT COUNT(*), MIN(datetime), MAX(datetime) FROM dbbardata "
            "WHERE symbol=? AND exchange=? AND interval='d'", (symbol, exchange)).fetchone()
        cur = self.conn.execute(
            "UPDATE dbbaroverview SET count=?, start=?, end=? "
            "WHERE symbol=? AND exchange=? AND interval='d'",
            (cnt, st, ed, symbol, exchange))
        if cur.rowcount == 0:
            self.conn.execute(
                "INSERT INTO dbbaroverview (symbol, exchange, interval, count, start, end) "
                "VALUES (?,?,?,?,?,?)", (symbol, exchange, "d", cnt, st, ed))

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def check_linkage(writer, symbol, exchange, em_rows):
    """衔接校验：库内最后一天的收盘价 与东财同日数据比对。

    返回 (ok, msg, ratio)。
      - ok=False：东财代码可能对应了不同证券，跳过不写；
      - ratio：量纲比（东财量/库量），≈1 同单位；≈10 的整次幂表示量单位不同
        （如 手 vs 股 差 100 倍），写库前按 1/ratio 换算成 TDX 单位。
    """
    last = writer.db_last(symbol, exchange)
    if not last:
        return True, "", None
    last_day = last[:10]
    db_row = writer.conn.execute(
        "SELECT close_price, volume, turnover FROM dbbardata WHERE symbol=? AND exchange=? "
        "AND interval='d' AND datetime=?", (symbol, exchange, last)).fetchone()
    em = {r["date"]: r for r in em_rows}.get(last_day)
    if em is None:
        return True, "东财无衔接日数据", None
    dc, dv, da = db_row
    if dc and abs(em["close"] - dc) / dc > PRICE_TOL:
        return False, f"衔接日收盘不一致：东财 {em['close']} vs 库 {dc}", None
    ratio = None
    if dv and em["volume"]:
        r_ = em["volume"] / dv
        k = round(math.log10(r_))
        if abs(r_ - 10 ** k) / (10 ** k) < 0.1:   # 接近 10 的整次幂 → 视为量纲差
            ratio = 10 ** k
    return True, "", ratio


# ---------------- 自动审计 ----------------
def index_segment(symbol, exchange):
    """是否属于东财可补的指数段：SSE 000xxx / SZSE 399xxx / BSE 899xxx。"""
    if len(symbol) != 6:
        return False
    if exchange == "SSE":
        return symbol.startswith("000")
    if exchange == "SZSE":
        return symbol.startswith("399")
    if exchange == "BSE":
        return symbol.startswith("899")
    return False


def find_stale_index_symbols(db_path=DEFAULT_DB, stale_days=120):
    """库内数据明显落后（> stale_days 天）的指数段标的清单，返回 (清单, 库内最新日期)。"""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=30000")
    rows = conn.execute(
        "SELECT symbol, exchange, end FROM dbbaroverview WHERE interval='d'").fetchall()
    conn.close()
    if not rows:
        return [], None
    global_end = max((r[2] for r in rows if r[2]), default=None)
    if not global_end:
        return [], None
    ge_dt = _p(global_end)
    out = []
    for sym, ex, ed in rows:
        if not ed or not index_segment(sym, ex):
            continue
        d = _p(ed)
        if (ge_dt - d).days > stale_days:
            out.append((sym, ex, ed[:10]))
    return sorted(out), global_end[:10]


def _p(s):
    from datetime import datetime as _dt
    return _dt.strptime(s[:10], "%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser(description="东方财富指数日线补全（直写 dbbardata）")
    ap.add_argument("--symbols", default=None,
                    help=f"逗号分隔，如 000985.SSE,000906.SSE（缺省 {DEFAULT_SYMBOLS}；--auto 时忽略）")
    ap.add_argument("--auto", action="store_true", help="自动审计库内全部停更指数段标的并补全")
    ap.add_argument("--dry-run", action="store_true", help="只列清单/校验，不写库")
    ap.add_argument("--start", default="19900101", help="起始日期 YYYYMMDD（默认全历史）")
    ap.add_argument("--end", default="20500101", help="结束日期 YYYYMMDD（默认至今）")
    ap.add_argument("--stale-days", type=int, default=120, help="--auto 停更判定天数（默认120）")
    ap.add_argument("--db", default=DEFAULT_DB, help="目标库路径")
    args = ap.parse_args()

    t0 = time.time()
    if args.auto:
        stale, global_end = find_stale_index_symbols(args.db, args.stale_days)
        print(f"[auto] 库内最新日线 {global_end}；停更(>{args.stale_days}天)指数段标的 {len(stale)} 个",
              flush=True)
        targets = [f"{s}.{e}" for s, e, _ in stale]
        if args.dry_run:
            for s, e, last in stale:
                print(f"    {s}.{e}  库内止于 {last}", flush=True)
            print(f"[dry-run] 未写库。若要补全，去掉 --dry-run 即可。", flush=True)
            return 0
    else:
        targets = [x.strip() for x in (args.symbols or DEFAULT_SYMBOLS).split(",") if x.strip()]

    writer = BarDbWriter(args.db)
    ok, skip, fail, total_new = [], [], [], 0
    for i, t in enumerate(targets, 1):
        sym, ex = parse_symbol(t)
        msg = ""
        try:
            name, rows = fetch_em_daily(sym, ex, args.start, args.end)
        except Exception as e:
            print(f"[{i}/{len(targets)}] {sym}.{ex} 获取失败: {e}", flush=True)
            fail.append(t)
            continue
        if not rows:
            print(f"[{i}/{len(targets)}] {sym}.{ex} 东财无数据，跳过", flush=True)
            skip.append(t)
            continue
        okflag, lmsg, ratio = check_linkage(writer, sym, ex, rows)
        if lmsg:
            msg = f"；{lmsg}"
        if not okflag:
            print(f"[{i}/{len(targets)}] {sym}.{ex} [{name}] 衔接校验失败{msg}，跳过写库", flush=True)
            fail.append(t)
            continue
        if ratio and ratio != 1.0:
            inv = 1.0 / ratio
            for r in rows:
                r["volume"] *= inv
            msg += f"；量纲差 {ratio:g}× 已按 TDX 单位换算"
        if rows[0]["date"] > (writer.db_last(sym, ex) or "9999")[:10]:
            msg += f"；东财自 {rows[0]['date']} 起有数据（与库末之间可能有无法补到的空洞）"
        if args.dry_run:
            print(f"[{i}/{len(targets)}] {sym}.{ex} [{name}] 东财 {rows[0]['date']}~{rows[-1]['date']} "
                  f"{len(rows)} 条（dry-run 不写库{msg}）", flush=True)
            ok.append(t)
            continue
        t1 = time.time()
        try:
            n = writer.append_bars(sym, ex, rows)
        except Exception as e:
            print(f"[{i}/{len(targets)}] {sym}.{ex} 写库失败: {e}", flush=True)
            fail.append(t)
            continue
        total_new += n
        end = writer.db_last(sym, ex) or "?"
        print(f"[{i}/{len(targets)}] {sym}.{ex} [{name}] 新增 {n} 条 -> 止于 {end[:10]}"
              f"（{time.time()-t1:.1f}s{msg}）", flush=True)
        ok.append(t)
        time.sleep(BATCH_DELAY_SEC)
    writer.close()

    print("-" * 62, flush=True)
    print(f"完成：成功 {len(ok)} | 跳过(无数据/已最新) {len(skip)} | 失败 {len(fail)} "
          f"| 共新增 {total_new} 条 | 总耗时 {time.time()-t0:.1f}s", flush=True)
    if total_new:
        print("已写入 dbbardata。请运行复权同步： python runTDXchuquan.py", flush=True)
    return 0 if (ok or skip) and not fail else 1


if __name__ == "__main__":
    sys.exit(main())
