"""批次测量值相容性评估（POST /api/assessments/measurement-compatibility）测试。"""
import pytest
from fastapi.testclient import TestClient

from app.engine import maximal_cliques

AT = "2026-06-15T00:00:00Z"
URL = "/api/assessments/measurement-compatibility"


def _inst(code, name=None, root=False):
    return {"code": code, "name": name or code, "is_national_standard": root}


def _cert(cid, inst, issued="2026-01-01T00:00:00",
          vf="2026-01-01T00:00:00", vu="2027-01-01T00:00:00",
          u=0.01, unit="mm", digest=None):
    data = {
        "id": cid, "instrument_code": inst, "cert_number": cid,
        "issued_at": issued, "valid_from": vf, "valid_until": vu,
        "uncertainty": u, "unit": unit,
    }
    if digest is not None:
        data["signature_digest"] = digest
    return data


def _submit(client, instruments, certificates, edges):
    r = client.post(
        "/api/submissions",
        json={"instruments": instruments,
              "certificates": certificates, "edges": edges},
    )
    assert r.status_code == 201, r.text


def _chain(client, leaf, leaf_u=0.02, unit="mm", digest=None):
    """提交一条 CG <- CR(leaf) <- leaf 的唯一有效链（幂等复用 CG/CR 已存在的情形由调用方避免）。"""
    certs = [
        _cert(f"CG-{leaf}", "G", issued="2024-01-01T00:00:00",
              vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00",
              u=0.0001, unit=unit),
        _cert(f"CR-{leaf}", f"R-{leaf}", issued="2025-06-01T00:00:00",
              vf="2025-06-01T00:00:00", vu="2027-06-01T00:00:00",
              u=0.001, unit=unit),
        _cert(f"CW-{leaf}", leaf, u=leaf_u, unit=unit, digest=digest),
    ]
    edges = [
        {"child_cert_id": f"CW-{leaf}", "parent_cert_id": f"CR-{leaf}"},
        {"child_cert_id": f"CR-{leaf}", "parent_cert_id": f"CG-{leaf}"},
    ]
    _submit(client, [_inst("G", root=True), _inst(f"R-{leaf}"), _inst(leaf)],
            certs, edges)


def _shared_chain_leaves(client, leaves, leaf_us=None, unit="mm", root="G"):
    """所有叶子共用同一基准 G、各自独立标准 Ri 的链。"""
    leaf_us = leaf_us or {}
    instruments = [_inst(root, root=True)]
    certs = [
        _cert("CG", root, issued="2024-01-01T00:00:00",
              vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00",
              u=0.0001, unit=unit),
    ]
    edges = []
    for leaf in leaves:
        instruments.append(_inst(f"R-{leaf}"))
        instruments.append(_inst(leaf))
        certs.append(
            _cert(f"CR-{leaf}", f"R-{leaf}", issued="2025-06-01T00:00:00",
                  vf="2025-06-01T00:00:00", vu="2027-06-01T00:00:00",
                  u=0.001, unit=unit)
        )
        certs.append(_cert(f"CW-{leaf}", leaf, u=leaf_us.get(leaf, 0.02), unit=unit))
        edges += [
            {"child_cert_id": f"CW-{leaf}", "parent_cert_id": f"CR-{leaf}"},
            {"child_cert_id": f"CR-{leaf}", "parent_cert_id": "CG"},
        ]
    _submit(client, instruments, certs, edges)


def _post(client, target_unit, pairs, at=AT):
    return client.post(
        URL,
        json={
            "at": at,
            "target_unit": target_unit,
            "readings": [
                {"instrument_code": c, "value": v} for c, v in pairs
            ],
        },
    )


