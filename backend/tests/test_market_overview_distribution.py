from app.services.market_overview_builder import _pct_band_rows


def test_pct_band_rows_match_symmetric_market_distribution_bins():
    values = [-0.11, -0.10, -0.08, -0.07, -0.06, -0.05, -0.04, -0.03, -0.01, 0.0, 0.01, 0.03, 0.04, 0.05, 0.06, 0.07, 0.09, 0.10, 0.11]

    rows = _pct_band_rows(values)

    assert [row["label"] for row in rows] == [">10%", "10~7", "7~5", "5~3", "3~0", "0", "0~3", "3~5", "5~7", "7~10", ">10%"]
    assert [row["count"] for row in rows] == [1, 2, 2, 2, 2, 1, 2, 2, 2, 2, 1]
    assert sum(row["count"] for row in rows) == len(values)


def test_pct_band_rows_rounds_to_display_precision_before_bucketing():
    # 7.0004%/10.0006% 展示为 7.00%/10.00%，应按展示值归档。
    values = [-0.070004, -0.050004, -0.030004, 0.02996, 0.030004, 0.070004, 0.100006]

    rows = _pct_band_rows(values)

    assert [row["count"] for row in rows] == [0, 0, 1, 1, 1, 0, 2, 0, 1, 1, 0]


def test_pct_band_rows_keeps_exact_boundaries_in_the_lower_magnitude_bucket():
    rows = _pct_band_rows([-0.10, -0.07, -0.05, -0.03, 0.03, 0.05, 0.07, 0.10])

    assert [row["count"] for row in rows] == [0, 1, 1, 1, 1, 0, 1, 1, 1, 1, 0]
