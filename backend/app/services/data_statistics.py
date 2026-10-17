"""Bounded parquet statistics for the data status read model."""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import tempfile
from contextlib import suppress
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq

logger = logging.getLogger(__name__)


def _read_cache(path: Path, *, compressed: bool = False) -> dict:
    try:
        opener = gzip.open if compressed else open
        with opener(path, "rt", encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError, EOFError):
        return {}


def _write_cache(path: Path, value: dict, *, compressed: bool = False) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
        opener = gzip.open if compressed else open
        with opener(temporary, "wt", encoding="utf-8") as stream:
            json.dump(value, stream)
        os.replace(temporary, path)
    except OSError:
        pass
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _file_stats(path: Path, root: Path) -> dict:
    symbols, dates = set(), set()
    partition_date = next((
        parent.name[5:] for parent in path.relative_to(root).parents
        if parent.name.startswith("date=")
    ), None)
    if partition_date:
        partition_date = date.fromisoformat(partition_date).isoformat()
    with pq.ParquetFile(path) as source:
        rows = source.metadata.num_rows
        if rows:
            columns = ["symbol"] if partition_date else ["symbol", "date"]
            if not set(columns).issubset(source.schema_arrow.names):
                raise ValueError("kline statistics require symbol/date columns")
            if partition_date:
                dates.add(partition_date)
            for batch in source.iter_batches(batch_size=8192, columns=columns, use_threads=False):
                symbols.update(str(value) for value in batch.column("symbol").unique().to_pylist() if value is not None)
                if not partition_date:
                    dates.update(str(value)[:10] for value in batch.column("date").unique().to_pylist() if value is not None)
    # 以字符串保存去重集合，避免缓存中为每个历史分区保留重复的字符串对象列表。
    return {"rows": rows, "symbols": "\n".join(sorted(symbols)), "dates": "\n".join(sorted(dates))}


def parquet_kline_stats(root: Path) -> dict | None:
    """只读取代码/日期列，每批最多 8192 行，不绑定全部宽表 schema。"""
    rows = 0
    symbols: set[str] = set()
    dates: set[str] = set()
    try:
        files = sorted(root.rglob("*.parquet"))
        if not files:
            return None
        fingerprint = hashlib.sha256()
        stamps = {}
        for path in files:
            stat = path.stat()
            name = path.relative_to(root).as_posix()
            stamps[name] = [stat.st_size, stat.st_mtime_ns]
            fingerprint.update(json.dumps([name, *stamps[name]]).encode())
        signature = fingerprint.hexdigest()
        cache_path = root.parent / ".data_statistics" / f"{root.name}.json"
        cached = _read_cache(cache_path)
        stats = cached.get("stats")
        if cached.get("version") == 2 and cached.get("signature") == signature and isinstance(stats, dict) and {"rows", "earliest_date", "latest_date", "symbols_covered", "trading_days"} <= stats.keys():
            return stats
        records_path = cache_path.with_suffix(".files.json.gz")
        old_records = _read_cache(records_path, compressed=True)
        records = {}
        for path in files:
            name = path.relative_to(root).as_posix()
            record = old_records.get(name)
            if (
                not isinstance(record, dict)
                or record.get("stamp") != stamps[name]
                or not isinstance(record.get("rows"), int)
                or not isinstance(record.get("symbols"), str)
                or not isinstance(record.get("dates"), str)
            ):
                record = {**_file_stats(path, root), "stamp": stamps[name]}
            records[name] = record
            rows += record["rows"]
            symbols.update(record["symbols"].splitlines())
            dates.update(record["dates"].splitlines())
    except Exception:  # noqa: BLE001
        logger.debug("parquet kline statistics unavailable for %s", root.name, exc_info=True)
        return None
    if not rows:
        return None
    stats = {
        "rows": rows,
        "earliest_date": min(dates) if dates else None,
        "latest_date": max(dates) if dates else None,
        "symbols_covered": len(symbols),
        "trading_days": len(dates),
    }
    # 当前分区变化时只重算该文件，历史分区沿用经大小/纳秒 mtime 校验的统计。
    _write_cache(records_path, records, compressed=True)
    _write_cache(cache_path, {"version": 2, "signature": signature, "stats": stats})
    return stats
