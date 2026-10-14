import struct
import os
import re
from datetime import datetime
from vnpy.trader.object import BarData

def read_tdx_day_file(file_path):
    data = []
    record_size = 32
    exchange = ""
    
    if not os.path.exists(file_path):
        print(f"文件不存在: {file_path}")
        return data
    filename = os.path.basename(file_path)
    filename2=filename.lower()
    if "sh" in filename2:
        exchange = "SSE"
    elif "sz" in filename2:
        exchange = "SZSE"
    elif "bj" in filename2:
        exchange = "BSE"
    else:
        print(f"文件路径中未包含有效交易所代码: {file_path}")
        return data
    # 代码=前缀(sh/sz/bj)与 .day 之间的字母数字段。
    # 不用 extract_number()（只取第一段纯数字）：含字母的代码（如 sz200b07.day）
    # 会被截成 '200'，写库后 symbol 错误且与其它代码潜在冲突。
    m = re.match(r'^(?:sh|sz|bj)([0-9a-z]+)\.day$', filename2)
    symbol = m.group(1) if m else extract_number(filename2)
    with open(file_path, 'rb') as f:
        while True:
            record = f.read(record_size)
            if len(record) < record_size:
                break

            date_raw = struct.unpack('<I', record[0:4])[0]
            open_raw = struct.unpack('<I', record[4:8])[0]
            high_raw = struct.unpack('<I', record[8:12])[0]
            low_raw = struct.unpack('<I', record[12:16])[0]
            close_raw = struct.unpack('<I', record[16:20])[0]
            amount = struct.unpack('<f', record[20:24])[0]#成交额元
            volume = struct.unpack('<I', record[24:28])[0]#量手

            year = date_raw // 10000
            month = (date_raw % 10000) // 100
            day = date_raw % 100

            if year < 1990 or year > 2050:
                continue

            data.append({
                'date': f"{year}-{month:02d}-{day:02d}",
                'exchange': exchange,
                'symbol': symbol,
                'open': open_raw / 100.0,
                'high': high_raw / 100.0,
                'low': low_raw / 100.0,
                'close': close_raw / 100.0,
                'amount': amount,
                'volume': volume
            })

    return data

def convertSymbolVNPY2TDX(symbol: str) -> str:
    exchange=""   
    if "SSE" == symbol:
        exchange = "sh"
    elif "SZSE" == symbol:
        exchange = "sz"
    elif "BSE" == symbol:
        exchange = "bj"
    return exchange

def extract_number(s: str) -> str:
    match = re.search(r'\d+', s)
    return match.group() if match else ""

def get_file_path(symbol: str) -> str:
    base_path = r"D:\HQdata\hsjday"

    symbol_lower = symbol.lower()
    if symbol_lower.startswith('sh'):
        exchange_prefix = 'sh'
        symbol_code = extract_number(symbol_lower)
    elif symbol_lower.startswith('sz'):
        exchange_prefix = 'sz'
        symbol_code = extract_number(symbol_lower)
    elif symbol_lower.startswith('bj'):
        exchange_prefix = 'bj'
        symbol_code = extract_number(symbol_lower)
    elif len(symbol) == 6:
        first_digit = symbol[0]
        if first_digit in ['6', '5']:
            exchange_prefix = 'sh'
        elif first_digit in ['0', '3', '1', '2']:
            exchange_prefix = 'sz'
        elif first_digit in ['4', '8', '9']:
            exchange_prefix = 'bj'
        else:
            exchange_prefix = 'sh'
        symbol_code = symbol
    else:
        exchange_prefix = 'sh'
        symbol_code = symbol

    file_path = os.path.join(base_path, exchange_prefix, 'lday', f"{exchange_prefix}{symbol_code}.day")
    return file_path

def get_market_and_code(symbol: str) -> tuple:
    symbol_lower = symbol.lower()
    if symbol_lower.startswith('sh'):
        return 1, extract_number(symbol_lower)
    elif symbol_lower.startswith('sz'):
        return 0, extract_number(symbol_lower)
    elif symbol_lower.startswith('bj'):
        return 2, extract_number(symbol_lower)
    elif len(symbol) == 6:
        first_digit = symbol[0]
        if first_digit in ['6', '5']:
            return 1, symbol
        else:
            return 0, symbol
    else:
        return 1, symbol

