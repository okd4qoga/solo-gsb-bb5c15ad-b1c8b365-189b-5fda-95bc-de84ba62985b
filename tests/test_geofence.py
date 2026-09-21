"""围栏点面归属算法单元测试。"""
import pytest

from app.geofence import (
    assign_zones,
    build_zone,
    summarize_zones,
    zone_contains,
)
from app.staypoint import StaySegment
from datetime import datetime, timedelta, timezone

BASE = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)

# 以 (0, 0) 为中心、经纬度各 0.02 度的方形围栏
BOX = {
    "type": "Polygon",
    "coordinates": [
        [
            [-0.01, -0.01],
            [0.01, -0.01],
            [0.01, 0.01],
            [-0.01, 0.01],
            [-0.01, -0.01],
        ]
    ],
}

# 跨 180° 经线的围栏：170~-170，纬度 ±0.01；顶点顺序两种写法均需支持
DATELINE_E2W = {
    "type": "Polygon",
    "coordinates": [
        [
            [179.99, -0.01],
            [-179.99, -0.01],
            [-179.99, 0.01],
            [179.99, 0.01],
            [179.99, -0.01],
        ]
    ],
}
DATELINE_W2E = {
    "type": "Polygon",
    "coordinates": [
        [
            [-179.99, -0.01],
            [179.99, -0.01],
            [179.99, 0.01],
            [-179.99, 0.01],
            [-179.99, -0.01],
        ]
    ],
}


def zone(zone_id, geometry, priority=0):
    return build_zone(zone_id, priority, geometry["type"], geometry["coordinates"])


def test_basic_inside_outside():
    z = zone("z", BOX)
    assert zone_contains(z, 0.0, 0.0)
    assert zone_contains(z, 0.005, -0.005)
    # 外部（远离围栏）
    assert not zone_contains(z, 10.0, 10.0)
    assert not zone_contains(z, 0.02, 0.0)
    assert not zone_contains(z, -0.02, 0.0)


def test_outer_ring_boundary_is_hit():
    z = zone("z", BOX)
    # 底边、顶边、左边、右边及顶点上的点均命中
    assert zone_contains(z, -0.01, 0.0)
    assert zone_contains(z, 0.01, 0.005)
    assert zone_contains(z, 0.005, 0.01)
    assert zone_contains(z, -0.005, -0.01)
    assert zone_contains(z, 0.01, 0.01)


def test_hole_interior_and_hole_boundary_are_miss():
    # 0.02 方框内挖掉以原点为中心、边长 0.006 的洞
    donut = {
        "type": "Polygon",
        "coordinates": [
            BOX["coordinates"][0],
            [
                [-0.003, -0.003],
                [0.003, -0.003],
                [0.003, 0.003],
                [-0.003, 0.003],
                [-0.003, -0.003],
            ],
        ],
    }
    z = zone("z", donut)
    # 环带内仍命中
    assert zone_contains(z, 0.008, 0.008)
    # 洞内未命中
    assert not zone_contains(z, 0.0, 0.0)
    # 洞边界也未命中（即使几何上该点也在外环内部）
    assert not zone_contains(z, 0.003, 0.0)
    assert not zone_contains(z, 0.0, -0.003)
    # 外环边界仍命中
    assert zone_contains(z, 0.0, -0.01)


def test_dateline_crossing_both_vertex_orders():
    for geom in (DATELINE_E2W, DATELINE_W2E):
        z = zone("z", geom)
        # 经线两侧的内部点
        assert zone_contains(z, 0.0, 179.995)
        assert zone_contains(z, 0.0, -179.995)
        # 对面经度上的点必须在外部（不会被 180° 跳变误判为内部）
        assert not zone_contains(z, 0.0, 0.0)
        assert not zone_contains(z, 0.0, 90.0)
        assert not zone_contains(z, 0.0, -90.0)
        # 跨线外侧
        assert not zone_contains(z, 0.0, 179.9)
        assert not zone_contains(z, 0.0, -179.9)
        # 外环边界命中
        assert zone_contains(z, 0.0, 179.99)
        assert zone_contains(z, 0.0, -179.99)
        assert zone_contains(z, 0.01, 180.0)


def test_multipolygon_any_member_hits():
    multi = {
        "type": "MultiPolygon",
        "coordinates": [
            BOX["coordinates"],
            DATELINE_E2W["coordinates"],
        ],
    }
    z = zone("z", multi)
    assert zone_contains(z, 0.0, 0.0)
    assert zone_contains(z, 0.0, 179.995)
    assert not zone_contains(z, 50.0, 50.0)


def test_dateline_multipolygon_member_with_hole():
    # 跨线多边形内，180 经线处挖洞
    donut = {
        "type": "MultiPolygon",
        "coordinates": [
            [
                DATELINE_E2W["coordinates"][0],
                [
                    [179.999, -0.001],
                    [-179.999, -0.001],
                    [-179.999, 0.001],
                    [179.999, 0.001],
                    [179.999, -0.001],
                ],
            ]
        ],
    }
    z = zone("z", donut)
    # 环带命中
    assert zone_contains(z, 0.005, 180.0)
    # 洞内、洞边界均未命中
    assert not zone_contains(z, 0.0, 180.0)
    assert not zone_contains(z, 0.001, 180.0)


def test_priority_desc_zone_id_asc_tiebreak():
    low = zone("z-low", BOX, priority=1)
    high_a = zone("aaa", BOX, priority=10)
    high_b = zone("bbb", BOX, priority=10)
    same = zone("zzz", BOX, priority=1)

    segments = [
        StaySegment(0, 0, BASE, BASE + timedelta(seconds=300), 0.0, 0.0),
    ]
    assert assign_zones(segments, [low, high_b, high_a, same]) == ["aaa"]
    # priority 优先于 zone_id 字典序
    assert assign_zones(segments, [high_b, low]) == ["bbb"]
    # 同 priority 取 zone_id 字典序
    assert assign_zones(segments, [same, low]) == ["z-low"]


def test_assignment_and_summary():
    z1 = zone("z1", BOX, priority=5)
    z2 = zone("z2", BOX, priority=1)

    def seg(lat, lon, duration_s):
        return StaySegment(
            0, 0, BASE, BASE + timedelta(seconds=duration_s), lat, lon
        )

    segments = [
        seg(0.0, 0.0, 300.0),     # 同时命中 z1/z2 -> z1
        seg(0.005, 0.005, 120.0),  # -> z1
        seg(50.0, 50.0, 60.0),    # 未命中
    ]
    assignment = assign_zones(segments, [z1, z2])
    assert assignment == ["z1", "z1", None]

    summary = summarize_zones([z1, z2], segments, assignment)
    assert summary == [("z1", 2, 420.0), ("z2", 0, 0.0)]
    # 无围栏时全部未命中
    assert assign_zones(segments, []) == [None, None, None]


def test_no_segments():
    z = zone("z", BOX)
    assert assign_zones([], [z]) == []
    assert summarize_zones([z], [], []) == [("z", 0, 0.0)]
