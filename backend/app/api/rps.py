"""涨幅轮动矩阵 API。

供「概念分析 → 涨幅RPS轮动」对话框调用。返回最近 N 个交易日的概念涨幅
排名矩阵:每列(日期)各自把所有概念按当天涨幅从高到低排序。
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.services import rps_rotation

router = APIRouter(prefix="/api/rps", tags=["rps"])


@router.get("/rotation")
def get_rotation(
    request: Request,
    days: int = Query(12, ge=7, le=30, description="最近 N 个交易日(7-30)"),
) -> dict:
    """概念涨幅轮动矩阵。

    Returns:
        dates: 日期字符串列表(最新在最前)
        columns: {日期: [[概念名, 涨幅小数], ...]} 每列各自降序
        concept_count: 去重概念总数
    """
    return rps_rotation.build_rps_rotation(request.app.state.repo, days)
