# -*- coding: utf-8 -*-
"""
runUpdataTDX.py —— 按「股票代码 + 交易所」在线更新日线数据到当前时间

数据源：TDXdataRead.read_tdx_day_online(symbol, start_date, end_date)（pytdx 在线接口，
指数自动走 get_index_bars、股票走 get_security_bars）。
写库方式：与 runTDXQuanLiang.py 完全一致（VNPYdataWriter.convertToVNPYbarData
+ write_tdx_day 增量写入，已存在的日期自动跳过，幂等可重跑）。

起始日期自动确定：
  - 库里已有该标的日线 -> 从「库内最大日期 + 1 天」开始（增量，不重复拉全历史）；
  - 库里没有该标的     -> 从 1990-01-01 开始（全历史）。
  也可用 --start 手工指定、--end 指定截止日（默认今天）。

用法
----
  python runUpdataTDX.py 000985.SSE          # 更新中证全指到当前时间
  python runUpdataTDX.py 000985SSE          # 同上（代码与交易所连写）
  python runUpdataTDX.py sh000985           # 同上（TDX 带市场前缀写法）
  python runUpdataTDX.py 000985.SSE 000906.SSE          # 多个标的
  python runUpdataTDX.py 000985.SSE,000906.SSE          # 逗号分隔也行
  python runUpdataTDX.py 000001.SZSE --start 2026-01-01 # 指定起始日

注意：
  - 裸 6 位代码（如 000001）无法区分市场（沪=上证指数/深=平安银行），必须带交易所；
  - 更新完 dbbardata 后，请按需同步复权表：
      python runTDXchuquan.py --symbol <代码>    # 只同步该代码
      python runTDXchuquan.py                    # 全市场增量
  - pytdx 行情服务器不稳定（连不上会自动换服务器重试），全部失败时会提示「未获取到数据」。
"""
import argparse
import re
import sqlite3
from datetime import datetime, timedelta

import TDXdataRead as TdxRead
import VNPYdataWriter as VNPYdataWriter

DEFAULT_DB = r"C:\Users\gdl_01\.vntrader\database.db"   # 与 VNPYdataWriter 同一个库


def parse_target(s: str):
    """'000985.SSE' / '000985SSE' / 'sh000985' -> (代码, 交易所)。裸代码返回 (代码, None)。"""
    s = str(s).strip().lower()
    m = re.match(r"^(\d{6})(sse|szse|bse)$", s)
    if m:
        return m.group(1), m.group(2).upper()
    if s.startswith(("sh", "sz", "bj")) and len(s) > 4:
        return s[2:], {"sh": "SSE", "sz": "SZSE", "bj": "BSE"}[s[:2]]
    if "." in s:
        code, ex = s.split(".", 1)
        return code, ex.upper()
    return s, None


def db_last_date(code: str, exchange: str, db_path: str = DEFAULT_DB):
    """库里该标的日线最大日期（'YYYY-MM-DD HH:MM:SS'），没有返回 None。

    走 (symbol, exchange, interval, datetime) 复合索引，瞬时返回。
    """
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout=30000")
        row = conn.execute(
            "SELECT MAX(datetime) FROM dbbardata WHERE symbol=? AND exchange=? "
            "AND interval='d'", (code, exchange)).fetchone()
        conn.close()
        return row[0] if row and row[0] else None
    except sqlite3.Error:
        return None


def main():
    ap = argparse.ArgumentParser(description="按代码+交易所在线更新日线到当前时间")
    ap.add_argument("targets", nargs="+",
                    help="股票代码+交易所：000985.SSE / 000985SSE / sh000985（可多个或逗号分隔）")
    ap.add_argument("--start", default=None,
                    help="起始日 YYYY-MM-DD（缺省=库内末日期+1天；库里没有则全历史）")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"),
                    help="截止日 YYYY-MM-DD（默认今天）")
    args = ap.parse_args()

    # 展开：支持空格分隔 + 逗号分隔
    targets = []
    for t in args.targets:
        targets += [x.strip() for x in t.split(",") if x.strip()]

    vnpWrite = VNPYdataWriter.VNPYdataWriter()
    print(f"在线更新 {len(targets)} 个标的，截止 {args.end}", flush=True)
    updated, skipped, failed = [], [], []

    for t in targets:
        code, ex = parse_target(t)
        if ex is None:
            print(f"[跳过] {t}: 裸代码无法区分市场（000001 沪=上证指数/深=平安银行），"
                  f"请写全 如 {t}.SSE / {t}.SZSE", flush=True)
            skipped.append(t)
            continue

        # 起始日：库内末日期 + 1 天；库里没有则全历史
        last = db_last_date(code, ex)
        if args.start:
            start = args.start
        elif last:
            start = (datetime.strptime(last[:10], "%Y-%m-%d")
                     + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            start = "1990-01-01"

        symbol = TdxRead.convertSymbolVNPY2TDX(ex) + code   # 如 SSE+000985 -> sh000985

        # 未显式指定 --start 且库内末日期已到截止日 -> 已是最新，无需在线获取
        # （显式 --start 时不拦截：允许回补/修复指定区间，重复日期由写库增量逻辑跳过）
        if not args.start and last and last[:10] >= args.end:
            print(f"[{code}.{ex}] 库内已到 {last[:10]}，数据已是最新，无需更新", flush=True)
            skipped.append(f"{code}.{ex}")
            continue

        print(f"[{code}.{ex}] 库内止于 {last[:10] if last else '（无数据）'}，"
              f"在线获取 {start} ~ {args.end} ...", flush=True)

        stock_data = TdxRead.read_tdx_day_online(symbol, start, args.end)
        if not stock_data:
            print(f"  未获取到数据（服务器连接失败或区间内无新数据）", flush=True)
            failed.append(f"{code}.{ex}")
            continue

        print(f"  读取到 {len(stock_data)} 条记录"
              f"（{stock_data[0]['date']} ~ {stock_data[-1]['date']}）", flush=True)
        vnpWrite.write_tdx_day(vnpWrite.convertToVNPYbarData(stock_data))
        new_last = db_last_date(code, ex)
        print(f"  库内现止于 {new_last[:10] if new_last else '?'}", flush=True)
        updated.append(f"{code}.{ex}")

    print("-" * 60, flush=True)
    print(f"完成：更新 {len(updated)} 个 {updated}"
          + (f" | 跳过 {len(skipped)}" if skipped else "")
          + (f" | 失败 {len(failed)} {failed}" if failed else ""), flush=True)
    if updated:
        codes = ",".join(sorted({u.split(".")[0] for u in updated}))
        print(f"已写入 dbbardata。如需同步复权表： python runTDXchuquan.py --symbol {codes}",
              flush=True)


if __name__ == "__main__":
    main()
