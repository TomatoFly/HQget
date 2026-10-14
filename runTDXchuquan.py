# -*- coding: utf-8 -*-
"""
runTDXchuquan.py —— 离线生成 前复权 / 后复权 数据并落库（增量入口）

默认自动扫描 D:\\HQdata\\tdxData\\ 目录下的 gbbq.zip 作为权息来源（含递归查找）。

流程：
  1. 若 dbbardata_fwd / dbbardata_bwd 两张表不存在，则新建（结构同 dbbardata，
     独立主键 id，复合唯一索引 (symbol, exchange, interval, datetime)）；
  2. 增量同步（adjust_incremental.sync_adjusted）：
     - 首次（无 cqInfo 水位）/ 新股 / 数据回填 / 事件被修改 -> 该股全量重建；
     - gbbq 追加新事件 -> SQL UPDATE 因子传播（秒级）；
     - dbbardata 追加新日线 -> 仅计算新 bar 插入；
     - 无变化 -> 跳过（零读写）。
  --rebuild: 忽略水位全市场全量重算（校准浮点漂移 / 数据异常时兜底）。

核心复权数学复用 adjust_factor_builder（口径见该文件），单事件因子存 cqInfo 表。
"""
import os
import time
import argparse
from datetime import datetime

_T0_IMPORT = time.time()      # 模块加载起点（vnpy 依赖导入很慢，纳入耗时统计）

from peewee import fn

from vnpy_adj_database import DbBarDataFwd, DbBarDataBwd, ensure_adj_tables, ensure_cqinfo_table
from vnpy_sqlite.sqlite_database import db
from adjust_incremental import sync_adjusted

#电信域名：
#http://www.tdx.com.cn/products/data/data/dbf/gbbq.zip
#电信IP：
#http://59.175.238.39/products/data/data/dbf/gbbq.zip
#网通域名：
#http://cnc.tdx.com.cn/dbf/gbbq.zip
#网通IP：
#http://210.51.55.211/dbf/gbbq.zip
# 默认 TDX 数据根目录（含 gbbq.zip）
DEFAULT_TDX_DIR = r"D:\HQdata\tdxData"


def resolve_gbbq(tdx_dir: str) -> str:
    """默认自动扫描 tdx_dir 下的 gbbq.zip（含递归），返回其所在目录，
    以便把正确目录传给 adjust_factor_builder（它按 tdx_dir/gbbq.zip 读取）。"""
    direct = os.path.join(tdx_dir, "gbbq.zip")
    if os.path.exists(direct):
        return tdx_dir
    for root, _dirs, files in os.walk(tdx_dir):
        if "gbbq.zip" in files:
            return root
    return tdx_dir   # 没找到：交给底层逻辑（离线失败会回退 online 或报错）


