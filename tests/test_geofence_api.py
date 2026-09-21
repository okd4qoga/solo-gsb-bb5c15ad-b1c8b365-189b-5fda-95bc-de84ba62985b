"""/api/v1/geofence-visits 接口集成测试。"""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

BASE = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)

HOME = (39.900000, 116.400000)
FAR = (39.902000, 116.400000)
WORK = (39.990000, 116.400000)


def box(lon_min, lat_min, lon_max, lat_max):
    """轴对齐矩形 Polygon（GeoJSON 坐标 [lon, lat]）。"""
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [lon_min, lat_min],
                [lon_max, lat_min],
                [lon_max, lat_max],
                [lon_min, lat_max],
                [lon_min, lat_min],
            ]
        ],
    }


def visit(lat, lon, start_s):
    """一次 180s 的停留：4 个点，间隔 60s，坐标小幅抖动（中心恰为 lat/lon）。"""
    return [
        {
            "lat": lat + dlat,
            "lon": lon,
            "timestamp": (BASE + timedelta(seconds=start_s + i * 60)).isoformat(),
        }
        for i, dlat in enumerate([0.0, 0.00001, -0.00001, 0.0])
    ]


POINTS = (
    visit(*HOME, start_s=0)
    + visit(*FAR, start_s=3000)
    + visit(*WORK, start_s=6000)
)


def build_payload(points, geofences, **overrides):
    payload = {
        "points": points,
        "distance_threshold_m": 100,
        "min_stay_duration_s": 180,
        "max_gap_s": 600,
        "geofences": geofences,
    }
    payload.update(overrides)
    return payload


# 三个围栏：home/far 重叠，home 优先级更高；work 独立；unused 无人命中
GEOFENCES = [
    {
        "zone_id": "zone_home",
        "priority": 5,
        "geometry": box(116.3999, 39.8999, 116.4001, 39.9001),
    },
    {
        "zone_id": "zone_chain",
        "priority": 1,
        "geometry": {
            "type": "MultiPolygon",
            "coordinates": [
                box(116.3999, 39.8999, 116.4001, 39.9001)["coordinates"],
                box(116.3999, 39.9019, 116.4001, 39.9021)["coordinates"],
            ],
        },
    },
    {
        "zone_id": "zone_work",
        "priority": 3,
        "geometry": box(116.3999, 39.9899, 116.4001, 39.9901),
    },
    {
        "zone_id": "zone_unused",
        "priority": 0,
        "geometry": box(0.0, 0.0, 0.001, 0.001),
    },
]


def test_geofence_visits_assignment_and_summary():
    resp = client.post(
        "/api/v1/geofence-visits", json=build_payload(POINTS, GEOFENCES)
    )
    assert resp.status_code == 200
    body = resp.json()

    assert len(body["stays"]) == 3
    assert [s["stay_id"] for s in body["stays"]] == [1, 2, 3]
    # HOME 同时命中 zone_home(5) 与 zone_chain(1) -> 高优先级；
    # FAR 只命中 zone_chain；WORK 命中 zone_work
    assert [s["zone_id"] for s in body["stays"]] == [
        "zone_home",
        "zone_chain",
        "zone_work",
    ]
    first = body["stays"][0]
    assert first["duration_s"] == 180.0
    assert (first["start_index"], first["end_index"]) == (0, 3)

    # 汇总按请求围栏顺序；未命中的 zone_unused 保留且为 0
    zones = {z["zone_id"]: z for z in body["zones"]}
    assert set(zones) == {"zone_home", "zone_chain", "zone_work", "zone_unused"}
    assert zones["zone_home"]["visit_count"] == 1
    assert zones["zone_home"]["total_duration_s"] == 180.0
    assert zones["zone_chain"]["visit_count"] == 1
    assert zones["zone_chain"]["total_duration_s"] == 180.0
    assert zones["zone_work"]["visit_count"] == 1
    assert zones["zone_unused"]["visit_count"] == 0
    assert zones["zone_unused"]["total_duration_s"] == 0.0
    # zones 数组顺序与请求一致
    assert [z["zone_id"] for z in body["zones"]] == [
        "zone_home",
        "zone_chain",
        "zone_work",
        "zone_unused",
    ]

    stats = body["stats"]
    assert stats["stay_count"] == 3
    assert stats["total_stay_duration_s"] == 540.0


def test_zone_id_lexicographic_tiebreak():
    # priority 相同：zone_id 字典序升序决胜
    geofences = [
        {"zone_id": "zzz", "priority": 2, "geometry": GEOFENCES[0]["geometry"]},
        {"zone_id": "aaa", "priority": 2, "geometry": GEOFENCES[0]["geometry"]},
    ]
    resp = client.post(
        "/api/v1/geofence-visits", json=build_payload(POINTS[:4], geofences)
    )
    assert resp.status_code == 200
    assert resp.json()["stays"][0]["zone_id"] == "aaa"