def test_compatible_pair_en_and_single_group(client: TestClient):
    _shared_chain_leaves(client, ["W", "W2"])
    r = _post(client, "mm", [("W", 10.00), ("W2", 10.01)])
    assert r.status_code == 200, r.text
    d = r.json()

    assert d["dry_run"] is True
    assert d["target_unit"] == "mm"
    assert d["participants"] == ["W", "W2"]
    assert d["nonparticipants"] == []
    assert d["pair_count"] == 1
    pair = d["pairs"][0]
    assert pair["instrument_i"] == "W"
    assert pair["instrument_j"] == "W2"
    assert pair["uncertainty_i"] == 0.02
    assert pair["uncertainty_j"] == 0.02
    assert pair["en"] == pytest.approx(0.3535533905932738, rel=1e-12)
    assert pair["compatible"] is True
    assert pair["decision"] == "COMPATIBLE"
    assert d["compatible_pair_count"] == 1
    assert d["incompatible_pair_count"] == 0
    assert d["group_count"] == 1
    group = d["groups"][0]
    assert group["group_index"] == 1
    assert group["size"] == 2
    assert group["instruments"] == ["W", "W2"]
    assert [m["instrument"] for m in group["members"]] == ["W", "W2"]
    assert d["reason_code"] is None

    # readings 按请求顺序，参与仪器的 U/证书取自起点证书
    assert [row["instrument"] for row in d["readings"]] == ["W", "W2"]
    assert all(row["participated"] for row in d["readings"])
    assert d["readings"][0]["start_certificate_id"] == "CW-W"
    assert d["readings"][0]["uncertainty"] == 0.02


def test_incompatible_pair_no_group(client: TestClient):
    _shared_chain_leaves(client, ["W", "W2"])
    d = _post(client, "mm", [("W", 10.00), ("W2", 10.10)]).json()
    pair = d["pairs"][0]
    assert pair["en"] > 1.0
    assert pair["compatible"] is False
    assert pair["decision"] == "INCOMPATIBLE"
    assert d["groups"] == []
    assert d["group_count"] == 0
    # 仍有 2 个参与者，只是互不相容：不属于“参与不足”
    assert d["reason_code"] is None
    assert d["compatible_pair_count"] == 0
    assert d["incompatible_pair_count"] == 1


def test_en_boundary_equal_one_is_compatible(client: TestClient):
    """En 恰为 1（3-4-5：0.03/0.04 合成 0.05）判为相容。"""
    _shared_chain_leaves(client, ["W", "W2"], leaf_us={"W": 0.03, "W2": 0.04})
    d = _post(client, "mm", [("W", 10.00), ("W2", 10.05)]).json()
    pair = d["pairs"][0]
    assert pair["en"] == pytest.approx(1.0, abs=1e-12)
    assert pair["compatible"] is True


def test_overlapping_maximal_groups_not_connected_component(client: TestClient):
    """W-W2-W3 路径相容图：极大团为 [W,W2] 与 [W2,W3]，而非连通分量 [W,W2,W3]。"""
    _shared_chain_leaves(client, ["W", "W2", "W3"])
    # 等 U=0.02 时相容阈值 dx<=0.02*sqrt2≈0.0283
    d = _post(
        client, "mm",
        [("W", 10.00), ("W2", 10.01), ("W3", 10.03)],
    ).json()
    by_pair = {
        (p["instrument_i"], p["instrument_j"]): p for p in d["pairs"]
    }
    assert by_pair[("W", "W2")]["compatible"] is True
    assert by_pair[("W2", "W3")]["compatible"] is True
    assert by_pair[("W", "W3")]["compatible"] is False
    groups = [tuple(g["instruments"]) for g in d["groups"]]
    assert groups == [("W", "W2"), ("W2", "W3")]
    # 分组可重叠：W2 同时出现在两个极大团中
    assert sum("W2" in g for g in groups) == 2
    for g in d["groups"]:
        assert g["size"] == len(g["instruments"]) == 2
        assert all(m["group_index"] == g["group_index"] for m in g["members"])


def test_two_overlapping_maximal_groups_triangle_and_edge(client: TestClient):
    """三角形团 [W,W2,W3] + 仅与 W 相容的 W4：两个极大团，不能合并为连通分量。"""
    _shared_chain_leaves(client, ["W", "W2", "W3", "W4"])
    # 等 U=0.02 相容阈值 0.02*sqrt2≈0.02828：
    # W/W2/W3 两两差 0.005/0.010 全相容；
    # W4=9.975 与 W 差 0.025 相容，与 W2/W3 差 0.030/0.035 均不相容。
    d = _post(
        client, "mm",
        [("W", 10.000), ("W2", 10.005), ("W3", 10.010), ("W4", 9.975)],
    ).json()
    by_pair = {
        (p["instrument_i"], p["instrument_j"]): p["compatible"]
        for p in d["pairs"]
    }
    assert by_pair[("W", "W2")] is True
    assert by_pair[("W", "W3")] is True
    assert by_pair[("W2", "W3")] is True
    assert by_pair[("W", "W4")] is True
    assert by_pair[("W2", "W4")] is False
    assert by_pair[("W3", "W4")] is False
    groups = [tuple(g["instruments"]) for g in d["groups"]]
    assert groups == [("W", "W2", "W3"), ("W", "W4")]
    sizes = {tuple(g["instruments"]): g["size"] for g in d["groups"]}
    assert sizes[("W", "W2", "W3")] == 3
    assert sizes[("W", "W4")] == 2
    # W 同时属于两个重叠分组；连通分量会错误地给出单个 4 人组
    assert sum("W" in g for g in groups) == 2


