"""请求/响应 Pydantic 模型。时间均按 UTC 解释。"""
from __future__ import annotations

import math
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .utils import SUPPORTED_ALGS, compute_digest, fmt


# ---------- 输入 ----------

class InstrumentIn(BaseModel):
    code: str = Field(..., min_length=1, description="仪器唯一编码")
    name: str
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    serial_no: Optional[str] = None
    is_national_standard: bool = Field(
        False, description="是否为国家基准（溯源根节点）"
    )


class CertificateIn(BaseModel):
    id: str = Field(..., min_length=1)
    instrument_code: str
    cert_number: str
    issued_at: datetime
    valid_from: datetime
    valid_until: datetime
    uncertainty: float = Field(..., ge=0)
    unit: str = ""
    digest_alg: str = "sha256"
    signature_digest: Optional[str] = Field(
        None, description="证书签名摘要；留空则由服务端按规范化载荷重算"
    )
    signed_payload: Optional[str] = Field(
        None, description="被签名的原始载荷；留空则按证书字段规范化生成"
    )

    @field_validator("digest_alg")
    @classmethod
    def _check_alg(cls, v: str) -> str:
        v = v.lower()
        if v not in SUPPORTED_ALGS:
            raise ValueError(f"不支持的摘要算法: {v}")
        return v

    def normalized(self) -> dict:
        data = self.model_dump()
        data["issued_at"] = fmt(self.issued_at)
        data["valid_from"] = fmt(self.valid_from)
        data["valid_until"] = fmt(self.valid_until)
        data["digest_alg"] = self.digest_alg.lower()
        if not data["signed_payload"]:
            data["signed_payload"] = None
        if not data["signature_digest"]:
            data["signature_digest"] = None
        return data


class TraceEdgeIn(BaseModel):
    child_cert_id: str
    parent_cert_id: str
    valid_from: Optional[datetime] = Field(
        None, description="边有效起点；留空取子证书 valid_from"
    )
    valid_until: Optional[datetime] = Field(
        None, description="边有效终点；留空取子证书 valid_until"
    )
    note: str = ""


class SubmissionIn(BaseModel):
    """实验室一次提交：仪器 + 证书 + 父子溯源边（全部按编码 upsert）。"""
    instruments: List[InstrumentIn] = Field(default_factory=list)
    certificates: List[CertificateIn] = Field(default_factory=list)
    edges: List[TraceEdgeIn] = Field(default_factory=list)


class SubmissionPreflightIn(SubmissionIn):
    """批量提交预检请求：载荷与 /api/submissions 相同，另加摘要校验开关。"""

    strict_digest: bool = Field(
        False,
        description=(
            "true 时显式 signature_digest 与重算值不符按错误（ERROR）计；"
            "false（默认）时仅按警告（WARN）计，不阻断提交"
        ),
    )


class DeactivationIn(BaseModel):
    """证书停用影响评估请求（dry-run，不写入数据库）。"""
    at: Optional[datetime] = Field(
        None, description="查询时点（UTC）；留空取服务端当前时间"
    )
    certificate_ids: List[str] = Field(
        ..., min_length=1, description="待停用证书 id 列表（至少 1 张）"
    )
    instrument_codes: Optional[List[str]] = Field(
        None,
        description="仅评估指定仪器；留空则自动覆盖全部受影响仪器",
    )

    @field_validator("certificate_ids")
    @classmethod
    def _check_certificate_ids(cls, v: List[str]) -> List[str]:
        if any(not cid or not cid.strip() for cid in v):
            raise ValueError("证书 id 不能为空字符串")
        return v

    @field_validator("instrument_codes")
    @classmethod
    def _check_instrument_codes(cls, v):
        if v is not None:
            if len(v) == 0:
                raise ValueError("仪器编码筛选列表不能为空")
            if any(not c or not c.strip() for c in v):
                raise ValueError("仪器编码不能为空字符串")
        return v