def test_outer_boundary_hit_hole_boundary_miss():
    # 停留中心精确落在 HOME 围栏外环底边（lat=39.8999）
    boundary_points = visit(39.8999, 116.400000, start_s=0)
    geofence = {
        "zone_id": "edge",
        "priority": 0,
        "geometry": box(116.3999, 39.8999, 116.4001, 39.9001),
    }
    resp = client.post(
        "/api/v1/geofence-visits",
        json=build_payload(boundary_points, [geofence]),
    )
    assert resp.status_code == 200
    assert resp.json()["stays"][0]["zone_id"] == "edge"

    # 中心落在洞内：外环大方框 + 以中心为洞边的小方框
    outer = box(116.3990, 39.8990, 116.4010, 39.9010)
    hole = box(116.3999, 39.8999, 116.4001, 39.9001)
    donut = {
        "zone_id": "donut",
        "priority": 0,
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                outer["coordinates"][0],
                hole["coordinates"][0],
            ],
        },
    }
    resp = client.post(
        "/api/v1/geofence-visits",
        json=build_payload(boundary_points, [donut]),
    )
    assert resp.status_code == 200
    # 点在洞边界上 -> 未命中
    assert resp.json()["stays"][0]["zone_id"] is None


def test_dateline_crossing_geofence():
    def dateline_box():
        return {
            "type": "Polygon",
            "coordinates": [
                [
                    [179.999, -0.0001],
                    [-179.999, -0.0001],
                    [-179.999, 0.0001],
                    [179.999, 0.0001],
                    [179.999, -0.0001],
                ]
            ],
        }

    # 中心在 179.9995 的停留命中；对面经度的停留不命中
    hit_points = visit(0.0, 179.9995, start_s=0)
    far_points = visit(0.0, 0.0, start_s=3000)
    geofence = {"zone_id": "idl", "priority": 0, "geometry": dateline_box()}
    resp = client.post(
        "/api/v1/geofence-visits",
        json=build_payload(hit_points + far_points, [geofence]),
    )
    assert resp.status_code == 200
    stays = resp.json()["stays"]
    assert [s["zone_id"] for s in stays] == ["idl", None]


def test_empty_geofences_and_no_stays():
    # 空围栏列表：全部停留 zone_id 为 null，zones 为空
    resp = client.post(
        "/api/v1/geofence-visits", json=build_payload(POINTS, [])
    )
    assert resp.status_code == 200
    body = resp.json()
    assert all(s["zone_id"] is None for s in body["stays"])
    assert body["zones"] == []

    # 识别不出停留：stays/zones 汇总均为 0，stats.stay_count=0
    payload = build_payload(
        [{"lat": 39.9, "lon": 116.4, "timestamp": BASE.isoformat()}],
        GEOFENCES,
        min_stay_duration_s=300,
    )
    resp = client.post("/api/v1/geofence-visits", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["stays"] == []
    assert all(z["visit_count"] == 0 for z in body["zones"])
    assert body["stats"]["stay_count"] == 0


def test_validation_errors():
    # 缺少 geofences
    bad = {"points": visit(*HOME, start_s=0), "min_stay_duration_s": 180}
    assert client.post("/api/v1/geofence-visits", json=bad).status_code == 422

    # 不支持的几何类型
    bad_geom = [
        {
            "zone_id": "z",
            "priority": 0,
            "geometry": {"type": "Point", "coordinates": [116.4, 39.9]},
        }
    ]
    assert (
        client.post(
            "/api/v1/geofence-visits", json=build_payload(POINTS[:4], bad_geom)
        ).status_code
        == 422
    )

    # 环未闭合
    unclosed = [
        {
            "zone_id": "z",
            "priority": 0,
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [0.0, 0.0],
                        [0.01, 0.0],
                        [0.01, 0.01],
                        [0.0, 0.01],
                    ]
                ],
            },
        }
    ]
    assert (
        client.post(
            "/api/v1/geofence-visits", json=build_payload(POINTS[:4], unclosed)
        ).status_code
        == 422
    )

    # zone_id 重复
    dup = [
        {"zone_id": "same", "priority": 0, "geometry": box(0, 0, 1, 1)},
        {"zone_id": "same", "priority": 1, "geometry": box(0, 0, 1, 1)},
    ]
    assert (
        client.post(
            "/api/v1/geofence-visits", json=build_payload(POINTS[:4], dup)
        ).status_code
        == 422
    )

    # 沿用的停留参数校验仍然生效
    bad_track = [{"lat": 91, "lon": 0, "timestamp": 0}]
    assert (
        client.post(
            "/api/v1/geofence-visits", json=build_payload(bad_track, [])
        ).status_code
        == 422
    )