def test_both_uncertainties_zero(client: TestClient):
    """两 U 均为 0：En=null；仅测量值相等才相容。"""
    # 零不确定度链：根/标准/叶子均为 0（仅 WARN 不阻断 valid）
    _submit(
        client,
        [_inst("GZ", root=True), _inst("RZ"), _inst("ZA"), _inst("ZB")],
        [
            _cert("CGZ", "GZ", issued="2024-01-01T00:00:00",
                  vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00",
                  u=0.0),
            _cert("CRZ", "RZ", issued="2025-06-01T00:00:00",
                  vf="2025-06-01T00:00:00", vu="2027-06-01T00:00:00",
                  u=0.0),
            _cert("CZA", "ZA", u=0.0),
            _cert("CZB", "ZB", u=0.0),
        ],
        [
            {"child_cert_id": "CZA", "parent_cert_id": "CRZ"},
            {"child_cert_id": "CZB", "parent_cert_id": "CRZ"},
            {"child_cert_id": "CRZ", "parent_cert_id": "CGZ"},
        ],
    )
    d = _post(client, "mm", [("ZA", 5.0), ("ZB", 5.0)]).json()
    pair = d["pairs"][0]
    assert pair["en"] is None
    assert pair["compatible"] is True
    assert pair["decision"] == "BOTH_UNCERTAINTY_ZERO_EQUAL"
    assert [tuple(g["instruments"]) for g in d["groups"]] == [("ZA", "ZB")]

    d = _post(client, "mm", [("ZA", 5.0), ("ZB", 5.1)]).json()
    pair = d["pairs"][0]
    assert pair["en"] is None
    assert pair["compatible"] is False
    assert pair["decision"] == "BOTH_UNCERTAINTY_ZERO_UNEQUAL"
    assert d["groups"] == []


def test_one_side_zero_uncertainty_en_computed(client: TestClient):
    """仅一侧 U=0（两链各自的上级 U 也为 0，保证沿链不收缩）：En 正常计算。"""
    _submit(
        client,
        [_inst("GZ", root=True), _inst("RZ"), _inst("R2"), _inst("ZA"), _inst("ZB")],
        [
            _cert("CGZ", "GZ", issued="2024-01-01T00:00:00",
                  vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00",
                  u=0.0),
            _cert("CRZ", "RZ", issued="2025-06-01T00:00:00",
                  vf="2025-06-01T00:00:00", vu="2027-06-01T00:00:00",
                  u=0.0),
            _cert("CR2", "R2", issued="2025-07-01T00:00:00",
                  vf="2025-07-01T00:00:00", vu="2027-07-01T00:00:00",
                  u=0.01),
            _cert("CZA", "ZA", u=0.0),
            _cert("CZB", "ZB", u=0.02),
        ],
        [
            {"child_cert_id": "CZA", "parent_cert_id": "CRZ"},
            {"child_cert_id": "CZB", "parent_cert_id": "CR2"},
            {"child_cert_id": "CRZ", "parent_cert_id": "CGZ"},
            {"child_cert_id": "CR2", "parent_cert_id": "CGZ"},
        ],
    )
    d = _post(client, "mm", [("ZA", 10.00), ("ZB", 10.01)]).json()
    assert d["participants"] == ["ZA", "ZB"]
    pair = d["pairs"][0]
    assert pair["uncertainty_i"] == 0.0
    assert pair["uncertainty_j"] == 0.02
    assert pair["en"] == pytest.approx(0.5)
    assert pair["compatible"] is True


