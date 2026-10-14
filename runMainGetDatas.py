# -*- coding: utf-8 -*-
"""
runMainGetDatas.py —— 每日数据更新主流程（一键串联）

流程：
  1. 检查 tdxData/hsjday.zip 与 tdxData/gbbq.zip 是否为「最新」：
     - 与下载源响应头 Last-Modified 比对，远端有更新才重新下载（hsjday 约 550MB）；
     - 远端不可达时退化为「本地文件日期=今天」判定；
     - 本地非最新/缺失/损坏 -> 下载成功后把旧文件按 bak 命名惯例
       （stem+YYYYMMDD.zip，同名追加 _HHMMSS）移入 tdxData/bak/；
     - 所有下载源失败 -> 弹窗提示并终止（旧文件原地保留，不丢数据）。
     下载后校验：hsjday 须为合法 zip 且含 .day（顺带打印包内 sh000001 最新数据日）；
                gbbq 须含 gbbq 与 gbbq.map。
  2. 两个文件都正确且存在 -> 解压 hsjday.zip 到 tdxData（sh/sz/bj/lday/*.day，原地覆盖）。
  3. 调 runTDXQuanLiang.py 本地解析 .day 入库（不加 --online）。
  4. 调 runIndexSupplement.py 补全指数数据。
  5. 调 runTDXchuquan.py 生成前/后复权数据（增量）。

任一步失败：弹窗提示并停止后续步骤（已完成步骤的结果保留），退出码非 0。

用法：
  python runMainGetDatas.py
  python runMainGetDatas.py --supplement-args "--auto"   # 透传给 runIndexSupplement.py
"""
import argparse
import ctypes
import os
import shutil
import struct
import subprocess
import sys
import zipfile
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.request import Request, urlopen

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TDX_DIR = os.path.join(SCRIPT_DIR, "tdxData")
BAK_DIR = os.path.join(TDX_DIR, "bak")

HSJDAY = "hsjday.zip"
GBBQ = "gbbq.zip"

# 下载源（按顺序尝试）；已实测：前两个 hsjday/前两个 gbbq 源可用，IP 镜像仅作兜底
DOWNLOAD_URLS = {
    HSJDAY: [
        "https://data.tdx.com.cn/vipdoc/hsjday.zip",
        "http://data.tdx.com.cn/vipdoc/hsjday.zip",
    ],
    GBBQ: [
        "http://www.tdx.com.cn/products/data/data/dbf/gbbq.zip",
        "http://cnc.tdx.com.cn/dbf/gbbq.zip",
        "http://59.175.238.39/products/data/data/dbf/gbbq.zip",
        "http://210.51.55.211/dbf/gbbq.zip",
    ],
}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
REMOTE_SKEW_SEC = 60        # Last-Modified 比对容差（秒）
CHUNK = 1024 * 1024         # 下载分块 1MB


# ---------------- 提示与工具 ----------------
def alert(title: str, msg: str) -> None:
    """弹窗提示（Windows 原生 MessageBox；其他平台退化为终端输出）。"""
    print(f"[{title}] {msg}", flush=True)
    try:
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(0, msg, title, 0x10)   # MB_ICONERROR
    except Exception:
        pass


def _remote_last_modified(urls):
    """依次 HEAD 各下载源，返回 (Last-Modified epoch 秒, url)；全部失败返回 (None, None)。"""
    for url in urls:
        try:
            req = Request(url, headers=UA, method="HEAD")
            with urlopen(req, timeout=15) as r:
                lm = r.headers.get("Last-Modified")
                if lm:
                    return parsedate_to_datetime(lm).timestamp(), url
                return None, url          # 连通但无 Last-Modified 头
        except Exception:
            continue
    return None, None


def is_latest(path: str, urls) -> bool:
    """本地 zip 是否最新：mtime >= 远端 Last-Modified（容差 REMOTE_SKEW_SEC）；
    远端全部不可达时退化为「文件日期为今天」。"""
    if not os.path.exists(path):
        return False
    mt = os.path.getmtime(path)
    lm, _ = _remote_last_modified(urls)
    if lm is not None:
        return mt >= lm - REMOTE_SKEW_SEC
    return datetime.fromtimestamp(mt).date() == datetime.now().date()