def is_index_symbol_tdx(symbol: str) -> bool:
    """带市场前缀的 TDX 符号（sh000985 / sz399001 / bj899050 …）是否为指数。

    与 adjust_factor_builder.is_index_symbol 的分段一致：SSE 000/88/999、
    SZSE 399、BSE 899。只认带前缀写法（6 位裸代码无法区分市场，如 000001
    在沪市是上证指数、在深市是平安银行）。
    """
    s = str(symbol).lower()
    if s.startswith("sh"):
        return s[2:].startswith(("000", "88", "999"))
    if s.startswith("sz"):
        return s[2:].startswith("399")
    if s.startswith("bj"):
        return s[2:].startswith("899")
    return False

def read_tdx_day_online(symbol: str, start_date: str, end_date: str) -> list:
    try:
        from pytdx.hq import TdxHq_API
    except ImportError:
        print("请先安装 pytdx: pip install pytdx")
        return []
    
    exchange=""
    if "sh" in symbol:
        exchange = "SSE"
    elif "sz" in symbol:
        exchange = "SZSE"
    elif "bj" in symbol:
        exchange = "BSE"
    else:
        print(f"文件路径中未包含有效交易所代码: {symbol}")
        return []
    
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    if(start_dt>=end_dt):
        #print(f": {start_dt}>={end_dt}")                 
        return [] 
    # 旧实现限制「起始年必须为今年」，导致任何停更超过一年的标的（如中证全指
    # 2016-06 停更）永远无法在线回补——已移除；翻页终止条件足以保证不会无限回溯。
    
    market, code = get_market_and_code(symbol)
    # 指数必须走 get_index_bars：get_security_bars 对指数代码恒返回 0 条
    # （这是此前在线通道始终补不了中证全指等指数的原因）。
    use_index_api = is_index_symbol_tdx(symbol)

    server_list = [
        ('124.71.163.106', 7709),
        ('119.147.212.81', 7709),
        ('221.231.141.60', 7709),
    ]

    api = TdxHq_API()
    all_data = []

    for server_ip, server_port in server_list:
        try:
            # pytdx 连接失败时 connect 返回 False（不抛异常），旧写法 with api.connect(...)
            # 会报 "'bool' object does not support the context manager protocol"。
            if not api.connect(server_ip, server_port, time_out=10):
                print(f"连接服务器 {server_ip}:{server_port} 失败，尝试下一个服务器")
                continue
            try:
                # pytdx 单次请求上限 800 条：超过会使 len(batch)<max_per_request
                # 提前终止而漏数据，故按 800 封顶翻页。
                max_per_request = min(800, (end_dt - start_dt).days + 1)
                offset = 0
               
                while True:
                    if use_index_api:
                        batch_data = api.get_index_bars(9, market, code, offset, max_per_request)
                    else:
                        batch_data = api.get_security_bars(9, market, code, offset, max_per_request)

                    if not batch_data:
                        break

                    batch_last_dt = datetime(batch_data[-1]['year'], batch_data[-1]['month'], batch_data[-1]['day'])
                    if batch_last_dt < start_dt:
                        break

                    for item in batch_data:
                        item_date_str = f"{item['year']}-{item['month']:02d}-{item['day']:02d}"
                        item_dt = datetime(item['year'], item['month'], item['day'])

                        if start_dt <= item_dt <= end_dt:
                            filtered_item = {
                                'date': item_date_str,
                                'exchange': exchange,
                                'symbol': code,
                                'open': item['open'],
                                'high': item['high'],
                                'low': item['low'],
                                'close': item['close'],
                                'amount': item['amount'],
                                'volume': item['vol']
                            }
                            all_data.append(filtered_item)

                    if len(batch_data) < max_per_request:
                        break

                    offset += max_per_request

                all_data.sort(key=lambda x: x['date'])
                break
            finally:
                api.disconnect()

        except Exception as e:
            print(f"连接服务器 {server_ip}:{server_port} 失败: {e}，尝试下一个服务器")
            continue

    return all_data