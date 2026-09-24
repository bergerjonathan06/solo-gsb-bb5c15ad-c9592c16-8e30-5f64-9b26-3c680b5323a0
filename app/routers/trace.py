from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlite3 import Connection

from .. import crud
from ..database import get_db
from ..engine import (
    assess_deactivation,
    build_common_trace_report,
    build_measurement_compatibility_report,
    build_report,
    build_window_report,
)
from ..schemas import (
    CommonTraceIn,
    CommonTraceReport,
    CoverageWindowReport,
    DeactivationImpactReport,
    DeactivationIn,
    MeasurementCompatibilityIn,
    MeasurementCompatibilityReport,
    TraceReport,
)

router = APIRouter(prefix="/api", tags=["trace"])


def _report(db: Connection, instrument: str, at: Optional[datetime]) -> dict:
    try:
        return build_report(db, instrument, at)
    except KeyError:
        raise HTTPException(404, f"仪器不存在: {instrument}")


@router.get("/trace/{instrument}", response_model=TraceReport)
def trace_chain(
    instrument: str,
    at: Optional[datetime] = None,
    db: Connection = Depends(get_db),
):
    """链路查询：按指定时点重建该仪器到国家基准的证书链。"""
    return _report(db, instrument, at)


@router.get("/reports/{instrument}", response_model=TraceReport)
def validation_report(
    instrument: str,
    at: Optional[datetime] = None,
    db: Connection = Depends(get_db),
):
    """校验报告：重建结果 + 区间/签名/不确定度的可解释冲突明细。"""
    return _report(db, instrument, at)


@router.get(
    "/coverage/{instrument}",
    response_model=CoverageWindowReport,
    tags=["time-window-coverage"],
)
def instrument_time_window_coverage(
    instrument: str,
    start: datetime = Query(
        ...,
        alias="start",
        description="查询窗起点（UTC，含），如 2026-01-01T00:00:00Z",
    ),
    end: datetime = Query(
        ...,
        alias="end",
        description="查询窗终点（UTC，含），如 2026-12-31T23:59:59Z",
    ),
    db: Connection = Depends(get_db),
):
    """仪器时间窗覆盖查询：按有效期边界划分区段并逐段给出校验状态。

    以仪器编码与 UTC 闭时间窗 ``[start, end]`` 为输入：收集该仪器相关
    证书及溯源边落在窗内的全部有效期边界（valid_from/valid_until），
    在每个**边界点**（闭区间语义）与每对相邻边界之间的**开区间内部**
    各运行一次现有时点校验报告；再将结果按时间排列，仅当状态
    （resolution）、有效性（valid）与冲突码多重集完全相同且端点
    严格连续（一开一闭相接）时合并为一个区段。每段显式标明
    ``start_inclusive`` / ``end_inclusive``，因此证书首尾相接（闭区间）
    时交接点上的瞬时冲突（如瞬时双重有效）不会被相邻区段吞掉。
    """
    try:
        return build_window_report(db, instrument, start, end)
    except KeyError:
        raise HTTPException(404, f"仪器不存在: {instrument}")
    except crud.ValidationError as exc:
        raise HTTPException(422, str(exc))


@router.post(
    "/assessments/certificate-deactivation",
    response_model=DeactivationImpactReport,
    tags=["impact-assessment"],
)
def certificate_deactivation_impact(
    body: DeactivationIn, db: Connection = Depends(get_db)
):
    """证书停用影响评估（dry-run）。

    在不写入数据库的前提下，模拟移除待停用证书及其关联溯源边，按查询时点
    重新评估受影响仪器的唯一链 / 多候选 / 断裂状态，给出停用前后状态、
    替代链摘要与可解释说明；可用 ``instrument_codes`` 限定评估仪器范围。
    """
    try:
        return assess_deactivation(
            db,
            body.at,
            body.certificate_ids,
            body.instrument_codes,
        )
    except crud.ValidationError as exc:
        raise HTTPException(422, str(exc))


@router.post(
    "/assessments/common-traceability",
    response_model=CommonTraceReport,
    tags=["common-traceability"],
)
def common_traceability(body: CommonTraceIn, db: Connection = Depends(get_db)):
    """批次共同溯源核查（只读，不写入业务数据）。

    输入至少两台**不同**仪器编码与 UTC 查询时点。服务端逐台复用现有
    单仪器链路报告：仅以该时点**唯一且校验通过**（resolution=unique 且
    零 ERROR）的证书链参与比较；其余仪器（不存在 / 无有效证书 / 起点
    不唯一 / 多候选链 / 链断裂 / 校验失败）逐台返回不可参与原因，
    **多候选链绝不任意选取一条**。

    对参与仪器的链证书 id 求交集，并按以下规则选出最近共同证书：

    1. 各链到候选共同证书的距离最大值最小；
    2. 最大值相同取距离总和最小；
    3. 仍相同取证书 ID 字典序末位。

    返回该共同证书、各参与仪器到它的证书子链及距离；无交集时明确
    返回“无共同证书”。整个过程只读数据库，响应恒带 ``dry_run: true``。
    """
    return build_common_trace_report(db, body.at, body.instrument_codes)


@router.post(
    "/assessments/measurement-compatibility",
    response_model=MeasurementCompatibilityReport,
    tags=["measurement-compatibility"],
)
def measurement_compatibility(
    body: MeasurementCompatibilityIn, db: Connection = Depends(get_db)
):
    """批次测量值相容性评估（只读，不写入业务数据）。

    输入 UTC 评估时点、目标单位及 2～12 条**仪器编码互异**的有限测量
    值读数。服务端逐条复用现有单仪器链路报告：仅以该时点**唯一且校验
    通过**（resolution=unique 且零 ERROR）且**起点证书不确定度单位与
    目标单位一致**的读数参与计算；链不可用（仪器不存在 / 无有效证书 /
    起点不唯一 / 多候选链 / 链断裂 / 校验失败）或单位不符的读数逐条
    返回原因，**多候选链绝不任意选取一条**。

    对可参与读数两两计算归一化偏差
    ``En = |x_i - x_j| / sqrt(U_i² + U_j²)``，其中 U 取各仪器起点证书
    的扩展不确定度：``En <= 1`` 判为相容；两个 U 均为 0 时分母为零，
    仅当测量值相等才相容。响应给出每对读数的 En 与判定，以及所有
    “人数至少为 2”的**极大两两相容分组**（极大团枚举，分组可重叠，
    按仪器编码稳定排序，不用连通分量代替）。整个过程只读数据库，
    响应恒带 ``dry_run: true``。
    """
    readings = [(r.instrument_code, r.value) for r in body.readings]
    return build_measurement_compatibility_report(
        db, body.at, body.target_unit, readings
    )