def test_pair_ordering_stable_when_input_reversed(client: TestClient):
    _shared_chain_leaves(client, ["W", "W2", "W3"])
    d = _post(
        client, "mm",
        [("W3", 10.00), ("W", 10.00), ("W2", 10.00)],
    ).json()
    # readings 保持请求顺序；participants / pairs / groups 按仪器编码稳定排序
    assert [row["instrument"] for row in d["readings"]] == ["W3", "W", "W2"]
    assert d["participants"] == ["W", "W2", "W3"]
    assert [(p["instrument_i"], p["instrument_j"]) for p in d["pairs"]] == [
        ("W", "W2"), ("W", "W3"), ("W2", "W3"),
    ]
    assert d["groups"] == [
        d["groups"][0],
    ]
    assert tuple(d["groups"][0]["instruments"]) == ("W", "W2", "W3")


def test_chain_unavailable_readings_returned_per_reading(client: TestClient):
    # W：唯一有效链；T：死路断裂；NOPE：仪器不存在
    _shared_chain_leaves(client, ["W"])
    _submit(
        client,
        [_inst("TP"), _inst("T")],
        [
            _cert("CTP", "TP", u=0.05, unit="C"),
            _cert("CT", "T", u=0.5, unit="C"),
        ],
        [{"child_cert_id": "CT", "parent_cert_id": "CTP"}],
    )
    d = _post(
        client, "mm",
        [("W", 10.0), ("T", 20.0), ("NOPE", 1.0)],
    ).json()
    assert d["participants"] == ["W"]
    assert d["pairs"] == []
    assert d["groups"] == []
    assert d["reason_code"] == "INSUFFICIENT_PARTICIPANTS"
    reasons = {row["instrument"]: row for row in d["nonparticipants"]}
    assert reasons["T"]["reason_code"] == "CHAIN_BROKEN"
    assert reasons["T"]["resolution"] == "broken"
    assert reasons["T"]["conflicts"]
    assert reasons["NOPE"]["reason_code"] == "INSTRUMENT_NOT_FOUND"
    assert reasons["NOPE"]["resolution"] is None
    # 不参与读数仍逐条给出测量值
    assert reasons["T"]["value"] == 20.0
    # readings 与 nonparticipants 均按请求顺序
    assert [row["instrument"] for row in d["readings"]] == ["W", "T", "NOPE"]
    assert [row["instrument"] for row in d["nonparticipants"]] == ["T", "NOPE"]


