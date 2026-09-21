"""围栏点面归属核心算法（纯函数，无 Web 依赖）。

在停留检测（见 ``staypoint.detect_stays``）的结果之上，以每段停留的**中心点**做点面归属：

1. **Polygon 语义**：点位于外环内部、或落在**外环边界**上 -> 命中；
   落入任一内环（洞）内部、或落在**洞边界**上 -> 未命中；
2. **MultiPolygon**：任一成员 Polygon 命中即命中；
3. **跨 180° 经线**：先把每个线性环的经度链展开为连续坐标（相邻顶点经差归一到
   ±180° 以内），再以待判点经度 ±360° 的三个副本分别做点面判定——
   无论环的顶点以 ``170/-170`` 还是 ``-170/170`` 形式给出，归属结果都一致，
   且远离围栏的点不会被误判；
4. **唯一决胜**：多个围栏同时命中时，按 ``priority`` 降序、``zone_id`` 字典序升序
   选取唯一围栏。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .staypoint import StaySegment

# 边界判定的坐标容差（度；约厘米级），抵消浮点求交误差
_EPS = 1e-9

# 展开后的线性环：((lon_unwrapped, lat), ...)，首尾点保留 GeoJSON 的闭合重复
Ring = Tuple[Tuple[float, float], ...]


@dataclass(frozen=True)
class Zone:
    """一个围栏：编号、优先级与展开经度后的多边形列表。"""

    zone_id: str
    priority: int
    # 一个多边形 = 多个环（rings[0] 为外环，其余为洞）；MultiPolygon 为多个多边形
    polygons: Tuple[Tuple[Ring, ...], ...]


def _unwrap_ring(ring: Sequence[Sequence[float]]) -> Ring:
    """把线性环的经度链展开为连续坐标。

    GeoJSON 中跨 180° 经线的环会出现相邻顶点从 170 跳到 -170（或反向）的情况。
    从首点出发逐点把经差归一到 [-180, 180]，得到一条连续的经度链，
    展开后坐标可能超出 [-180, 180]（如 190），由点的 ±360° 副本负责匹配。
    """
    positions = [(float(p[0]), float(p[1])) for p in ring]
    unwrapped_lons: List[float] = [positions[0][0]]
    for i in range(1, len(positions)):
        lon = positions[i][0]
        while lon - unwrapped_lons[-1] > 180.0:
            lon -= 360.0
        while lon - unwrapped_lons[-1] < -180.0:
            lon += 360.0
        unwrapped_lons.append(lon)
    return tuple(
        (unwrapped_lons[i], positions[i][1]) for i in range(len(positions))
    )


def build_zone(
    zone_id: str,
    priority: int,
    geometry_type: str,
    coordinates: Sequence,
) -> Zone:
    """由已校验的 GeoJSON 几何构建围栏（经度链在此一次性展开）。"""
    raw_polygons: Sequence = (
        (coordinates,) if geometry_type == "Polygon" else coordinates
    )
    polygons = tuple(
        tuple(_unwrap_ring(ring) for ring in polygon) for polygon in raw_polygons
    )
    return Zone(zone_id=zone_id, priority=priority, polygons=polygons)


def _on_segment(
    px: float,
    py: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
) -> bool:
    """点 P 是否落在线段 AB 上（叉积共线 + 包围盒判定，带容差）。"""
    cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
    scale = max(1.0, abs(bx - ax), abs(by - ay))
    if abs(cross) > _EPS * scale:
        return False
    return (
        min(ax, bx) - _EPS <= px <= max(ax, bx) + _EPS
        and min(ay, by) - _EPS <= py <= max(ay, by) + _EPS
    )


def _ring_label(lat: float, lon: float, ring: Ring) -> int:
    """单点对单个（已展开）线性环的归属：1 内部 / 0 边界 / -1 外部。

    经典射线法（ray casting）：水平向右射线与环边的交点奇偶性判内外；
    顶点采用“一端严格高于、另一端不高于”的半开约定，水平边不计入。
    """
    inside = False
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if _on_segment(lon, lat, x1, y1, x2, y2):
            return 0
        if (y1 > lat) != (y2 > lat):
            x_cross = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if x_cross > lon:
                inside = not inside
    return 1 if inside else -1


def _ring_status(lat: float, lon: float, ring: Ring) -> str:
    """点对环的归属，含跨 180° 的经度副本匹配。

    依次用 lon-360 / lon / lon+360 三个副本判定：环可能整体展开在任一经度周期，
    三个副本保证其中之一与环处于同一坐标系。边界优先于内部。
    """
    for shift in (-360.0, 0.0, 360.0):
        if _ring_label(lat, lon + shift, ring) == 0:
            return "boundary"
    for shift in (-360.0, 0.0, 360.0):
        if _ring_label(lat, lon + shift, ring) == 1:
            return "inside"
    return "outside"


def _polygon_contains(lat: float, lon: float, rings: Sequence[Ring]) -> bool:
    """多边形点面归属：外环（含边界）命中；洞内及洞边界未命中。"""
    if _ring_status(lat, lon, rings[0]) == "outside":
        return False
    for hole in rings[1:]:
        if _ring_status(lat, lon, hole) != "outside":
            return False
    return True


def zone_contains(zone: Zone, lat: float, lon: float) -> bool:
    """点是否命中围栏（MultiPolygon 任一成员命中即可）。"""
    return any(_polygon_contains(lat, lon, rings) for rings in zone.polygons)


def assign_zones(
    segments: Sequence[StaySegment], zones: Sequence[Zone]
) -> List[Optional[str]]:
    """为每段停留的中心做围栏归属。

    多围栏同时命中时按 priority 降序、zone_id 字典序升序唯一决胜；
    无命中为 ``None``。
    """
    assignment: List[Optional[str]] = []
    for seg in segments:
        winners = [
            zone
            for zone in zones
            if zone_contains(zone, seg.center_lat, seg.center_lon)
        ]
        if winners:
            winners.sort(key=lambda z: (-z.priority, z.zone_id))
            assignment.append(winners[0].zone_id)
        else:
            assignment.append(None)
    return assignment


def summarize_zones(
    zones: Sequence[Zone],
    segments: Sequence[StaySegment],
    assignment: Sequence[Optional[str]],
) -> List[Tuple[str, int, float]]:
    """按围栏（请求顺序）汇总访问次数与累计停留时长（秒）。

    未被任何停留命中的围栏也保留一行，计数与时长为 0。
    """
    visit_count: Dict[str, int] = {zone.zone_id: 0 for zone in zones}
    total_duration: Dict[str, float] = {zone.zone_id: 0.0 for zone in zones}
    for seg, zone_id in zip(segments, assignment):
        if zone_id is not None:
            visit_count[zone_id] += 1
            total_duration[zone_id] += seg.duration_s
    return [
        (zone.zone_id, visit_count[zone.zone_id], round(total_duration[zone.zone_id], 3))
        for zone in zones
    ]
