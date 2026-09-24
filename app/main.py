"""计量校准溯源 API 入口。"""
from __future__ import annotations

import math
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .database import get_conn_ctx, init_db
from .routers import (
    calibration,
    certificates,
    edges,
    export,
    instruments,
    submissions,
    trace,
)
from .seed import DEMO_AT, seed_demo


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if os.getenv("SEED_DEMO", "1") == "1":
        with get_conn_ctx() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM instruments").fetchone()["n"]
            if n == 0:
                seed_demo(conn)
    yield


app = FastAPI(
    title="计量校准溯源 API",
    description=(
        "接收实验室仪器、校准证书、父子溯源关系及有效区间；按指定时点重建"
        "从仪器到国家基准的唯一证书链，校验区间连续性、签名摘要与测量不确定度，"
        "并在多候选链或区间断裂时返回可解释的冲突节点。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def _sanitizing_validation_handler(
    request: Request, exc: RequestValidationError
):
    """422 校验错误处理器。

    默认处理器会把请求中的原始输入原样回显；当输入含 NaN/Infinity 等
    非有限浮点（如相容性评估的测量值）时，回显体会违反 JSON 规范并在
    响应序列化阶段二次失败。这里把非有限值替换为字符串占位，保证
    客户端始终拿到结构化 422 而非 500。
    """

    def _sanitize(value):
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        if isinstance(value, BaseException):
            return str(value)
        if isinstance(value, dict):
            return {k: _sanitize(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_sanitize(v) for v in value]
        return value

    return JSONResponse(
        status_code=422,
        content={"detail": [_sanitize(e) for e in exc.errors()]},
    )

app.include_router(instruments.router)
app.include_router(certificates.router)
app.include_router(edges.router)
app.include_router(submissions.router)
app.include_router(trace.router)
app.include_router(calibration.router)
app.include_router(export.router)
app.include_router(export.admin_router)


@app.get("/", tags=["meta"])
def root():
    return {
        "service": "metrological-traceability-api",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "endpoints": {
            "chain_query": "/api/trace/{instrument}?at=YYYY-MM-DDTHH:MM:SSZ",
            "validation_report": "/api/reports/{instrument}",
            "time_window_coverage": (
                "/api/coverage/{instrument}"
                "?start=YYYY-MM-DDTHH:MM:SSZ&end=YYYY-MM-DDTHH:MM:SSZ"
            ),
            "json_export": "/api/export/{instrument}",
            "batch_submission": "POST /api/submissions",
            "batch_submission_preflight": (
                "POST /api/submissions/preflight（dry-run，不写库）"
            ),
            "certificate_deactivation_impact": (
                "POST /api/assessments/certificate-deactivation"
            ),
            "batch_common_traceability": (
                "POST /api/assessments/common-traceability"
                "（多仪器共同溯源核查，只读，不写库）"
            ),
            "batch_measurement_compatibility": (
                "POST /api/assessments/measurement-compatibility"
                "（多仪器测量值 En 两两相容性 + 极大相容分组，只读，不写库）"
            ),
            "calibration_slot_query": (
                "GET /api/calibration-slots?instrument=&start=&end=&duration_seconds="
            ),
            "calibration_booking": (
                "POST /api/calibration-bookings"
                "（同一写事务内重查占用与证书截止，冲突 409）"
            ),
            "calibration_booking_list": "GET /api/calibration-bookings?instrument=",
        },
        "demo_query_time": DEMO_AT,
    }


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