def archive_old(path: str) -> str:
    """非最新原始数据归档：改名 stem+YYYYMMDD.zip 移入 tdxData/bak/（同名追加 _HHMMSS）。"""
    os.makedirs(BAK_DIR, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0]
    d = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y%m%d")
    dst = os.path.join(BAK_DIR, f"{stem}{d}.zip")
    if os.path.exists(dst):
        dst = os.path.join(BAK_DIR, f"{stem}{d}_{datetime.now():%H%M%S}.zip")
    shutil.move(path, dst)
    return dst


# ---------------- zip 校验 ----------------
def validate_hsjday(path: str):
    """合法 zip、含 .day 且 CRC 完整。返回 (ok, 描述)，顺带读包内 sh000001 最新数据日。"""
    try:
        with zipfile.ZipFile(path) as z:
            if z.testzip() is not None:
                return False, "zip CRC 校验失败（存在坏条目）"
            days = [n for n in z.namelist() if n.lower().endswith(".day")]
            if not days:
                return False, "zip 内无 .day 文件"
            extra = ""
            sh_idx = next((n for n in days if n.lower().endswith("sh000001.day")), None)
            if sh_idx:
                with z.open(sh_idx) as f:
                    data = f.read()
                if len(data) >= 32:      # .day 每条 32B，首 4B 为 uint32 YYYYMMDD
                    last = struct.unpack("<I", data[-32:-28])[0]
                    extra = f"，包内 sh000001 最新数据日 {last}"
            return True, f"{len(days)} 个 .day{extra}"
    except Exception as e:
        return False, f"zip 打开失败: {e}"


def validate_gbbq(path: str):
    """合法 zip、含 gbbq 与 gbbq.map 且 CRC 完整。返回 (ok, 描述)。"""
    try:
        with zipfile.ZipFile(path) as z:
            if z.testzip() is not None:
                return False, "zip CRC 校验失败（存在坏条目）"
            names = {os.path.basename(n).lower() for n in z.namelist()}
            if "gbbq" not in names or "gbbq.map" not in names:
                return False, f"zip 内缺少 gbbq/gbbq.map（实际条目: {sorted(names)[:8]}）"
            return True, "gbbq + gbbq.map 齐全"
    except Exception as e:
        return False, f"zip 打开失败: {e}"


VALIDATORS = {HSJDAY: validate_hsjday, GBBQ: validate_gbbq}


# ---------------- 检查 / 归档 / 下载 ----------------
def _download(url: str, dst_part: str) -> None:
    """流式下载到 dst_part（带简单进度）。失败抛异常。"""
    req = Request(url, headers=UA)
    done = 0
    next_log = 32 * CHUNK
    with urlopen(req, timeout=60) as r, open(dst_part, "wb") as f:
        while True:
            chunk = r.read(CHUNK)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if done >= next_log:
                next_log += 32 * CHUNK
                print(f"    已下载 {done / 1024 / 1024:.0f} MB ...", flush=True)


def ensure_zip(name: str):
    """确保 tdxData/name 为最新且校验通过。返回 (True, info) 或 (False, 错误信息)。

    顺序：先下载到 .part 并校验 -> 成功后才归档旧文件、就位（下载失败旧文件原地保留）。"""
    path = os.path.join(TDX_DIR, name)
    urls = DOWNLOAD_URLS[name]
    validate = VALIDATORS[name]

    # 1) 是否需要下载：缺失 / 损坏 / 非最新
    if not os.path.exists(path):
        print(f"  本地不存在 {name} -> 需要下载")
        need = True
    else:
        ok, info = validate(path)
        if not ok:
            print(f"  [warn] 本地 {name} 校验失败（{info}）-> 需要下载")
            need = True
        elif not is_latest(path, urls):
            print(f"  本地 {name} 非最新 -> 需要下载")
            need = True
        else:
            print(f"  [OK] {name} 已是最新，跳过下载（{info}）")
            return True, info

    # 2) 多源下载 + 校验（先下到 .part，成功才动旧文件）
    part = path + ".part"
    ok_new, info, last_err, t0 = False, "", None, None
    for url in urls:
        try:
            print(f"  下载 {name} <- {url} ...", flush=True)
            t0 = datetime.now()
            _download(url, part)
            ok, info = validate(part)
            if not ok:
                last_err = f"{url}: 校验失败（{info}）"
                print(f"    [warn] {last_err}")
                continue
            ok_new = True
            break
        except Exception as e:
            last_err = f"{url}: {e}"
            print(f"    [warn] 下载失败: {last_err}")
        finally:
            if not ok_new and os.path.exists(part):
                try:
                    os.remove(part)
                except OSError:
                    pass
    if not ok_new:
        alert(f"下载失败 - {name}",
              f"{name} 所有下载源均失败：\n{last_err}\n\n流程终止（原有文件未改动）。")
        return False, last_err

    # 3) 归档旧文件 -> 新文件就位
    if os.path.exists(path):
        bak = archive_old(path)
        print(f"  旧文件已归档: {bak}")
    os.replace(part, path)
    sec = (datetime.now() - t0).total_seconds() if t0 else 0
    print(f"  [OK] {name} 就绪（{info}，下载+校验耗时 {sec:.0f}s）")
    return True, info


