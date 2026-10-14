# -*- coding: utf-8 -*-
"""
runJBM20260727.py

功能：将 D:\\HQdata\\jbmDataV1 下的基本面日快照 CSV（含压缩包内 CSV）解析写入
      C:\\Users\\gdl_01\\.vntrader\\fundamental.db 的两张表
      jbmbaseA202607 / jbmbaseB202607。
jbm表示基本面
设计要点：
1) 检查 fundamental.db 是否存在，不存在则创建。
2) 检查两张表是否存在，不存在则创建（字段按需求定义）。
3) symbol/exchange/datetime 结构与 VNPY 的 database.db 一致：
     symbol   TEXT
     exchange TEXT（取 Exchange 枚举 value，如 'SSE'/'SZSE'，即 VNPY 约定）
     datetime TEXT 存 'YYYY-MM-DD HH:MM:SS'（与 peewee DateTimeField 一致）
4) 首次建表时按解析数据推断数值列类型（INTEGER/REAL）；表已存在则用 PRAGMA 读取类型。
5) 默认扫描 DATA_DIR 下 *.csv 及压缩文件（.zip / .rar）内的 *.csv。
6) 写入：同只股票相邻时间点仅写入“有变化”的记录（A、B 表均启用）；
     重复 (symbol,exchange,datetime) 默认不覆盖（overwrite=False），并在终端输出重复信息。
"""
import os
import csv
import sqlite3
import zipfile
import io
import subprocess
import tempfile
import shutil
import glob
from datetime import datetime

# ---------- 路径配置 ----------
FUND_DIR = r"C:\Users\gdl_01\.vntrader"
DB_NAME = "fundamental.db"
DB_PATH = os.path.join(FUND_DIR, DB_NAME)
DATA_DIR = r"D:\HQdata\jbmDataV1"
TABLE_A = "jbmbaseA202607"
TABLE_B = "jbmbaseB202607"

# ---------- 中文列名 -> 英文字段名（与需求一致）----------
# jbmbaseA202607：名称,行业,城市,上市日期,总股本(亿),流通股本(亿),总资产(亿),
#   流动资产(亿),固定资产(亿),每股净资产,每股收益,市净率,未分配利润,每股未分配利润,
#   公积金,每股公积金,毛利率%,净利润率%,收入同比%,利润同比%,股东人数
A_CN_EN = [
    ("名称", "name"), ("行业", "industry"), ("城市", "city"), ("上市日期", "list_date"),
    ("总股本(亿)", "total_shares"), ("流通股本(亿)", "float_shares"), ("总资产(亿)", "total_assets"),
    ("流动资产(亿)", "current_assets"), ("固定资产(亿)", "fixed_assets"), ("每股净资产", "bvps"),
    ("每股收益", "eps"), ("市净率", "pb"), ("未分配利润", "undistributed_profit"),
    ("每股未分配利润", "udpps"), ("公积金", "reserve"), ("每股公积金", "rpps"),
    ("毛利率%", "gross_margin"), ("净利润率%", "net_margin"), ("收入同比%", "revenue_yoy"),
    ("利润同比%", "profit_yoy"), ("股东人数", "shareholders"),
]
# jbmbaseB202607：市盈率(动),内盘(手),外盘(手),振幅,强弱度%,活跃度%,攻击波%
B_CN_EN = [
    ("市盈率(动)", "pe_ttm"), ("内盘(手)", "inner_volume"), ("外盘(手)", "outer_volume"),
    ("振幅", "amplitude"), ("强弱度%", "strength"), ("活跃度%", "activity"), ("攻击波%", "attack_wave"),
]
CN_TO_EN = dict(A_CN_EN + B_CN_EN)
# 文本类字段（固定为 TEXT，其余数值列按解析数据推断 INTEGER/REAL）
TEXT_CN = {"名称", "行业", "城市", "上市日期"}
# exchange 采用 VNPY 中 Exchange 枚举 value 的约定
EX_MAP = {".SH": "SSE", ".SZ": "SZSE", ".BJ": "BJSE"}

SAMPLE_ROWS = 2000  # 类型推断每文件采样行数