class CalibrationBookingIn(BaseModel):
    """校准工位预约确认请求（起点 + 所需时长，结束时间由服务端计算）。"""

    instrument_code: str = Field(..., min_length=1, description="受校准仪器编码")
    start: datetime = Field(
        ..., description="预约开始时刻（UTC，含），如 2026-06-20T09:00:00Z"
    )
    duration_seconds: int = Field(
        ..., gt=0, description="所需时长（正整数秒）；结束 = start + duration"
    )
    note: str = ""


class CommonTraceIn(BaseModel):
    """批次共同溯源核查请求（只读，不写入数据库）。"""

    at: Optional[datetime] = Field(
        None, description="UTC 查询时点；留空取服务端当前时间"
    )
    instrument_codes: List[str] = Field(
        ...,
        min_length=2,
        description="参与核查的仪器编码列表（至少 2 台，且编码互不相同）",
    )

    @field_validator("instrument_codes")
    @classmethod
    def _check_instrument_codes(cls, v: List[str]) -> List[str]:
        if any(not c or not c.strip() for c in v):
            raise ValueError("仪器编码不能为空字符串")
        codes = [c.strip() for c in v]
        distinct = list(dict.fromkeys(codes))
        if len(distinct) < 2:
            raise ValueError("共同溯源核查至少需要 2 台不同仪器")
        if len(distinct) != len(codes):
            dup = sorted({c for c in codes if codes.count(c) > 1})
            raise ValueError(f"仪器编码不得重复: {dup}")
        return codes


