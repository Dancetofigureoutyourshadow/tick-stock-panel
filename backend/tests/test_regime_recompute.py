from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import regime
from app.services import market_mainline, preferences, regime_builder


@pytest.fixture
def recompute_client(tmp_path, monkeypatch):
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_enriched_range=lambda *args: None,
        get_instruments=lambda: pl.DataFrame(),
        get_historical_shares=lambda: pl.DataFrame(),
        get_index_daily=lambda *args, **kwargs: pl.DataFrame(),
    )
    monkeypatch.setattr(preferences, 'get_sentiment_exclude_st', lambda: False)
    monkeypatch.setattr(preferences, 'get_regime_batch_days', lambda: 60)
    monkeypatch.setattr(preferences, 'get_regime_warmup_days', lambda: 40)
    monkeypatch.setattr(market_mainline, 'compute_mainline_range', lambda *args, **kwargs: pl.DataFrame())
    app = FastAPI()
    app.state.repo = repo
    app.include_router(regime.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _write_partition(root, day, *, legacy=False, integer_volume=False):
    path = root / 'kline_daily_enriched' / f'date={day}' / 'part.parquet'
    path.parent.mkdir(parents=True)
    close = 10.0 if legacy else 11.0
    data = {
        'symbol': ['600000.SH'], 'date': [day],
        'open': [close], 'high': [close], 'low': [close], 'close': [close],
        'raw_close': [close], 'raw_high': [close], 'raw_low': [close],
        'volume': [1000] if integer_volume else [1000.0], 'amount': [10000.0],
        'turnover_rate': [1.0],
        'consecutive_limit_ups': pl.Series([0], dtype=pl.UInt32),
        'consecutive_limit_downs': pl.Series([0], dtype=pl.UInt32),
    }
    if legacy:
        data['prev_close'] = [99.0]
    else:
        data['quote_ts'] = [1792022400000]
    pl.DataFrame(data).write_parquet(path)
    return path


@pytest.mark.parametrize('integer_volume', [False, True])
def test_recompute_extends_coverage_across_partition_schema_changes(tmp_path, recompute_client, integer_volume):
    old_day, new_day = date(2026, 9, 24), date(2026, 9, 28)
    _write_partition(tmp_path, old_day, legacy=True)
    _write_partition(tmp_path, new_day, integer_volume=integer_volume)
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        'date': [old_day], 'state': ['range'], 'score': [50],
    }))
    response = recompute_client.post('/api/regime/recompute', params={'end': str(new_day)})
    assert response.status_code == 200, response.text
    assert response.json()['computed'] == 2
    assert recompute_client.get('/api/regime/coverage').json() == {
        'rows': 2, 'earliest_date': str(old_day), 'latest_date': str(new_day),
    }
    latest = regime_builder.load_regime_history(tmp_path).filter(pl.col('date') == new_day)
    assert latest['up_count'].item() == 1
    assert latest['avg_pct'].item() == pytest.approx(0.1)


def test_recompute_read_failure_is_not_reported_as_success(tmp_path, recompute_client):
    day = date(2026, 9, 28)
    path = _write_partition(tmp_path, day)
    path.write_bytes(b'broken parquet')
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        'date': [date(2026, 9, 24)], 'state': ['range'], 'score': [50],
    }))
    old_bytes = regime_builder.regime_path(tmp_path).read_bytes()
    response = recompute_client.post('/api/regime/recompute', params={'end': str(day)})
    assert response.status_code == 500
    assert '计算失败' in response.json()['detail']
    assert regime_builder.regime_path(tmp_path).read_bytes() == old_bytes


def test_recompute_without_partitions_returns_zero(recompute_client):
    response = recompute_client.post('/api/regime/recompute')
    assert response.status_code == 200
    assert response.json() == {'ok': True, 'computed': 0}