def test_ambiguous_chain_not_chosen(client: TestClient):
    """多候选仪器不得任取一条链参与。"""
    _submit(
        client,
        [_inst("G", root=True), _inst("R"), _inst("R2"), _inst("W"), _inst("W2")],
        [
            _cert("CG", "G", issued="2024-01-01T00:00:00",
                  vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00", u=0.0001),
            _cert("CR", "R", issued="2025-06-01T00:00:00",
                  vf="2025-06-01T00:00:00", vu="2027-06-01T00:00:00", u=0.001),
            _cert("CR2", "R2", issued="2025-07-01T00:00:00",
                  vf="2025-07-01T00:00:00", vu="2027-07-01T00:00:00", u=0.002),
            _cert("CW", "W"),
            _cert("CW2", "W2", u=0.02),
        ],
        [
            {"child_cert_id": "CW", "parent_cert_id": "CR"},
            {"child_cert_id": "CW", "parent_cert_id": "CR2"},
            {"child_cert_id": "CR", "parent_cert_id": "CG"},
            {"child_cert_id": "CR2", "parent_cert_id": "CG"},
            {"child_cert_id": "CW2", "parent_cert_id": "CG"},
        ],
    )
    d = _post(client, "mm", [("W", 10.0), ("W2", 10.0)]).json()
    assert d["participants"] == ["W2"]
    np_ = d["nonparticipants"][0]
    assert np_["instrument"] == "W"
    assert np_["reason_code"] == "MULTIPLE_VALID_CHAINS"
    assert np_["uncertainty"] is None


def test_certificate_unit_mismatch_excluded(client: TestClient):
    """起点证书单位与目标单位不符：逐条返回原因，不参与计算（不做单位换算）。"""
    _shared_chain_leaves(client, ["W", "W2"])
    d = _post(client, "kPa", [("W", 10.0), ("W2", 10.0)]).json()
    assert d["participants"] == []
    assert {row["instrument"] for row in d["nonparticipants"]} == {"W", "W2"}
    for row in d["nonparticipants"]:
        assert row["reason_code"] == "CERTIFICATE_UNIT_MISMATCH"
        assert row["certificate_unit"] == "mm"
        assert row["uncertainty"] == 0.02
        assert row["start_certificate_id"].startswith("CW-")
        assert "kPa" in row["reason"] and "mm" in row["reason"]
    assert d["pairs"] == []
    assert d["groups"] == []
    assert d["reason_code"] == "INSUFFICIENT_PARTICIPANTS"


def test_unit_mismatch_reading_excluded_but_others_pair(client: TestClient):
    """单位不符的读数剔除后，其余同单位读数照常两两计算。"""
    _shared_chain_leaves(client, ["W", "W2"])
    # 温度链（单位 C）
    _chain(client, "TC", leaf_u=0.5, unit="C")
    d = _post(
        client, "mm",
        [("TC", 100.0), ("W", 10.0), ("W2", 10.01)],
    ).json()
    assert d["participants"] == ["W", "W2"]
    reasons = {r["instrument"]: r["reason_code"] for r in d["nonparticipants"]}
    assert reasons == {"TC": "CERTIFICATE_UNIT_MISMATCH"}
    assert len(d["pairs"]) == 1
    assert d["pairs"][0]["instrument_i"] == "W"


def test_request_validation_422(client: TestClient):
    base = {"at": AT, "target_unit": "mm"}

    def code422(payload):
        r = client.post(URL, json=payload)
        assert r.status_code == 422, r.text

    code422({**base, "readings": [{"instrument_code": "W", "value": 1.0}]})
    code422({
        **base,
        "readings": [
            {"instrument_code": f"W{i}", "value": 1.0} for i in range(13)
        ],
    })
    code422({**base, "readings": [
        {"instrument_code": "W", "value": 1.0},
        {"instrument_code": "W", "value": 2.0},
    ]})
    code422({**base, "readings": [
        {"instrument_code": "W", "value": 1.0},
        {"instrument_code": "  ", "value": 2.0},
    ]})
    code422({"at": AT, "target_unit": "   ", "readings": [
        {"instrument_code": "W", "value": 1.0},
        {"instrument_code": "W2", "value": 2.0},
    ]})
    code422({"at": AT, "target_unit": "mm"})


def test_non_finite_values_rejected_by_server(client: TestClient):
    """服务端必须拒绝 NaN/Infinity（绕过客户端序列化，发送原始 JSON 文本）。"""
    for literal in ("NaN", "Infinity", "-Infinity", "1e999"):
        body = (
            '{"at": "2026-06-15T00:00:00Z", "target_unit": "mm", '
            f'"readings": [{{"instrument_code": "W", "value": {literal}}}, '
            '{"instrument_code": "W2", "value": 2.0}]}'
        )
        r = client.post(URL, content=body,
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 422, (literal, r.text)


def test_does_not_write_business_data(client: TestClient):
    _shared_chain_leaves(client, ["W", "W2"])

    def counts():
        return (
            client.get("/api/certificates").json().__len__(),
            client.get("/api/edges").json().__len__(),
            client.get("/api/instruments").json().__len__(),
        )

    before = counts()
    r = _post(client, "mm", [("W", 10.0), ("W2", 10.1), ("NOPE", 1.0)])
    assert r.status_code == 200
    assert r.json()["dry_run"] is True
    assert counts() == before


def test_maximal_cliques_helper():
    # 路径图 a-b-c：两个大小为 2 的极大团，而不是一个连通分量
    adj = {"a": {"b"}, "b": {"a", "c"}, "c": {"b"}}
    assert maximal_cliques(adj) == [("a", "b"), ("b", "c")]

    # 完全图 K4：唯一极大团
    k4 = ["a", "b", "c", "d"]
    adj4 = {v: set(k4) - {v} for v in k4}
    assert maximal_cliques(adj4) == [("a", "b", "c", "d")]

    # 空图；孤立节点本身是大小 1 的极大团（相容性评估再过滤 size<2）
    assert maximal_cliques({}) == []
    assert maximal_cliques({"a": set(), "b": set()}) == [("a",), ("b",)]

    # 两个不相交三角形：各自独立成极大团
    adj = {
        "a": {"b", "c"}, "b": {"a", "c"}, "c": {"a", "b"},
        "x": {"y", "z"}, "y": {"x", "z"}, "z": {"x", "y"},
    }
    assert maximal_cliques(adj) == [("a", "b", "c"), ("x", "y", "z")]
