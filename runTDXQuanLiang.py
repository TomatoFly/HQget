import argparse
import os
from datetime import datetime

import TDXdataRead as TdxRead
import VNPYdataWriter as VNPYdataWriter
from vnpy.trader.object import BarData

# 数据源控制开关：
#   True  = 通过网络(pytdx)获取数据写库；
#   False = 只从本地 .day 文件解析数据写库（默认）。
ONLINE_FETCH = False

# 全量数据下载和写入数据库的主函数
# 先手动下载全量数据包：https://data.tdx.com.cn/vipdoc/hsjday.zip，放在 runTDX.py 所在目录下,
# 并解压到 D:\HQdata 目录下（解压出 sh/sz/bj/lday/*.day）
# 再配置数据库信息：VNPYdataWriter.py 下的 SETTINGS，driver 可以是 mysql 或 sqlite 等
# 然后运行该函数：
#   python runTDXQuanLiang.py              # 只从文件解析写库（OFFLINE）
#   python runTDXQuanLiang.py --online     # 通过网络(pytdx)获取数据写库
def main():
    ap = argparse.ArgumentParser(description="全量行情导入：本地 .day 解析 或 网络获取（开关控制）")
    ap.add_argument("--online", action="store_true", default=ONLINE_FETCH,
                    help="通过网络(pytdx)获取数据；不加此开关则只从本地 .day 文件解析写库")
    ap.add_argument("--base", default=r"D:\HQdata",
                    help="本地 .day 文件所在根目录（在线模式下仅作为符号清单来源）")
    ap.add_argument("--start", default="1990-01-01",
                    help="在线模式取数起始日 YYYY-MM-DD（默认全历史）")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"),
                    help="在线模式取数结束日 YYYY-MM-DD（默认今天）")
    args = ap.parse_args()

    online = args.online
    vnpWrite = VNPYdataWriter.VNPYdataWriter()
    base_path = args.base
    day_files = []
    t1 = datetime.now()
    for root, dirs, files in os.walk(base_path):
        for file in files:
            if file.endswith('.day'):
                day_files.append(os.path.join(root, file))

    mode = "网络获取(pytdx)" if online else "本地文件解析"
    print(f"数据源: {mode} | 在线取数区间 {args.start} ~ {args.end}", flush=True)
    print(f"共发现 {len(day_files)} 个 .day 文件"
          + ("（在线模式仅作为符号清单来源）" if online else ""), flush=True)

    i = 0
    for file_path in day_files:
        t11 = datetime.now()
        i += 1
        file_name = os.path.basename(file_path)

        print(f"\n正在处理: {file_name}", flush=True)

        if online:
            # 本地文件名作为符号清单：sh600519.day -> sh600519
            symbol = os.path.splitext(file_name)[0].lower()
            stock_data = TdxRead.read_tdx_day_online(symbol, args.start, args.end)
        else:
            stock_data = TdxRead.read_tdx_day_file(file_path)

        if not stock_data:
            print(f"  未读取到数据，跳过", flush=True)
            continue

        print(f"  读取到 {len(stock_data)} 条记录", flush=True)

        bars: list[BarData] = vnpWrite.convertToVNPYbarData(stock_data)
        vnpWrite.write_tdx_day(bars)
        t12 = datetime.now()
        print(f"  {i}/{len(day_files)}已写入数据库,耗时: {t12-t11}", flush=True)

    t2 = datetime.now()
    print(f"  总共耗时: {t2-t1}", flush=True)


if __name__ == "__main__":
    main()
