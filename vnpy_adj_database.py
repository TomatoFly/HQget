# -*- coding: utf-8 -*-
"""
VNPY 复权数据读写扩展（方案 A）

- 不复权数据仍存于原表 dbbardata（VNPY 默认，永不改动）。
- 前复权 / 后复权数据分别存于 dbbardata_fwd / dbbardata_bwd（独立表、独立主键，绝不触碰 dbbardata）。
- AdjustedSqliteDatabase 重写 load_bar_data：
    mode='none'    -> 调父类原逻辑（与 VNPY 行为完全一致，原回测结果不变）
    mode='forward' -> 读 dbbardata_fwd
    mode='backward'-> 读 dbbardata_bwd
- 仅对日线(interval='d')生效，分钟线原样返回不复权。

使用（在回测脚本最前面、任何 get_database() 之前）：
    from vnpy_adj_database import use_adjusted_database
    use_adjusted_database(mode="forward")   # 之后正常 import vnpy / 启动回测
"""
from datetime import datetime

import vnpy.trader.database as db_mod
from vnpy.trader.database import DB_TZ
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData
from peewee import (
    CharField, DateTimeField, FloatField, IntegerField, Model, TextField,
)

# 复用 vnpy_sqlite 的同一个 peewee 连接（指向 SETTINGS 配置的 database.db）
from vnpy_sqlite.sqlite_database import (
    db, DbBarData, DbBarOverview, DbTickData, DbTickOverview, SqliteDatabase,
)


# ---------- 复权模式（全局，回测前设定） ----------
_ADJ = {"mode": "none"}


def set_adjustment_mode(mode: str) -> None:
    """设置复权模式: 'none' / 'forward' / 'backward'"""
    assert mode in ("none", "forward", "backward"), f"未知复权模式: {mode}"
    _ADJ["mode"] = mode


def get_adjustment_mode() -> str:
    return _ADJ["mode"]


# ---------- 独立复权表模型（绑定同一 sqlite 连接） ----------
class _DbBarDataAdj(Model):
    """复权 K 线表基类（与 dbbardata 字段一致，独立表名）"""
    symbol = CharField()
    exchange = CharField()
    datetime = DateTimeField()
    interval = CharField()
    volume = FloatField()
    turnover = FloatField()
    open_interest = FloatField()
    open_price = FloatField()
    high_price = FloatField()
    low_price = FloatField()
    close_price = FloatField()

    class Meta:
        database = db
        indexes = ((("symbol", "exchange", "interval", "datetime"), True),)


class DbBarDataFwd(_DbBarDataAdj):
    """前复权 K 线表（表名显式指定为 dbbardata_fwd，避免 peewee 按类名生成 dbbardatafwd）"""

    class Meta:
        table_name = "dbbardata_fwd"
        indexes = ((("symbol", "exchange", "interval", "datetime"), True),)


class DbBarDataBwd(_DbBarDataAdj):
    """后复权 K 线表（表名显式指定为 dbbardata_bwd）"""

    class Meta:
        table_name = "dbbardata_bwd"
        indexes = ((("symbol", "exchange", "interval", "datetime"), True),)


def ensure_adj_tables() -> None:
    """确保两张复权表存在（safe=True: 已存在则跳过；仅建表，不修改 dbbardata）。
    连接已打开时不再重复 connect，避免 peewee 'Connection already opened'。"""
    if db.is_closed():
        db.connect()
    db.create_tables([DbBarDataFwd, DbBarDataBwd], safe=True)


class DbCqInfo(Model):
    """除权信息快照表（增量复权计算的水位记录，表名 cqInfo）。

    每只股票一行，记录上次成功计算复权数据时的状态：
      - events:     上次所用权息事件列表（JSON，含算好的单事件因子 f）
                    [{date, fh, sg, zg, pg, pgj, f}, ...] 按日期升序
      - last_bar_dt: 上次处理时 dbbardata 该股日线的最大 datetime
      - bar_count:   上次处理时 dbbardata 该股日线的行数
    下次运行时与最新 gbbq / dbbardata 比对，决定 跳过 / 增量 / 全量重建。
    """
    symbol = CharField()
    exchange = CharField()
    events = TextField()
    last_bar_dt = DateTimeField()
    bar_count = IntegerField()
    updated_at = DateTimeField(default=datetime.now)

    class Meta:
        database = db
        table_name = "cqInfo"
        indexes = ((("symbol", "exchange"), True),)


def ensure_cqinfo_table() -> None:
    """确保 cqInfo 表存在（safe=True: 已存在则跳过）。"""
    if db.is_closed():
        db.connect()
    db.create_tables([DbCqInfo], safe=True)


# ---------- 自定义 Database ----------
class AdjustedSqliteDatabase(SqliteDatabase):
    """在原生 SqliteDatabase 上扩展复权读取能力，默认 none 时行为不变。"""

    def __init__(self) -> None:
        # 不调用 super().__init__()（其会再次 connect 报已连接）；自行安全建表
        self.db = db
        if db.is_closed():
            db.connect()
        db.create_tables(
            [DbBarData, DbTickData, DbBarOverview, DbTickOverview,
             DbBarDataFwd, DbBarDataBwd],
            safe=True,
        )

    @staticmethod
    def _rows_to_bars(model, symbol, exchange, interval, start, end):
        s = (
            model.select()
            .where(
                (model.symbol == symbol)
                & (model.exchange == exchange.value)
                & (model.interval == interval.value)
                & (model.datetime >= start)
                & (model.datetime <= end)
            )
            .order_by(model.datetime)
        )
        bars = []
        for d in s:
            bar = BarData(
                symbol=d.symbol,
                exchange=Exchange(d.exchange),
                datetime=datetime.fromtimestamp(d.datetime.timestamp(), DB_TZ),
                interval=Interval(d.interval),
                volume=d.volume,
                turnover=d.turnover,
                open_interest=d.open_interest,
                open_price=d.open_price,
                high_price=d.high_price,
                low_price=d.low_price,
                close_price=d.close_price,
                gateway_name="DB",
            )
            bars.append(bar)
        return bars

    def load_bar_data(self, symbol, exchange, interval, start, end):
        mode = _ADJ["mode"]
        # 不复权 或 非日线：完全沿用 VNPY 原逻辑，保证原回测数据不变
        if mode == "none" or interval.value != "d":
            return super().load_bar_data(symbol, exchange, interval, start, end)
        model = DbBarDataFwd if mode == "forward" else DbBarDataBwd
        return self._rows_to_bars(model, symbol, exchange, interval, start, end)


def use_adjusted_database(mode: str = "none"):
    """在 get_database() 之前调用，替换 VNPY 数据库单例为复权版本。"""
    set_adjustment_mode(mode)
    db_mod.database = AdjustedSqliteDatabase()
    return db_mod.database