class MeasurementReadingIn(BaseModel):
    """一条仪器测量读数：互异仪器编码 + 有限测量值（以目标单位计）。"""

    instrument_code: str = Field(..., description="测量所用仪器编码")
    value: float = Field(
        ..., description="该仪器在评估时点的有限测量值（以 target_unit 计）"
    )

    @field_validator("instrument_code")
    @classmethod
    def _check_instrument_code(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("仪器编码不能为空字符串")
        return v.strip()

    @field_validator("value")
    @classmethod
    def _check_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("测量值必须为有限数值（不接受 NaN/Infinity/-Infinity）")
        return v


class MeasurementCompatibilityIn(BaseModel):
    """批次测量值相容性评估请求（只读，不写入数据库）。"""

    at: Optional[datetime] = Field(
        None, description="UTC 评估时点；留空取服务端当前时间"
    )
    target_unit: str = Field(
        ..., min_length=1, description="目标测量单位（如 mm / kPa），需与起点证书单位一致"
    )
    readings: List[MeasurementReadingIn] = Field(
        ...,
        min_length=2,
        max_length=12,
        description="2～12 条测量读数，仪器编码必须互不相同",
    )

    @field_validator("target_unit")
    @classmethod
    def _check_target_unit(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("目标单位不能为空字符串")
        return v.strip()

    @model_validator(mode="after")
    def _check_distinct_instruments(self):
        codes = [r.instrument_code.strip() for r in self.readings]
        distinct = list(dict.fromkeys(codes))
        if len(distinct) != len(codes):
            dup = sorted({c for c in codes if codes.count(c) > 1})
            raise ValueError(f"读数中的仪器编码不得重复: {dup}")
        return self


# ---------- 输出 ----------

class InstrumentOut(InstrumentIn):
    created_at: datetime


class CertificateOut(BaseModel):
    id: str
    instrument_code: str
    cert_number: str
    issued_at: datetime
    valid_from: datetime
    valid_until: datetime
    uncertainty: float
    unit: str
    digest_alg: str
    signature_digest: str
    signed_payload: str
    created_at: datetime


class TraceEdgeOut(BaseModel):
    id: int
    child_cert_id: str
    parent_cert_id: str
    valid_from: datetime
    valid_until: datetime
    note: str


class SubmissionResult(BaseModel):
    instruments_upserted: List[str]
    certificates_upserted: List[str]
    edges_upserted: List[int]


class PreflightIssue(BaseModel):
    """预检发现的单条错误/警告，可精确定位到提交载荷中的字段路径。"""

    path: str = Field(
        ...,
        description=(
            "稳定的字段路径（JSON Pointer 风格），如 "
            "certificates/0/instrument_code、edges/2/parent_cert_id"
        ),
    )
    code: str = Field(..., description="机器可读错误码")
    message: str = Field(..., description="中文原因说明")
    detail: dict = Field(default_factory=dict, description="结构化证据")


class PreflightUpsertCount(BaseModel):
    instruments: int
    certificates: int
    edges: int
    total: int


class SubmissionPreflightResult(BaseModel):
    strict_digest: bool
    dry_run: bool = Field(
        ..., description="恒为 true：预检在独立事务中模拟并整体回滚，不写库"
    )
    ok: bool = Field(
        ..., description="errors 为空时为 true（警告不影响 ok）"
    )
    error_count: int
    warning_count: int
    errors: List[PreflightIssue] = Field(
        default_factory=list, description="稳定排序后的错误列表"
    )
    warnings: List[PreflightIssue] = Field(
        default_factory=list, description="稳定排序后的警告列表"
    )
    would_upsert: PreflightUpsertCount = Field(
        ..., description="若按当前载荷正式提交，将执行 upsert 的条目数量"
    )


class Conflict(BaseModel):
    code: str
    severity: str = Field(..., description="ERROR | WARN")
    node: Optional[str] = Field(None, description="冲突所在证书 id（若适用）")
    instrument: Optional[str] = Field(None, description="冲突所在仪器编码（若适用）")
    path_index: Optional[int] = Field(
        None, description="冲突所属候选链下标（节点级检查为 None）"
    )
    message: str
    detail: dict = Field(default_factory=dict)


class ChainNode(BaseModel):
    certificate_id: str
    cert_number: str
    instrument_code: str
    instrument_name: str
    is_national_standard: bool
    valid_from: datetime
    valid_until: datetime
    issued_at: datetime
    uncertainty: float
    unit: str


class ChainEdge(BaseModel):
    edge_id: int
    child_cert_id: str
    parent_cert_id: str
    valid_from: datetime
    valid_until: datetime
    note: str


class Chain(BaseModel):
    nodes: List[ChainNode]
    edges: List[ChainEdge]
    reaches_national_standard: bool
    cycle: bool
    errors: int = 0
    warnings: int = 0


class TraceReport(BaseModel):
    instrument: str
    at: datetime
    resolution: str = Field(
        ..., description="unique | ambiguous | broken（唯一/多候选/断裂）"
    )
    valid: bool = Field(..., description="唯一成链且无 ERROR 级问题时为 true")
    starts: List[str]
    active_chain: Optional[Chain] = None
    candidates: List[Chain] = Field(
        default_factory=list, description="到达国家基准的候选链（ambiguous 时 >=2）"
    )
    broken_chains: List[Chain] = Field(
        default_factory=list, description="未能到达国家基准的部分链"
    )
    conflicts: List[Conflict] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    statistics: dict = Field(default_factory=dict)


# ---------- 证书停用影响评估输出 ----------

class ChainSummary(BaseModel):
    role: str = Field(
        ...,
        description="链在评估中的角色：active=唯一有效链 | candidate=候选链 | broken=断裂分支",
    )
    certificate_ids: List[str]
    node_count: int
    reaches_national_standard: bool
    contains_disabled_certificate: bool
    disabled_certificates_on_path: List[str]
    errors: int = 0
    warnings: int = 0
    chain: Chain = Field(..., description="完整链（节点 + 边）摘要")


class TraceStateSnapshot(BaseModel):
    """停用前/后任一时刻的完整评估快照（与 TraceReport 同构）。"""
    resolution: str
    valid: bool
    starts: List[str]
    active_chain: Optional[Chain] = None
    candidates: List[Chain] = Field(default_factory=list)
    broken_chains: List[Chain] = Field(default_factory=list)
    conflicts: List[Conflict] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    statistics: dict = Field(default_factory=dict)


class InstrumentDeactivationResult(BaseModel):
    instrument: str
    impacted: bool = Field(..., description="停用前后评估结果是否发生变化")
    transition: str = Field(
        ...,
        description=(
            "状态迁移：unchanged | unique_to_broken | unique_to_ambiguous | "
            "ambiguous_to_unique | ambiguous_to_broken | broken_to_unique | "
            "broken_to_ambiguous"
        ),
    )
    before: TraceStateSnapshot = Field(..., description="停用前状态")
    after: TraceStateSnapshot = Field(..., description="停用后（模拟）状态")
    alternative_chain: Optional[ChainSummary] = Field(
        None, description="停用后的唯一有效替代链（若存在）"
    )
    alternative_chains: List[ChainSummary] = Field(
        default_factory=list,
        description="停用后仍存在的全部到基准候选链（多候选时 >=2）",
    )
    severed_chains: List[ChainSummary] = Field(
        default_factory=list,
        description="停用前存在、因途经被停用证书而被切断的链",
    )
    simulation_notes: List[Conflict] = Field(
        default_factory=list,
        description="模拟过程的可解释说明（severity=INFO，非真实数据冲突）",
    )


# ---------- 仪器时间窗覆盖查询输出 ----------

class CoverageSegment(BaseModel):
    """时间窗内状态恒定的一个区段（按起点升序，互不重叠，覆盖整个查询窗）。"""

    start: datetime = Field(..., description="区段起点（UTC）")
    end: datetime = Field(..., description="区段终点（UTC）")
    start_inclusive: bool = Field(
        ..., description="是否包含起点；与相邻区段在边界点上恰好一含一不含"
    )
    end_inclusive: bool = Field(
        ..., description="是否包含终点；与相邻区段在边界点上恰好一含一不含"
    )
    resolution: str = Field(
        ..., description="unique | ambiguous | broken（与 /api/reports 同义）"
    )
    valid: bool = Field(..., description="该区段代表采样点上唯一成链且无 ERROR")
    conflict_codes: List[str] = Field(
        default_factory=list,
        description="该区段全部冲突码（去重排序）；合并时按冲突码多重集判定",
    )
    error_count: int
    warning_count: int
    sample_at: datetime = Field(
        ...,
        description=(
            "该区段代表评估点：点区段为该边界点本身；"
            "开区间区段为两端点之间的内部采样时刻（整秒中点或半秒）"
        ),
    )
    starts: List[str] = Field(
        default_factory=list, description="代表采样点上该仪器的有效起点证书"
    )
    conflicts: List[Conflict] = Field(
        default_factory=list, description="代表采样点的完整可解释冲突明细"
    )
    statistics: dict = Field(default_factory=dict)


class CoverageWindowReport(BaseModel):
    instrument: str
    window_start: datetime = Field(..., description="查询窗起点（UTC，含）")
    window_end: datetime = Field(..., description="查询窗终点（UTC，含）")
    boundary_count: int = Field(
        ..., description="窗内相关证书/溯源边有效期边界点数量（不含窗口端点）"
    )
    grid_points: List[datetime] = Field(
        default_factory=list,
        description="窗口端点与窗内边界点合并去重、升序排列后的完整网格",
    )
    segment_count: int
    segments: List[CoverageSegment] = Field(
        default_factory=list,
        description=(
            "按时间排列的状态区段；仅当 resolution/valid/冲突码多重集相同"
            "且端点严格连续（一开一闭相接）时合并，瞬时冲突独占点区段"
        ),
    )
    statistics: dict = Field(default_factory=dict)


class DeactivationImpactReport(BaseModel):
    at: datetime
    dry_run: bool = Field(
        ..., description="恒为 true：模拟不写入数据库"
    )
    disabled_certificate_ids: List[str]
    unknown_certificate_ids: List[str] = Field(
        default_factory=list, description="不存在于库中的待停用证书 id"
    )
    instruments_filter: Optional[List[str]] = Field(
        None, description="请求指定的仪器筛选范围；未筛选为 null"
    )
    affected_owner_instruments: List[str] = Field(
        default_factory=list, description="被停用证书直接所属的仪器编码"
    )
    removed_edge_count: int = Field(
        ..., description="模拟中一并移除的溯源边数量"
    )
    affected_instrument_count: int
    evaluated_instrument_count: int
    transition_counts: dict = Field(
        default_factory=dict, description="按状态迁移类型计数"
    )
    results: List[InstrumentDeactivationResult]
    statistics: dict = Field(default_factory=dict)


# ---------- 批次共同溯源核查输出 ----------

class CommonTraceNonparticipant(BaseModel):
    """一台不可参与共同比较的仪器及其原因。"""

    instrument: str
    reason_code: str = Field(
        ...,
        description=(
            "不可参与原因码：INSTRUMENT_NOT_FOUND | NO_ACTIVE_CERTIFICATE | "
            "MULTIPLE_ACTIVE_CERTIFICATES | MULTIPLE_VALID_CHAINS | "
            "CHAIN_BROKEN | ALL_CHAINS_FAILED_CHECKS | CHAIN_VALIDATION_FAILED"
        ),
    )
    reason: str = Field(..., description="中文原因说明")
    resolution: Optional[str] = Field(
        None, description="该时点单仪器报告的 resolution（仪器不存在时为 null）"
    )
    valid: bool
    starts: List[str] = Field(
        default_factory=list, description="该时点有效的起点证书 id"
    )
    conflicts: List[Conflict] = Field(
        default_factory=list,
        description="该时点单仪器报告的完整可解释冲突明细（结构同 /api/reports）",
    )
    error_count: Optional[int] = None
    warning_count: Optional[int] = None


class CommonCandidateDistance(BaseModel):
    instrument: str
    distance: int = Field(
        ..., description="该仪器的唯一链从起点证书到候选共同证书经过的边数"
    )


class CommonCertificateCandidate(BaseModel):
    """最近共同证书的一个候选（参与仪器链证书集合的交集成员）。"""

    certificate_id: str
    max_distance: int = Field(..., description="各仪器到该证书距离的最大值")
    total_distance: int = Field(..., description="各仪器到该证书距离的总和")
    distances: List[CommonCandidateDistance] = Field(
        ..., description="各参与仪器到该证书的距离"
    )
    selected: bool = Field(..., description="是否为最终选出的最近共同证书")


class InstrumentCommonChain(BaseModel):
    """单台仪器在共同核查中的结果：参与则给子链与距离，否则给不可参与原因。"""

    instrument: str
    participated: bool = Field(
        ..., description="该仪器是否以唯一且校验通过的证书链参与了比较"
    )
    start_certificate_id: Optional[str] = Field(
        None, description="该仪器在查询时点的唯一有效起点证书 id"
    )
    chain_certificate_ids: List[str] = Field(
        default_factory=list,
        description="该仪器完整的唯一有效链（叶子→根的证书 id 序列）",
    )
    sub_chain: Optional[Chain] = Field(
        None, description="从该仪器起点证书到共同证书（含）为止的证书子链"
    )
    distance: Optional[int] = Field(
        None, description="起点证书到共同证书经过的边数（起点证书自身为 0）"
    )
    nonparticipation_reason_code: Optional[str] = None
    nonparticipation_reason: Optional[str] = None


class CommonTraceReport(BaseModel):
    at: datetime
    dry_run: bool = Field(
        ..., description="恒为 true：共同核查为只读计算，不写入业务数据"
    )
    instrument_codes: List[str] = Field(
        ..., description="请求提交的仪器编码（按提交顺序、已去空白）"
    )
    participant_count: int
    nonparticipant_count: int
    participants: List[str] = Field(
        default_factory=list,
        description="实际以唯一且校验通过的证书链参与比较的仪器编码",
    )
    nonparticipants: List[CommonTraceNonparticipant] = Field(
        default_factory=list,
        description="逐台给出的不可参与仪器及原因（多候选链不任取一条）",
    )
    common_certificate_found: bool = Field(
        ..., description="参与仪器的链证书 id 交集是否非空"
    )
    common_certificate_id: Optional[str] = Field(
        None, description="选出的最近共同证书 id；无交集时为 null"
    )
    common_certificate: Optional[ChainNode] = Field(
        None, description="最近共同证书节点详情；无共同证书时为 null"
    )
    reason_code: Optional[str] = Field(
        None,
        description=(
            "未能给出共同证书时的原因码：INSUFFICIENT_PARTICIPANTS | "
            "NO_COMMON_CERTIFICATE；成功时为 null"
        ),
    )
    reason: Optional[str] = Field(None, description="中文原因说明")
    selection_rule: List[str] = Field(
        default_factory=list,
        description="最近共同证书选择规则（按优先级的中文描述）",
    )
    candidates: List[CommonCertificateCandidate] = Field(
        default_factory=list,
        description="全部共同证书候选及距离指标（按当选顺序排列）",
    )
    instrument_chains: List[InstrumentCommonChain] = Field(
        default_factory=list,
        description="逐台仪器的参与结果：子链、距离或不可参与原因",
    )
    statistics: dict = Field(default_factory=dict)


# ---------- 校准工位预约输出 ----------

class CalibrationBookingOut(BaseModel):
    """一条已确认的校准工位预约（占用区间为半开 [start_at, end_at)）。"""

    id: str
    instrument_code: str
    start_at: datetime = Field(..., description="占用起点（UTC，含）")
    end_at: datetime = Field(..., description="占用终点（UTC，不含）")
    duration_seconds: int
    certificate_id: str = Field(
        ..., description="确认时该仪器唯一有效溯源链的起点证书 id"
    )
    certificate_valid_until: datetime = Field(
        ..., description="确认时起点证书的有效期截止（UTC）"
    )
    note: str
    created_at: datetime


class SlotBusyBooking(BaseModel):
    """空档查询窗内与该窗（半开）相交的一条既有占用。"""

    booking_id: str
    instrument_code: str
    start_at: datetime
    end_at: datetime


class CalibrationSlot(BaseModel):
    """最早可用时段（半开 [start, end)）。"""

    start: datetime = Field(..., description="时段起点（UTC，含）")
    end: datetime = Field(..., description="时段终点（UTC，不含）")
    ends_by_certificate_valid_until: bool = Field(
        ..., description="时段结束是否不晚于起点证书有效期截止"
    )


class CalibrationSlotsOut(BaseModel):
    """GET /api/calibration-slots 响应。

    链不可用、窗内放不下或无空档时 ``slot`` 为 null 并给出
    ``reason_code`` / ``reason``；``trace_report`` 为查询起点该仪器
    的完整溯源校验报告（链不可用时据此说明原因）。
    """

    instrument: str
    window_start: datetime = Field(..., description="请求的查询窗起点（UTC，含）")
    window_end: datetime = Field(..., description="请求的查询窗终点（UTC）")
    duration_seconds: int
    traceability_at: datetime = Field(
        ..., description="溯源链判定时点（UTC，取查询窗起点）"
    )
    traceability_available: bool = Field(
        ..., description="查询起点是否存在唯一且校验通过的溯源链"
    )
    reason_code: Optional[str] = Field(
        None,
        description=(
            "无空档原因：TRACEABILITY_CHAIN_UNAVAILABLE | "
            "WINDOW_TOO_SHORT_FOR_DURATION | NO_FREE_SLOT_IN_WINDOW | "
            "CERTIFICATE_DEADLINE_TOO_EARLY；有时段时为 null"
        ),
    )
    reason: Optional[str] = Field(None, description="中文原因说明")
    chain_reason_code: Optional[str] = Field(
        None,
        description=(
            "溯源链不可用的细分原因：NO_ACTIVE_CERTIFICATE | "
            "MULTIPLE_ACTIVE_CERTIFICATES | MULTIPLE_VALID_CHAINS | "
            "CHAIN_BROKEN | ALL_CHAINS_FAILED_CHECKS | CHAIN_VALIDATION_FAILED"
        ),
    )
    certificate_id: Optional[str] = Field(
        None, description="唯一有效链的起点证书 id（链不可用时为 null）"
    )
    certificate_valid_until: Optional[datetime] = Field(
        None, description="起点证书有效期截止（预约须在此之前结束）"
    )
    search_window_start: datetime = Field(
        ..., description="实际搜索窗起点（UTC，等于请求窗起点）"
    )
    search_window_end: datetime = Field(
        ..., description="实际搜索窗终点（UTC，受证书截止裁剪）"
    )
    slot: Optional[CalibrationSlot] = Field(
        None, description="时间窗内最早可用时段；不存在时为 null"
    )
    busy: List[SlotBusyBooking] = Field(
        default_factory=list,
        description="与搜索窗（半开）相交的既有预约，按起点升序",
    )
    trace_report: dict = Field(
        ..., description="查询起点该仪器的完整溯源校验报告（结构同 /api/reports）"
    )


# ---------- 批次测量值相容性评估输出 ----------

class MeasurementNonparticipant(BaseModel):
    """一条不可参与相容性计算的读数及其原因。"""

    instrument: str = Field(..., description="读数提交的仪器编码")
    value: Optional[float] = Field(
        None, description="该读数的测量值（以 target_unit 计）"
    )
    reason_code: str = Field(
        ...,
        description=(
            "不可参与原因码：INSTRUMENT_NOT_FOUND | NO_ACTIVE_CERTIFICATE | "
            "MULTIPLE_ACTIVE_CERTIFICATES | MULTIPLE_VALID_CHAINS | "
            "CHAIN_BROKEN | ALL_CHAINS_FAILED_CHECKS | CHAIN_VALIDATION_FAILED | "
            "CERTIFICATE_UNIT_MISMATCH"
        ),
    )
    reason: str = Field(..., description="中文原因说明")
    resolution: Optional[str] = Field(
        None, description="该时点单仪器报告的 resolution（仪器不存在时为 null）"
    )
    valid: bool
    start_certificate_id: Optional[str] = Field(
        None, description="该仪器唯一有效链的起点证书 id（链不可用时为 null）"
    )
    certificate_unit: Optional[str] = Field(
        None, description="起点证书登记的不确定度单位（单位不符时给出，否则为 null）"
    )
    uncertainty: Optional[float] = Field(
        None, description="起点证书的扩展不确定度（不参与计算时为 null）"
    )
    conflicts: List[Conflict] = Field(
        default_factory=list,
        description="该时点单仪器报告的完整可解释冲突明细（结构同 /api/reports）",
    )
    error_count: Optional[int] = None
    warning_count: Optional[int] = None


class MeasurementReadingResult(BaseModel):
    """单条读数的参与结果（按请求中的读数顺序排列）。"""

    instrument: str
    value: float = Field(..., description="测量值（以 target_unit 计）")
    participated: bool = Field(
        ..., description="该读数是否以唯一且校验通过、单位一致的溯源链参与计算"
    )
    start_certificate_id: Optional[str] = Field(
        None, description="该仪器在评估时点唯一有效链的起点证书 id"
    )
    uncertainty: Optional[float] = Field(
        None, description="起点证书的扩展不确定度（不参与计算时为 null）"
    )
    unit: Optional[str] = Field(
        None, description="起点证书登记的不确定度单位（不参与计算时为 null）"
    )
    nonparticipation_reason_code: Optional[str] = None
    nonparticipation_reason: Optional[str] = None


class CompatibilityPair(BaseModel):
    """一对参与读数的 En 计算结果（仪器编码字典序升序，两两一组）。"""

    instrument_i: str
    instrument_j: str
    value_i: float = Field(..., description="仪器 i 的测量值（以 target_unit 计）")
    value_j: float = Field(..., description="仪器 j 的测量值（以 target_unit 计）")
    uncertainty_i: float = Field(..., description="仪器 i 起点证书扩展不确定度 U_i")
    uncertainty_j: float = Field(..., description="仪器 j 起点证书扩展不确定度 U_j")
    en: Optional[float] = Field(
        None,
        description=(
            "归一化偏差 En=|x_i-x_j|/sqrt(U_i²+U_j²)；"
            "两 U 均为 0 时分母为 0，无法计算，返回 null"
        ),
    )
    compatible: bool = Field(
        ..., description="En<=1 判为相容；两 U 均为 0 时仅测量值相等才相容"
    )
    decision: str = Field(
        ...,
        description=(
            "判定中文描述：COMPATIBLE | INCOMPATIBLE | "
            "BOTH_UNCERTAINTY_ZERO_EQUAL | BOTH_UNCERTAINTY_ZERO_UNEQUAL"
        ),
    )


class CompatibilityGroupMember(BaseModel):
    group_index: int = Field(
        ..., description="所在相容分组的 1 基序号（与 groups 下标对应）"
    )
    instrument: str
    value: float
    uncertainty: float


class CompatibilityGroup(BaseModel):
    """一个人数至少为 2 的极大两两相容分组（极大团）。"""

    group_index: int = Field(..., description="分组序号（从 1 开始）")
    size: int = Field(..., description="分组人数（至少为 2）")
    instruments: List[str] = Field(
        ...,
        description="组内仪器编码（字典序升序）；分组可重叠，组间为极大团而非连通分量",
    )
    members: List[CompatibilityGroupMember] = Field(
        ..., description="组内各仪器的读数与不确定度（与 instruments 同序）"
    )


class MeasurementCompatibilityReport(BaseModel):
    at: datetime
    dry_run: bool = Field(
        ..., description="恒为 true：相容性评估为只读计算，不写入业务数据"
    )
    target_unit: str = Field(..., description="请求的目标单位")
    readings: List[MeasurementReadingResult] = Field(
        ..., description="逐条读数的参与结果（按请求顺序）"
    )
    requested_reading_count: int
    participant_count: int
    nonparticipant_count: int
    participants: List[str] = Field(
        default_factory=list,
        description="实际参与计算的仪器编码（字典序升序）",
    )
    nonparticipants: List[MeasurementNonparticipant] = Field(
        default_factory=list,
        description="逐条返回的不可参与读数及原因（按请求中的读数顺序）",
    )
    pair_count: int = Field(..., description="参与读数两两组合数 C(n,2)")
    pairs: List[CompatibilityPair] = Field(
        default_factory=list,
        description="全部两两 En 结果（仪器编码按字典序稳定排序）",
    )
    compatible_pair_count: int
    incompatible_pair_count: int
    group_count: int
    groups: List[CompatibilityGroup] = Field(
        default_factory=list,
        description=(
            "人数至少为 2 的全部极大两两相容分组（Bron–Kerbosch 极大团枚举，"
            "非连通分量），按组内仪器编码元组字典序稳定排序"
        ),
    )
    reason_code: Optional[str] = Field(
        None,
        description=(
            "可参与读数不足 2 个时为 INSUFFICIENT_PARTICIPANTS；正常完成时为 null"
        ),
    )
    reason: Optional[str] = Field(None, description="中文原因说明")
    statistics: dict = Field(default_factory=dict)