# ---------------- 解压与子流程 ----------------
def extract_hsjday() -> None:
    """解压 hsjday.zip 到 tdxData（内部结构 sh/sz/bj/lday/*.day，原地覆盖）。"""
    path = os.path.join(TDX_DIR, HSJDAY)
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():                     # 防路径穿越
            norm = n.replace("\\", "/")
            if norm.startswith("/") or ".." in norm.split("/"):
                raise RuntimeError(f"zip 内含不安全路径: {n}")
        print(f"  解压 {HSJDAY} -> {TDX_DIR} ...", flush=True)
        t0 = datetime.now()
        z.extractall(TDX_DIR)
    print(f"  解压完成（{(datetime.now() - t0).total_seconds():.0f}s）")


def run_step(script: str, args=None) -> bool:
    """以当前解释器运行同目录脚本（输出直通控制台）；失败弹窗并返回 False。"""
    full = os.path.join(SCRIPT_DIR, script)
    cmd = [sys.executable, full] + list(args or [])
    print(f"\n>>> 运行 {script} {' '.join(args or [])}".rstrip(), flush=True)
    t0 = datetime.now()
    r = subprocess.run(cmd, cwd=SCRIPT_DIR)
    sec = (datetime.now() - t0).total_seconds()
    if r.returncode != 0:
        alert(f"步骤失败 - {script}",
              f"{script} 退出码 {r.returncode}（耗时 {sec:.0f}s）。\n流程终止，已完成步骤的结果保留。")
        return False
    print(f"<<< {script} 完成（{sec:.0f}s）")
    return True


# ---------------- 主流程 ----------------
def main() -> int:
    ap = argparse.ArgumentParser(description="每日数据更新主流程：下载 -> 解压 -> 入库 -> 补全 -> 复权")
    ap.add_argument("--supplement-args", default="",
                    help='透传给 runIndexSupplement.py 的参数（如 "--auto"）')
    args = ap.parse_args()

    t_all = datetime.now()
    print("=" * 66)
    print(f"每日数据更新 开始于 {t_all:%Y-%m-%d %H:%M:%S}")
    print(f"脚本目录: {SCRIPT_DIR}")
    print(f"数据目录: {TDX_DIR}")

    os.makedirs(TDX_DIR, exist_ok=True)
    os.makedirs(BAK_DIR, exist_ok=True)

    # 第 1 步：两个 zip 最新且正确
    print("\n第 1 步：检查/下载 hsjday.zip 与 gbbq.zip ...")
    for name in (HSJDAY, GBBQ):
        print(f"- {name}")
        ok, _info = ensure_zip(name)
        if not ok:                       # 弹窗已在 ensure_zip 内弹出
            return 1

    # 第 2 步：解压 hsjday.zip
    print("\n第 2 步：解压 hsjday.zip ...")
    try:
        extract_hsjday()
    except Exception as e:
        alert("解压失败", f"hsjday.zip 解压失败：{e}\n流程终止。")
        return 1

    # 第 3 步：本地数据解析入库
    if not run_step("runTDXQuanLiang.py"):
        return 1

    # 第 4 步：指数数据补全
    supp_args = args.supplement_args.split() if args.supplement_args else []
    if not run_step("runIndexSupplement.py", supp_args):
        return 1

    # 第 5 步：除权处理（前/后复权，增量）
    if not run_step("runTDXchuquan.py"):
        return 1

    print("\n" + "=" * 66)
    print(f"全部完成，总耗时 {(datetime.now() - t_all).total_seconds():.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
