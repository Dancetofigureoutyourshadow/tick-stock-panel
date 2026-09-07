"""分钟 K 按日期精确读取的路径兼容性测试。"""
from datetime import date, datetime

import polars as pl
import pytest

from app.tickflow.repository import DataStore, KlineRepository


@pytest.mark.parametrize("separator", ["/", "\\"])
@pytest.mark.parametrize(
    ("asset_type", "dirname", "glob_attr"),
    [
        ("stock", "kline_minute", "_minute_glob"),
        ("etf", "kline_etf_minute", "_etf_minute_glob"),
    ],
)
def test_get_minute_by_dates_is_independent_of_glob_separator(
    tmp_path,
    separator,
    asset_type,
    dirname,
    glob_attr,
):
    repo = KlineRepository(DataStore(tmp_path))
    part_dir = tmp_path / dirname / "date=2026-09-04"
    part_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["000565.SZ", "000565.SZ"],
        "datetime": [
            datetime(2026, 9, 4, 9, 30),
            datetime(2026, 9, 4, 14, 59),
        ],
        "close": [6.30, 6.33],
    }).write_parquet(part_dir / "part.parquet")

    # 模拟 POSIX 与 Windows 风格；查询应从 data_dir 构造路径，而不解析 glob。
    normalized_glob = getattr(repo, glob_attr).replace("\\", "/")
    setattr(repo, glob_attr, normalized_glob.replace("/", separator))

    result = repo.get_minute_by_dates(
        ["000565.SZ"],
        [date(2026, 9, 4)],
        asset_type=asset_type,
    )

    assert result.height == 2
    assert result["close"].to_list() == [6.30, 6.33]
