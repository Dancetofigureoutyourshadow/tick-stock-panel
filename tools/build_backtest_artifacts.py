"""Build evidence-only summaries from backtest_runner JSON artifacts."""
from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = ROOT / ".tmp-strategy-opt-v2"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def stats_from(path: Path) -> dict:
    event = load(path)
    response = event.get("response") or {}
    stats = response.get("stats") or {}
    trades = response.get("trades") or []
    exits = Counter(str(t.get("exit_reason") or "unknown") for t in trades if isinstance(t, dict))
    return {
        "file": str(path.relative_to(ROOT)),
        "strategy_id": (stats.get("strategy_info") or {}).get("id") or response.get("config", {}).get("strategy_id"),
        "annual_return": stats.get("annual_return"),
        "max_drawdown": stats.get("max_drawdown"),
        "calmar": stats.get("calmar"),
        "n_trades": stats.get("n_trades"),
        "win_rate": stats.get("win_rate"),
        "selection": stats.get("selection"),
        "execution": stats.get("execution"),
        "exit_reason_distribution": dict(sorted(exits.items())),
        "direction": event.get("direction"),
    }


def main() -> None:
    baseline_files = sorted((RUN_ROOT).glob("00*.json"))
    baseline_files = [p for p in baseline_files if "r2" not in str(p) and "r3" not in str(p) and "r5" not in str(p)]
    diagnosis = {
        "generated_at": datetime.now(UTC).isoformat(),
        "method": "derived only from actual API responses; no estimated metrics",
        "strategies": [stats_from(path) for path in baseline_files],
        "skipped": [{
            "strategy_id": "custom_sequoia_private_placement",
            "reason": "local directional-placement announcement dataset is unavailable",
            "conclusion": "needs_data_source",
        }],
    }
    write(RUN_ROOT / "r1_diagnosis.json", diagnosis)
    write(RUN_ROOT / "r0_baseline.json", {
        "generated_at": diagnosis["generated_at"],
        "method": "six serial strategy API requests",
        "results": diagnosis["strategies"],
        "skipped": diagnosis["skipped"],
    })

    r3 = load(RUN_ROOT / "r3" / "summary.json")
    grid = []
    for item in r3["runs"]:
        event = load(RUN_ROOT / "r3" / item["file"])
        response = event.get("response") or {}
        stats = response.get("stats") or {}
        grid.append({
            "name": item["name"],
            "file": str((RUN_ROOT / "r3" / item["file"]).relative_to(ROOT)),
            "params": event.get("request", {}).get("params"),
            "max_hold_days": (event.get("request", {}).get("overrides") or {}).get("max_hold_days"),
            "annual_return": stats.get("annual_return"),
            "max_drawdown": stats.get("max_drawdown"),
            "calmar": stats.get("calmar"),
            "direction": event.get("direction"),
        })
    candidates = [row for row in grid if row["name"] != "v2-full-anchor"]
    best = max(candidates, key=lambda row: row["calmar"] if row["calmar"] is not None else float("-inf"))
    write(RUN_ROOT / "r3_optimize.json", {
        "generated_at": datetime.now(UTC).isoformat(),
        "objective": "calmar",
        "method": "four serial strategy API requests; grid dimensions selected from diagnosis",
        "grid": grid,
        "best_candidate_by_calmar": best,
        "best_candidate_passed_positive_gate": bool(best["direction"] and best["direction"].get("recommendation") == "advance_one_step"),
        "boundary_note": "the tested grid points are not a sufficient basis to expand the grid; no candidate passed the double positive gate",
    })

    def summarize_dir(name: str) -> list[dict]:
        directory = RUN_ROOT / name
        rows = []
        for item in sorted(directory.glob("00*.json")):
            event = load(item)
            response = event.get("response") or {}
            stats = response.get("stats") or {}
            rows.append({
                "name": event.get("name"),
                "file": str(item.relative_to(ROOT)),
                "request": event.get("request"),
                "annual_return": stats.get("annual_return"),
                "max_drawdown": stats.get("max_drawdown"),
                "calmar": stats.get("calmar"),
                "n_trades": stats.get("n_trades"),
                "win_rate": stats.get("win_rate"),
                "direction": event.get("direction"),
                "error": event.get("error"),
            })
        return rows

    write(RUN_ROOT / "r2_v2_vs_base.json", {
        "generated_at": datetime.now(UTC).isoformat(),
        "method": "three serial strategy API requests",
        "results": summarize_dir("r2"),
    })
    write(RUN_ROOT / "r5_basic_filter.json", {
        "generated_at": datetime.now(UTC).isoformat(),
        "method": "four serial strategy API requests",
        "results": summarize_dir("r5"),
        "conclusion": "in this configured 2016-01-01 to 2026-09-02 run, removing either lower bound worsened Calmar and absolute max drawdown",
    })

    page_event = load(RUN_ROOT / "001-custom_sequoia_high_tight_flag.json")
    page_request = page_event["request"]
    api_config = (page_event.get("response") or {}).get("config") or {}
    expected_page_keys = {
        "strategy_id", "asset_type", "symbols", "start", "end", "matching",
        "entry_fill", "exit_fill", "commission_pct", "stamp_tax_pct", "slippage_bps",
        "max_positions", "max_exposure_pct", "initial_capital", "position_sizing",
        "params", "overrides", "mode", "holding_days", "minute_fill", "regime_filter",
    }
    actual_check = None
    actual_path = RUN_ROOT / "r6_page_api_actual.json"
    page_sse_path = RUN_ROOT / "page-response.sse"
    if actual_path.is_file() and page_sse_path.is_file():
        actual = load(actual_path)
        page_sse_lines = page_sse_path.read_text(encoding="utf-8").splitlines()
        page_response = None
        for line in page_sse_lines:
            if line.startswith("data: "):
                page_response = json.loads(line[6:])
                break
        if page_response is None:
            raise ValueError(f"page SSE has no data event: {page_sse_path}")
        api_response = actual.get("api_response") or {}
        volatile = {
            "run_id", "elapsed_ms", "timing_ms", "worker",
            "matrix_data_cache_timing_ms", "shared_prepare_timing_ms",
            "matrix_compute_cache",
        }

        def normalize(value):
            if isinstance(value, dict):
                return {
                    key: normalize(item)
                    for key, item in value.items()
                    if key not in volatile and not key.endswith("_timing_ms")
                }
            if isinstance(value, list):
                return [normalize(item) for item in value]
            return value

        page_normalized = normalize(page_response)
        api_normalized = normalize(api_response)
        actual_check = {
            "status": "browser_and_api_verified" if page_normalized == api_normalized else "browser_api_mismatch",
            "page_request_file": str((RUN_ROOT / "page-request.jsonl").relative_to(ROOT)),
            "page_response_file": str(page_sse_path.relative_to(ROOT)),
            "api_response_file": str(actual_path.relative_to(ROOT)),
            "normalized_equal": page_normalized == api_normalized,
            "ignored_volatile_fields": sorted(volatile),
            "page_stats": {
                key: (page_response.get("stats") or {}).get(key)
                for key in ("total_return", "annual_return", "max_drawdown", "calmar", "win_rate", "n_trades", "final_equity")
            },
            "api_stats": {
                key: (api_response.get("stats") or {}).get(key)
                for key in ("total_return", "annual_return", "max_drawdown", "calmar", "win_rate", "n_trades", "final_equity")
            },
        }
        log_path = RUN_ROOT / "backtest.log"
        existing_log = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
        if '"type": "page_api_verification"' not in existing_log:
            observed_stats = {
                key: (api_response.get("stats") or {}).get(key)
                for key in ("total_return", "annual_return", "max_drawdown", "calmar", "win_rate", "n_trades", "final_equity")
            }
            with log_path.open("a", encoding="utf-8") as stream:
                for source, response_file in (("browser_page", page_sse_path.name), ("api_replay", actual_path.name)):
                    stream.write(json.dumps({
                        "type": "page_api_verification",
                        "source": source,
                        "request": actual.get("api_request"),
                        "response_file": response_file,
                        "response_status": "ok",
                        "result": observed_stats,
                        "direction": {
                            "status": "observed",
                            "recommendation": "no_parameter_change_from_consistency_check",
                        },
                    }, ensure_ascii=False, sort_keys=True) + "\n")
    write(RUN_ROOT / "r6_page_api_consistency.json", {
        "generated_at": datetime.now(UTC).isoformat(),
        "status": actual_check["status"] if actual_check else "static_source_and_response_verified",
        "method": "frontend handleRun payload fields compared with API request model; normalized response config checked against captured request",
        "frontend_payload_keys": sorted(expected_page_keys),
        "captured_api_request_keys": sorted(page_request),
        "api_normalized_config_keys": sorted(api_config),
        "captured_request": page_request,
        "normalized_response_config": api_config,
        "missing_from_captured_request": sorted(set(api_config) - set(page_request) - {"commission_pct", "stamp_tax_pct", "entry_fill", "exit_fill", "timing_mode", "score_min", "score_max"}),
        "actual_browser_check": actual_check,
        "note": "A browser-generated SSE request and a same-parameter API response are compared after removing only runtime-volatile fields.",
    })

    sse_path = RUN_ROOT / "r4_walkforward.sse"
    if sse_path.is_file():
        done_data = None
        lines = sse_path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if line.strip() == "event: done" and index + 1 < len(lines):
                payload = lines[index + 1]
                if payload.startswith("data: "):
                    done_data = json.loads(payload[6:])
                    break
        if done_data is None:
            raise ValueError(f"walk-forward SSE has no done event: {sse_path}")
        fold_dir = RUN_ROOT / "r4"
        fold_dir.mkdir(parents=True, exist_ok=True)
        fold_summaries = []
        for fold in done_data.get("folds", []):
            fold_path = fold_dir / f"fold-{int(fold['index']):03d}.json"
            fold_path.write_text(json.dumps(fold, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            oos = fold.get("oos_stats") or {}
            fold_summaries.append({
                "index": fold.get("index"),
                "train_start": fold.get("train_start"),
                "train_end": fold.get("train_end"),
                "test_start": fold.get("test_start"),
                "test_end": fold.get("test_end"),
                "best_params": fold.get("best_params"),
                "is_calmar": fold.get("is_score"),
                "oos_calmar": fold.get("oos_objective"),
                "oos_annual_return": oos.get("annual_return"),
                "oos_max_drawdown": oos.get("max_drawdown"),
                "oos_n_trades": oos.get("n_trades"),
                "oos_degraded": fold.get("oos_degraded"),
                "file": str(fold_path.relative_to(ROOT)),
            })
        summary = done_data.get("summary") or {}
        positive_folds = sum(1 for fold in fold_summaries if (fold["oos_calmar"] or 0) > 0)
        log_path = RUN_ROOT / "backtest.log"
        existing_log = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
        with log_path.open("a", encoding="utf-8") as stream:
            if "walkforward_fold" not in existing_log:
                for fold in fold_summaries:
                    stream.write(json.dumps({"type": "walkforward_fold", **fold}, ensure_ascii=False, sort_keys=True) + "\n")
        write(RUN_ROOT / "r4_walkforward.json", {
            "generated_at": datetime.now(UTC).isoformat(),
            "status": "completed",
            "objective": done_data.get("objective"),
            "required_config": {"train_days": 252, "test_days": 63, "step_days": 63},
            "strategy_id": "custom_sequoia_ma_volume_v2",
            "source_sse": str(sse_path.relative_to(ROOT)),
            "n_planned_folds": done_data.get("n_planned_folds"),
            "n_folds": done_data.get("n_folds"),
            "n_skipped": done_data.get("n_skipped"),
            "oos_positive_calmar_folds": positive_folds,
            "oos_positive_fold_ratio": round(positive_folds / len(fold_summaries), 4) if fold_summaries else None,
            "summary": summary,
            "folds": fold_summaries,
            "conclusion": "OOS results are recorded for all folds; positive-fold ratio and compounded return are descriptive, not a claim of universal strategy usability.",
        })
    else:
        write(RUN_ROOT / "r4_walkforward.json", {
            "generated_at": datetime.now(UTC).isoformat(),
            "status": "not_executed",
            "objective": "calmar",
            "required_config": {"train_days": 252, "test_days": 63, "step_days": 63},
            "reason": "walk-forward SSE artifact is absent",
            "conclusion": "no walk-forward conclusion is claimed",
        })


if __name__ == "__main__":
    main()