# ---------- 数据源扫描（普通 csv + 压缩包内 csv）----------
def find_7z():
    """查找 7z.exe / 7za.exe 路径（rar 解压后端，支持 Rar5）。"""
    candidates = ["7z.exe", "7za.exe",
                  r"C:\Program Files\7-Zip\7z.exe",
                  r"C:\Program Files (x86)\7-Zip\7z.exe"]
    for c in candidates:
        try:
            if c in ("7z.exe", "7za.exe"):
                p = shutil.which(c)
                if p:
                    return p
            elif os.path.exists(c):
                return c
        except Exception:
            pass
    return None


def decode_csv_bytes(data):
    """去除可能出现的多重 BOM（部分历史 rar 内 csv 带双 BOM）后以 utf-8 解码。"""
    while data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8")


def _iter_rar_7z(p):
    """用 7z 解压 rar（含 Rar5）到临时目录，逐个 csv 产出 (label, StringIO)。"""
    seven_zip = find_7z()
    if not seven_zip:
        return
    tmp = tempfile.mkdtemp(prefix="jbm_rar_")
    try:
        r = subprocess.run([seven_zip, "x", "-y", f"-o{tmp}", p],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[WARN] 7z 解压 rar 失败 {p}: {r.stderr[:200]}")
            return
        for csv_path in sorted(glob.glob(os.path.join(tmp, "**", "*.csv"), recursive=True)):
            with open(csv_path, "rb") as fh:
                yield (f"{p}|{os.path.basename(csv_path)}",
                       io.StringIO(decode_csv_bytes(fh.read())))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _iter_rar_rarfile(p, rar_mod):
    """回退方案：用 rarfile（需 unrar 后端）解压 rar 内 csv。"""
    try:
        rf = rar_mod.RarFile(p)
        for name in sorted(rf.namelist()):
            if name.lower().endswith(".csv"):
                data = rf.read(name)
                yield (f"{p}|{name}", io.StringIO(decode_csv_bytes(data)))
        rf.close()
    except Exception as e:
        print(f"[WARN] 读取 rar 失败 {p}: {e}")


def iter_csv_streams(data_dir):
    """流式产出 (label, text_stream)。支持普通 csv、zip 内 csv、rar 内 csv。"""
    try:
        import rarfile
        rar_mod = rarfile
    except ImportError:
        rar_mod = None
    seven_zip = find_7z()
    rar_count = 0
    for root, _dirs, files in os.walk(data_dir):
        for f in sorted(files):
            if f.lower().endswith(".csv"):
                p = os.path.join(root, f)
                with open(p, "rb") as fh:
                    yield (p, io.StringIO(decode_csv_bytes(fh.read())))
        for f in sorted(files):
            if f.lower().endswith(".zip"):
                p = os.path.join(root, f)
                try:
                    zf = zipfile.ZipFile(p)
                    for name in sorted(zf.namelist()):
                        if name.lower().endswith(".csv"):
                            data = zf.read(name)
                            yield (f"{p}|{name}", io.StringIO(decode_csv_bytes(data)))
                    zf.close()
                except Exception as e:
                    print(f"[WARN] 读取 zip 失败 {p}: {e}")
        for f in sorted(files):
            if f.lower().endswith(".rar"):
                rar_count += 1
                p = os.path.join(root, f)
                if seven_zip:
                    for item in _iter_rar_7z(p):
                        yield item
                elif rar_mod:
                    for item in _iter_rar_rarfile(p, rar_mod):
                        yield item
    if rar_count and seven_zip:
        print(f"[提示] 已用 7z 处理 {rar_count} 个 .rar 压缩包（含历史数据）。")
    elif rar_count and not seven_zip and not rar_mod:
        print(f"[提示] 检测到 {rar_count} 个 .rar 压缩包，但缺少解压后端"
              f"（需 7z 或 rarfile+unrar），已跳过。")


# ---------- 类型推断 / 建表 ----------
def infer_column_types(data_dir):
    """按解析数据推断数值列类型：含小数点 -> REAL，否则 -> INTEGER。"""
    state = {cn: False for cn in (set(CN_TO_EN) - TEXT_CN)}  # True 表示需 REAL
    for label, stream in iter_csv_streams(data_dir):
        try:
            reader = csv.reader(stream)
            h = next(reader)
        except StopIteration:
            stream.close()
            continue
        idx = {name: i for i, name in enumerate(h)}
        n = 0
        for row in reader:
            if n >= SAMPLE_ROWS:
                break
            n += 1
            for cn in state:
                i = idx.get(cn)
                if i is None or i >= len(row):
                    continue
                v = row[i].strip()
                if v in ("", "-", "--"):
                    continue
                if "." in v:
                    state[cn] = True
        stream.close()
    return {cn: ("REAL" if state[cn] else "INTEGER") for cn in state}


def get_existing_types(cur, table, cn_en):
    cur.execute(f"PRAGMA table_info({table})")
    cols = {r[1]: r[2] for r in cur.fetchall()}
    return {cn: cols.get(en, "REAL") for cn, en in cn_en}


def build_create_sql(table, cn_en, col_type):
    parts = ["symbol TEXT NOT NULL", "exchange TEXT NOT NULL", "datetime TEXT NOT NULL"]
    for cn, en in cn_en:
        t = "TEXT" if cn in TEXT_CN else col_type.get(cn, "REAL")
        parts.append(f"{en} {t}")
    parts.append("PRIMARY KEY (symbol, exchange, datetime)")
    return f"CREATE TABLE IF NOT EXISTS {table} (\n  " + ",\n  ".join(parts) + "\n)"


# ---------- 解析与写入 ----------
def parse_row(row, idx):
    di = idx.get("日期")
    ci = idx.get("代码")
    if di is None or ci is None or di >= len(row) or ci >= len(row):
        return None
    dstr = row[di].strip()
    try:
        dt = datetime.strptime(dstr, "%Y%m%d")
    except Exception:
        return None
    code = row[ci].strip()
    if "." not in code:
        return None
    sym, suf = code.split(".", 1)
    ex = EX_MAP.get("." + suf.upper(), suf.upper())
    raw = {}
    for cn in CN_TO_EN:
        i = idx.get(cn)
        raw[cn] = row[i].strip() if (i is not None and i < len(row)) else ""
    return {"symbol": sym, "exchange": ex, "dt": dt,
            "dt_str": dt.strftime("%Y-%m-%d %H:%M:%S"), "raw": raw}


def to_typed(raw, ctype):
    raw = (raw or "").strip()
    if raw in ("", "-", "--"):
        return None
    if ctype == "INTEGER":
        try:
            return int(round(float(raw)))
        except ValueError:
            return None
    try:
        return float(raw)
    except ValueError:
        return None


def table_exists(cur, table):
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def insert_record(cur, table, cols, vals, seen, overwrite=False):
    """写入一条记录。overwrite=False 时若 (symbol,exchange,datetime) 已存在则跳过。
    去重基于内存 seen 集合（启动时载入已有主键），避免逐行 SELECT 造成锁竞争；
    并额外捕获 UNIQUE 约束作为兜底（如 A/B 两表主键集合因变化过滤发散而不一致时）。
    返回 'inserted' / 'dup'。"""
    sym, ex, dt = vals[0], vals[1], vals[2]
    key = (sym, ex, dt)
    if not overwrite and key in seen:
        return "dup"
    ph = ",".join(["?"] * len(cols))
    try:
        cur.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph})", vals)
    except sqlite3.IntegrityError:
        # 主键已存在（默认不覆盖）
        return "dup"
    seen.add(key)
    return "inserted"


