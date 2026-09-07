"""Run backtests sequentially and persist every response before the next run.

The runner deliberately never invents metrics. A failed request is persisted as
an error record, and a direction is emitted only when the compared metrics are
present in actual responses. The audit log contains each request and observed
metric summary; the response file retains the complete raw response.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_.-") or "run"


def _load_json(path: Path):
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _metrics(result: dict) -> dict[str, float]:
    candidates = [result, result.get("stats", {}) if isinstance(result, dict) else {}]
    found: dict[str, float] = {}
    for source in candidates:
        if not isinstance(source, dict):
            continue
        for key in ("calmar", "annual_return", "max_drawdown", "win_rate", "n_trades", "trades"):
            value = source.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                found[key] = float(value)
    return found


def _log_result(response: dict | None) -> dict:
    """Return only observed result fields for the line-oriented audit log."""
    if response is None:
        return {}
    stats = response.get("stats") if isinstance(response, dict) else None
    summary = _metrics(response)
    if isinstance(stats, dict):
        for key in ("strategy_info", "selection", "execution"):
            if key in stats:
                summary[key] = stats[key]
    return summary


def _direction(current: dict, baseline: dict | None) -> dict:
    if baseline is None:
        return {"status": "baseline", "recommendation": "record_baseline_only"}
    before = _metrics(baseline)
    after = _metrics(current)
    required = ("calmar", "max_drawdown")
    if any(key not in before or key not in after for key in required):
        return {
            "status": "unknown",
            "recommendation": "no_change_metrics_missing",
            "before": before,
            "after": after,
        }
    calmar_up = after["calmar"] > before["calmar"]
    drawdown_not_worse = abs(after["max_drawdown"]) <= abs(before["max_drawdown"])
    if calmar_up and drawdown_not_worse:
        recommendation = "advance_one_step"
        status = "positive"
    elif calmar_up:
        recommendation = "hold_and_review_drawdown"
        status = "mixed"
    else:
        recommendation = "rollback_or_hold"
        status = "negative"
    return {
        "status": status,
        "recommendation": recommendation,
        "before": before,
        "after": after,
        "delta": {key: after[key] - before[key] for key in required},
    }


def _post_json(url: str, payload: dict, timeout: int) -> tuple[dict | None, dict | None]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw), None
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return None, {"type": "http", "status": exc.code, "body": body}
    except (URLError, TimeoutError, OSError) as exc:
        return None, {"type": "transport", "message": str(exc)}
    except json.JSONDecodeError as exc:
        return None, {"type": "json", "message": str(exc)}


def run(config_path: Path, out_dir: Path, base_url: str, timeout: int, dry_run: bool) -> int:
    raw = _load_json(config_path)
    runs = raw.get("runs", raw) if isinstance(raw, dict) else raw
    if not isinstance(runs, list) or not runs:
        raise ValueError("config must contain a non-empty list or {\"runs\": [...]}")
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "backtest.log"
    central_log_path = (
        out_dir.parent / "backtest.log"
        if out_dir.parent.name == ".tmp-strategy-opt-v2"
        else None
    )
    baselines: dict[str, dict] = {}
    summary: list[dict] = []

    def write_log(event: dict) -> None:
        encoded = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        paths = [log_path]
        if central_log_path is not None and central_log_path != log_path:
            paths.append(central_log_path)
        for path in paths:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(encoded)

    for index, item in enumerate(runs, start=1):
        if not isinstance(item, dict) or not isinstance(item.get("request"), dict):
            raise ValueError(f"run {index} must contain a request object")
        name = str(item.get("name") or item["request"].get("strategy_id") or f"run-{index}")
        baseline_name = item.get("baseline_of")
        started = datetime.now(UTC).isoformat()
        event = {
            "index": index,
            "name": name,
            "baseline_of": str(baseline_name) if baseline_name else None,
            "started_at": started,
            "request": item["request"],
            "dry_run": dry_run,
        }
        if dry_run:
            response, error = None, {"type": "dry_run", "message": "request not sent"}
        else:
            response, error = _post_json(base_url.rstrip("/") + "/api/backtest/strategy/run", item["request"], timeout)
        event["finished_at"] = datetime.now(UTC).isoformat()
        event["response"] = response
        event["error"] = error
        direction = {"status": "not_computed", "recommendation": "no_change_without_result"}
        if response is not None:
            if baseline_name:
                baseline = baselines.get(str(baseline_name))
                if baseline is None:
                    direction = {
                        "status": "unknown",
                        "recommendation": "no_change_baseline_missing",
                        "baseline_of": str(baseline_name),
                    }
                else:
                    direction = _direction(response, baseline)
            else:
                direction = _direction(response, None)
            baselines[name] = response
        event["direction"] = direction
        result_path = out_dir / f"{index:03d}-{_slug(name)}.json"
        with result_path.open("w", encoding="utf-8") as stream:
            json.dump(event, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        write_log({
            "index": index,
            "file": result_path.name,
            "name": name,
            "baseline_of": str(baseline_name) if baseline_name else None,
            "started_at": started,
            "finished_at": event["finished_at"],
            "request": item["request"],
            "response_file": result_path.name,
            "response_status": "ok" if response is not None else "error",
            "result": _log_result(response),
            "error": error,
            "direction": direction,
        })
        print(json.dumps({
            "name": name,
            "result": _log_result(response),
            "direction": direction,
            "error": error,
        }, ensure_ascii=False, sort_keys=True), flush=True)
        summary.append({"name": name, "file": result_path.name, "direction": direction, "error": error})
        time.sleep(0.05)

    with (out_dir / "summary.json").open("w", encoding="utf-8") as stream:
        json.dump({"config": str(config_path), "runs": summary}, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return 0 if all(item["error"] is None or item["error"].get("type") == "dry_run" for item in summary) else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path(".tmp-strategy-opt-v2"))
    parser.add_argument("--base-url", default="http://127.0.0.1:3018")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        return run(args.config, args.out_dir, args.base_url, args.timeout, args.dry_run)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"backtest runner error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