def _fmt_hms(sec: float) -> str:
    """秒 -> 人可读：'3.4s' / '02m03s' / '1h05m09s'"""
    sec = max(0.0, float(sec))
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(int(round(sec)), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m{s:02d}s"


def _count_rows(model) -> int:
    """表行数估算；表不存在返回 -1。

    用 MAX(id) 而非 COUNT(*)：后者在千万行表上要全表/全索引扫描（本次实测 ~80s），
    MAX(id) 走主键索引一次定位即可，用于进度展示足够（全量重建会删旧行插新行，
    id 只增不减，故该值只会偏大不会偏小）。
    """
    if not _table_exists(model):
        return -1
    v = model.select(fn.MAX(model.id)).scalar()
    return int(v) if v else 0


def _table_exists(model) -> bool:
    """可靠判断复权表是否存在。

    注意：peewee 的 Model.table_exists() 在连接未打开(closed)时会误报 True，
    因此这里改用已打开连接上的 db.get_tables()（实测与 sqlite_master 一致，可靠）。
    """
    if db.is_closed():
        db.connect()
    return model._meta.table_name in db.get_tables()


def ensure_adj_tables_step1():
    """第 1 步：若 dbbardata_fwd / dbbardata_bwd / cqInfo 不存在则创建。
    返回 (fwd_existed_before, bwd_existed_before)——用于判断是否需要强制全量。"""
    t = time.time()
    fwd_exists = _table_exists(DbBarDataFwd)
    bwd_exists = _table_exists(DbBarDataBwd)
    if not fwd_exists or not bwd_exists:
        ensure_adj_tables()   # safe=True：已存在跳过；仅建表，不改动 dbbardata
        print("第 1 步：已创建缺失的复权表 "
              f"（dbbardata_fwd={'新建' if not fwd_exists else '已存在'}, "
              f"dbbardata_bwd={'新建' if not bwd_exists else '已存在'}）", flush=True)
    else:
        n_fwd, n_bwd = _count_rows(DbBarDataFwd), _count_rows(DbBarDataBwd)
        print(f"第 1 步：复权表均已存在，跳过建表 "
              f"（dbbardata_fwd 最大 id ≈{n_fwd:,}, dbbardata_bwd 最大 id ≈{n_bwd:,}；"
              f"精确 COUNT(*) 在该量级需数十秒，故只报估算）", flush=True)
    ensure_cqinfo_table()
    print(f"         cqInfo 水位表就绪（{_fmt_hms(time.time() - t)}）", flush=True)
    return fwd_exists, bwd_exists


def main():
    ap = argparse.ArgumentParser(description="离线生成前/后复权数据并落库（增量）")
    ap.add_argument("--tdx-dir", default=DEFAULT_TDX_DIR,
                    help=f"含 gbbq.zip 的 TDX 目录（默认 {DEFAULT_TDX_DIR}，自动扫描 gbbq.zip）")
    ap.add_argument("--symbol", help="仅处理指定 6 位代码（可选，缺省=全市场日线）")
    ap.add_argument("--source", choices=["offline", "online"], default="offline",
                    help="权息因子来源：offline=TDXgbbqReader 离线硬解析(默认)；online=mootdx 在线 xdxr")
    ap.add_argument("--rebuild", action="store_true",
                    help="忽略 cqInfo 水位，全市场全量重算（定期校准浮点漂移/数据异常兜底）")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每股动作")
    ap.add_argument("--log-interval", type=float, default=10.0,
                    help="进度行打印的最小间隔秒数（默认 10，另每 5%% 至少打印一次）")
    args = ap.parse_args()

    t_all = time.time()
    print("=" * 66, flush=True)
    print(f"复权数据同步 开始于 {datetime.now():%Y-%m-%d %H:%M:%S} "
          f"（环境加载 {_fmt_hms(t_all - _T0_IMPORT)}）", flush=True)
    print(f"数据库: {getattr(db, 'database', '?')}", flush=True)
    print(f"参数: source={args.source} | symbol={args.symbol or '全市场'} | "
          f"rebuild={args.rebuild} | verbose={args.verbose} | "
          f"log-interval={args.log_interval}s", flush=True)

    # 第 0 步：定位 gbbq.zip（默认自动扫描 tdx_dir）
    t0 = time.time()
    print("\n第 0 步：定位权息文件 ...", flush=True)
    tdx_dir = resolve_gbbq(args.tdx_dir)
    gbbq = os.path.join(tdx_dir, "gbbq.zip")
    if os.path.exists(gbbq):
        st = os.stat(gbbq)
        print(f"权息文件: {gbbq} "
              f"（{st.st_size / 1024 / 1024:.2f} MB, "
              f"修改时间 {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M:%S}）", flush=True)
    else:
        print(f"[warn] 未在 {args.tdx_dir} 下找到 gbbq.zip；将依赖 --source 回退"
              f"（online 需联网）", flush=True)
    print(f"        完成（{_fmt_hms(time.time() - t0)}）", flush=True)

    # 第 1 步：确保三张表存在；若复权表此前不存在（新建/被删），强制全量
    fwd_exists, bwd_exists = ensure_adj_tables_step1()
    rebuild = args.rebuild or not fwd_exists or not bwd_exists
    if rebuild:
        print("[模式] 本次将执行全量重建"
              + ("（复权表为新建/被删除 -> 强制全量）" if (not fwd_exists or not bwd_exists)
                 else "（--rebuild）"), flush=True)
    else:
        print("[模式] 增量同步（按 cqInfo 水位只处理变化部分）", flush=True)

    # 第 2 步：增量同步前/后复权数据
    print("\n第 2 步：同步前/后复权数据 ...", flush=True)
    t2 = time.time()
    sync_adjusted(tdx_dir, source=args.source, symbol=args.symbol,
                  rebuild=rebuild, verbose=args.verbose,
                  progress_sec=args.log_interval)
    print(f"        第 2 步耗时 {_fmt_hms(time.time() - t2)}", flush=True)

    # 第 3 步：收尾核对
    print("\n第 3 步：核对落库结果 ...", flush=True)
    n_fwd, n_bwd = _count_rows(DbBarDataFwd), _count_rows(DbBarDataBwd)
    print(f"落库结果: dbbardata_fwd 最大 id ≈{n_fwd:,} | "
          f"dbbardata_bwd 最大 id ≈{n_bwd:,}", flush=True)
    print(f"\n全部完成，总耗时 {_fmt_hms(time.time() - t_all)}"
          f"（结束于 {datetime.now():%H:%M:%S}）", flush=True)


if __name__ == "__main__":
    main()