def write_all(conn, col_type, seen):
    cur = conn.cursor()
    a_cols = ["symbol", "exchange", "datetime"] + [en for _, en in A_CN_EN]
    b_cols = ["symbol", "exchange", "datetime"] + [en for _, en in B_CN_EN]
    prev_a, prev_b = {}, {}
    stats = {"files": 0, "parsed": 0, "ins_a": 0, "ins_b": 0,
             "skip_a": 0, "skip_b": 0, "dup_a": 0, "dup_b": 0}
    for label, stream in iter_csv_streams(DATA_DIR):
        stats["files"] += 1
        try:
            reader = csv.reader(stream)
            h = next(reader)
        except StopIteration:
            stream.close()
            continue
        idx = {name: i for i, name in enumerate(h)}
        if "日期" not in idx or "代码" not in idx:
            print(f"[WARN] 跳过（缺日期/代码列）: {label}")
            stream.close()
            continue
        for row in reader:
            rec = parse_row(row, idx)
            if rec is None:
                continue
            stats["parsed"] += 1
            sym = rec["symbol"]
            # ---- A 表 ----
            a_vals = [sym, rec["exchange"], rec["dt_str"]]
            a_ind = []
            for cn, _en in A_CN_EN:
                raw = rec["raw"].get(cn, "")
                if cn in TEXT_CN:
                    a_vals.append(raw if raw != "" else None)
                else:
                    a_vals.append(to_typed(raw, col_type[cn]))
                a_ind.append(a_vals[-1])
            a_ind_t = tuple(a_ind)
            if prev_a.get(sym) == a_ind_t:
                stats["skip_a"] += 1
            else:
                st = insert_record(cur, TABLE_A, a_cols, a_vals, seen, overwrite=False)
                if st == "dup":
                    stats["dup_a"] += 1
                    if stats["dup_a"] <= 20:
                        print(f"[重复-A] {sym}.{rec['exchange']} {rec['dt_str']} 已存在，不覆盖")
                else:
                    stats["ins_a"] += 1
                prev_a[sym] = a_ind_t
            # ---- B 表 ----
            b_vals = [sym, rec["exchange"], rec["dt_str"]]
            b_ind = []
            for cn, _en in B_CN_EN:
                b_vals.append(to_typed(rec["raw"].get(cn, ""), col_type[cn]))
                b_ind.append(b_vals[-1])
            b_ind_t = tuple(b_ind)
            if prev_b.get(sym) == b_ind_t:
                stats["skip_b"] += 1
            else:
                st = insert_record(cur, TABLE_B, b_cols, b_vals, seen, overwrite=False)
                if st == "dup":
                    stats["dup_b"] += 1
                    if stats["dup_b"] <= 20:
                        print(f"[重复-B] {sym}.{rec['exchange']} {rec['dt_str']} 已存在，不覆盖")
                else:
                    stats["ins_b"] += 1
                prev_b[sym] = b_ind_t
        stream.close()
        conn.commit()
        print(f"[进度] 已处理 {stats['files']} 个文件，累计写入 A={stats['ins_a']} B={stats['ins_b']}")
    print("=" * 44)
    print(f"处理文件数         : {stats['files']}")
    print(f"解析记录数         : {stats['parsed']}")
    print(f"A 表 写入={stats['ins_a']}  跳过(无变化)={stats['skip_a']}  重复={stats['dup_a']}")
    print(f"B 表 写入={stats['ins_b']}  跳过(无变化)={stats['skip_b']}  重复={stats['dup_b']}")


