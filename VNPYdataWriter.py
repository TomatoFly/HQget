import pandas as pd
import vnpy.trader.database as db
from vnpy.trader.setting import SETTINGS
from vnpy.trader.database import DB_TZ, database
from vnpy.trader.object import BarData
from vnpy.trader.database import BarOverview
from vnpy.trader.constant import Exchange, Interval
from csv import DictReader
from datetime import datetime, timedelta
class VNPYdataWriter:
    def __init__(self):
        self.overviews: list[BarOverview] = []
        SETTINGS["database.timezone"] = "Asia/Shanghai"
        SETTINGS["database.driver"] = "sqlite"#"mysql"
        SETTINGS["database.database"] = "database.db"
        SETTINGS["database.host"] = ""
        SETTINGS["database.port"] = 0
        SETTINGS["database.user"] = ""
        SETTINGS["database.password"] = ""
        SETTINGS["database.name"] = "sqlite"

    def convertToVNPYbarData(self, tdx_data, interval: Interval=Interval.DAILY):
        bar_list = []
        for item in tdx_data:
            bar = BarData(
                gateway_name="DB",
                symbol=item['symbol'],
                exchange=Exchange(item['exchange']),
                datetime=datetime.strptime(item['date'], "%Y-%m-%d"),
                open_price=item['open'],
                high_price=item['high'],
                low_price=item['low'],
                close_price=item['close'],
                volume=item['volume'],
                turnover=item['amount'],
                open_interest=0,
                interval=interval
            )
            bar_list.append(bar)
        return bar_list

    def write_tdx_day(self, bars: list[BarData], overwrite: bool = False):
        if overwrite:
            print(f"覆盖模式：正在写入 {len(bars)} 条记录到数据库")
            db.get_database().save_bar_data(bars)
        else:
            if not self.overviews:
                self.overviews = db.get_database().get_bar_overview()

            overview_map = {}
            for ov in self.overviews:
                key = (ov.symbol, ov.exchange.value, ov.interval.value)
                overview_map[key] = ov

            newbars: list[BarData] = []
            for bar in bars:
                key = (bar.symbol, bar.exchange.value, bar.interval.value)
                ov = overview_map.get(key)

                if ov is None:
                    newbars.append(bar)
                elif bar.datetime < ov.start or bar.datetime > ov.end:
                    newbars.append(bar)

            if len(newbars) > 0:
                print(f"增量模式：新增 {len(newbars)} 条记录（原始 {len(bars)} 条，跳过 {len(bars) - len(newbars)} 条已有数据）")
                db.get_database().save_bar_data(newbars)
            else:
                print(f"增量模式：无新增数据（原始 {len(bars)} 条均已存在）")



    def getOverViewsUpdataInfo(self):
        overviews = db.get_database().get_bar_overview()
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        result = []
        for ov in overviews:
            new_ov = BarOverview(
                symbol=ov.symbol,
                exchange=ov.exchange,
                interval=ov.interval,
                count=ov.count,
                start=ov.end + timedelta(days=1) if ov.end else None,
                end=today
            )
            result.append(new_ov)

        return result