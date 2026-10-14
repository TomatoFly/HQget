import TDXdataRead as TdxRead
import VNPYdataWriter as VnpWrite
from datetime import datetime
# 从TDX获取股票数据并写入数据库
#通过API获取数据的增量模式，不稳定，有时候会连接不上
def main():
    #test_read_tdx_day_online()
    #test_compare_online_with_local()
    #test_getOverViewsUpdataInfo()
    t1 = datetime.now()
    vnpWrite = VnpWrite.VNPYdataWriter()
    overviews = vnpWrite.getOverViewsUpdataInfo()
    print(f"共获取到 {len(overviews)} 条更新信息")
    i=0
    for ov in overviews:
        i+=1
        t11 = datetime.now()
        if ov.interval.value != "d":
            continue
        symbol=TdxRead.convertSymbolVNPY2TDX(ov.exchange.value)+ov.symbol
        start_date=ov.start.strftime("%Y-%m-%d")
        end_date=ov.end.strftime("%Y-%m-%d")
        stock_data = TdxRead.read_tdx_day_online(symbol, start_date, end_date)
        if not stock_data:
            print(f"未获取到 {symbol} {start_date} ~ {end_date} 数据")
            continue
        print(f"  读取 {symbol} {start_date} ~ {end_date} 共 {len(stock_data)} 条数据")        
        bars = vnpWrite.convertToVNPYbarData(stock_data)
        vnpWrite.write_tdx_day(bars)        
        t12 = datetime.now()
        print(f" {i}/{len(overviews)} 已写入数据库,耗时: {t12-t11}")   
    t2 = datetime.now()
    print(f"  总共耗时: {t2-t1}")    


def test_read_tdx_day_online():
    print("=" * 80)
    print("测试: read_tdx_day_online 函数测试")
    print("=" * 80)

    symbol = "sh600511"
    start_date = "2026-05-14"
    end_date = "2026-05-15"

    print(f"\n测试股票: {symbol}")
    print(f"日期范围: {start_date} ~ {end_date}")
    print("-" * 80)

    data = TdxRead.read_tdx_day_online(symbol, start_date, end_date)

    if not data:
        print("未获取到数据")
        return

    print(f"\n共获取到 {len(data)} 条K线数�?")
    print("-" * 80)
    print(f"{'日期':<12} {'开盘':<10} {'最高':<10} {'最低':<10} {'收盘':<10} {'成交金额':<15} {'成交量':<15}")
    print("-" * 80)

    for item in data:
        print(f"{item['date']:<12} {item['open']:<10} {item['high']:<10} {item['low']:<10} {item['close']:<10} {item['amount']:<15} {item['volume']:<15}")

    print("=" * 80)
    print(f"最新一条: {data[0]['date']} ~ 最早一条: {data[-1]['date']}")
    print("测试完成!")


def test_compare_online_with_local():
    print("\n" + "=" * 80)
    print("测试: 在线数据 vs 本地.day文件数据 对比")
    print("=" * 80)

    symbol = "sz000001"
    start_date = "2026-05-01"
    end_date = "2026-05-15"

    print(f"\n对比股票: {symbol}")
    print(f"日期范围: {start_date} ~ {end_date}")

    file_path = r"D:\HQdata\hsjday\sz\lday\sz000001.day"

    local_data = TdxRead.read_tdx_day_file(file_path)

    local_filtered = [
        item for item in local_data if start_date <= item['date'] <= end_date]

    online_data = TdxRead.read_tdx_day_online(symbol, start_date, end_date)

    print(f"\n本地数据: {len(local_filtered)} 条记录")
    print(f"在线数据: {len(online_data)} 条记录")
    print("-" * 80)

    len_min = min(len(local_filtered), len(online_data))

    print(f"{'本地日期':<12} {'本地开盘':<12} {'在线开盘':<12} {'对比结果':<10}")
    print("-" * 80)

    for i in range(len_min):
        local_item = local_filtered[-(i + 1)]
        online_item = online_data[i] if i < len(online_data) else None

        if online_item:
            match = "匹配" if abs(local_item['open'] - online_item['open']) < 0.01 else "不匹配"
            print(f"{local_item['date']:<12} {local_item['open']:<12} {online_item['open']:<12} {match}")
        else:
            print(f"{local_item['date']:<12} {local_item['open']:<12} {'无在线数据':<12} -")

    print("=" * 80)
    print("对比测试完成!")


def test_getOverViewsUpdataInfo():
    print("\n" + "=" * 80)
    print("测试�?: getOverViewsUpdataInfo 函数测试")
    print("=" * 80)

    overviews = VnpWrite.getOverViewsUpdataInfo()

    if not overviews:
        print("数据库无概要信息 ，请先导入数据范围")
        return

    print(f"\n获取到 {len(overviews)} 条股票更新范围：")
    print("-" * 80)
    print(f"{'合约代码':<12} {'交易所':<10} {'周期':<10} {'K线条数':<10} {'新开始日':<15} {'结束日期':<15}")
    print("-" * 80)

    for ov in overviews:
        start_str = ov.start.strftime("%Y-%m-%d") if ov.start else "None"
        end_str = ov.end.strftime("%Y-%m-%d") if ov.end else "None"
        print(f"{ov.symbol:<12} {str(ov.exchange):<10} {str(ov.interval):<10} {ov.count:<10} {start_str:<15} {end_str:<15}")

    print("=" * 80)
    print("getOverViewsUpdataInfo 测试完成!")


if __name__ == "__main__":
    main()