def main():
    os.makedirs(FUND_DIR, exist_ok=True)
    existed = os.path.exists(DB_PATH)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    cur = conn.cursor()
    print(f"数据库路径: {DB_PATH}")
    print(f"数据库文件: {'已存在' if existed else '不存在，将新建'}")
    a_exists = table_exists(cur, TABLE_A)
    b_exists = table_exists(cur, TABLE_B)
    if not (a_exists and b_exists):
        print("推断字段数据类型（基于解析数据）...")
        col_type = infer_column_types(DATA_DIR)
        print("  推断类型:", {CN_TO_EN[c]: t for c, t in col_type.items()})
    else:
        col_type = {}
        col_type.update(get_existing_types(cur, TABLE_A, A_CN_EN))
        col_type.update(get_existing_types(cur, TABLE_B, B_CN_EN))
    if not a_exists:
        print(f"创建表 {TABLE_A}")
        cur.execute(build_create_sql(TABLE_A, A_CN_EN, col_type))
    if not b_exists:
        print(f"创建表 {TABLE_B}")
        cur.execute(build_create_sql(TABLE_B, B_CN_EN, col_type))
    conn.commit()
    # 载入已有主键到内存集合，用于写入时去重（避免逐行 SELECT 导致锁竞争）
    seen = set()
    if a_exists or b_exists:
        cur.execute(f"SELECT symbol,exchange,datetime FROM {TABLE_A}")
        seen.update(tuple(r) for r in cur.fetchall())
        cur.execute(f"SELECT symbol,exchange,datetime FROM {TABLE_B}")
        seen.update(tuple(r) for r in cur.fetchall())
    print(f"已载入已有主键 {len(seen)} 条，开始写入...")
    write_all(conn, col_type, seen)
    conn.close()
    print("完成。")


if __name__ == "__main__":
    main()
