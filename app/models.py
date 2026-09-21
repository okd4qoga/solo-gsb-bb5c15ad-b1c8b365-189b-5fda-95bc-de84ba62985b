"""Pydantic 请求 / 响应模型。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class TrackPoint(BaseModel):
    """轨迹点：经纬度 + 时间戳。

    timestamp 支持三种形式：
    - ISO 8601 字符串，如 "2026-09-21T08:00:00Z"、"2026-09-21T08:00:00+08:00"
    - Unix 时间戳（秒，int/float），如 1758432000
    - 不带时区的 ISO 字符串按 UTC 处理
    """

    lat: float = Field(..., ge=-90, le=90, description="纬度，[-90, 90]")
    lon: float = Field(..., ge=-180, le=180, description="经度，[-180, 180]")
    timestamp: datetime = Field(..., description="ISO 8601 字符串或 Unix 秒时间戳")

    @field_validator("timestamp", mode="before")
    @classmethod
    def _parse_timestamp(cls, v):
        if isinstance(v, bool):
            raise ValueError("非法时间戳")
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v, tz=timezone.utc)
        if isinstance(v, str):
            s = v.strip()
            try:
                return datetime.fromtimestamp(float(s), tz=timezone.utc)
            except ValueError:
                pass
            if s.endswith(("Z", "z")):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        return v


class StayRequest(BaseModel):
    """停留分析请求。"""

    points: List[TrackPoint] = Field(
        ..., min_length=1, description="按时间升序排列的轨迹点"
    )
    distance_threshold_m: float = Field(
        100.0, gt=0, description="停留距离阈值（米）：连续点距锚点不超过该值则合并为同一停留"
    )
    min_stay_duration_s: float = Field(
        300.0, ge=0, description="最短停留时长（秒）：区段时间跨度达到该值才判定为停留"
    )
    max_gap_s: Optional[float] = Field(
        None,
        gt=0,
        description="最大采样间隔（秒）：相邻点间隔超过该值视为采样间断，强制切分段落；"
        "为空时默认取 4 * min_stay_duration_s",
    )


class Stay(BaseModel):
    """一个停留区段。"""

    stay_id: int = Field(..., description="停留序号，从 1 开始")
    center_lat: float = Field(..., description="停留中心纬度（区段内点均值）")
    center_lon: float = Field(..., description="停留中心经度（区段内点均值）")
    start_time: datetime = Field(..., description="进入停留区的时间（区段首点时间）")
    end_time: datetime = Field(..., description="离开停留区的时间（区段末点时间）")
    duration_s: float = Field(..., description="停留时长（秒）")
    start_index: int = Field(..., description="区段首点在请求 points 中的下标（含）")
    end_index: int = Field(..., description="区段末点在请求 points 中的下标（含）")
    point_count: int = Field(..., description="区段包含的轨迹点数")


class Stats(BaseModel):
    """整体统计。"""

    point_count: int = Field(..., description="轨迹点总数")
    total_distance_m: float = Field(..., description="总里程（米），相邻点 Haversine 距离之和")
    total_duration_s: float = Field(..., description="轨迹总时长（秒），首末点时间差")
    stay_count: int = Field(..., description="识别出的停留次数")
    total_stay_duration_s: float = Field(..., description="停留总时长（秒）")


class HotspotRequest(StayRequest):
    """热点归并请求：沿用停留检测参数，追加热点判定参数。"""

    hotspot_radius_m: float = Field(
        ...,
        gt=0,
        description="热点半径（米）：两段停留中心间 Haversine 距离不超过该值则连边",
    )
    min_visits: int = Field(
        ...,
        ge=1,
        description="最少访问次数：连通分量内停留数达到该值才判为热点，否则为噪声",
    )


class HotspotStay(Stay):
    """带热点归属的停留区段。"""

    hotspot_id: Optional[int] = Field(
        ..., description="所属热点编号（从 1 开始）；噪声停留为 null"
    )
    noise: bool = Field(..., description="是否为噪声停留（不属于任何热点）")


class Hotspot(BaseModel):
    """一个热点：中心间距离连边后达标的连通分量。"""

    hotspot_id: int = Field(..., description="热点编号，按最早访问时间从 1 开始")
    visit_count: int = Field(..., description="访问次数（分量内停留段数）")
    total_duration_s: float = Field(..., description="成员停留累计时长（秒）")
    center_lat: float = Field(..., description="热点中心纬度（按停留时长加权平均）")
    center_lon: float = Field(..., description="热点中心经度（按停留时长加权平均）")
    first_visit_time: datetime = Field(..., description="最早访问时间（最早停留进入时间）")
    last_visit_time: datetime = Field(..., description="最近离开时间（最晚停留离开时间）")
    stay_ids: List[int] = Field(..., description="成员停留的 stay_id（按编号升序）")


class HotspotResponse(BaseModel):
    stays: List[HotspotStay]
    hotspots: List[Hotspot]
    stats: Stats


class StayResponse(BaseModel):
    stays: List[Stay]
    stats: Stats


# ---------------------------------------------------------------------------
# 围栏访问（geofence visits）
# ---------------------------------------------------------------------------


class GeoJSONGeometry(BaseModel):
    """GeoJSON Polygon / MultiPolygon 几何（坐标顺序 [lon, lat]）。"""

    type: Literal["Polygon", "MultiPolygon"] = Field(
        ..., description="几何类型，仅支持 Polygon 与 MultiPolygon"
    )
    coordinates: Any = Field(
        ...,
        description=(
            "Polygon: [[[lon, lat], ...]]（首个环为外环，其余为洞）；"
            "MultiPolygon: [[[[lon, lat], ...]]]"
        ),
    )

    @model_validator(mode="after")
    def _validate_coordinates(self) -> "GeoJSONGeometry":
        coords = self.coordinates
        if not isinstance(coords, list) or len(coords) == 0:
            raise ValueError("coordinates 必须为非空数组")

        polygons = coords if self.type == "MultiPolygon" else [coords]
        if self.type == "Polygon":
            if not all(isinstance(ring, list) for ring in coords):
                raise ValueError("Polygon coordinates 必须为线性环数组")
        else:
            if not all(isinstance(polygon, list) and len(polygon) > 0 for polygon in polygons):
                raise ValueError("MultiPolygon coordinates 必须为非空多边形数组")

        for pi, polygon in enumerate(polygons):
            if not isinstance(polygon, list) or len(polygon) == 0:
                raise ValueError(f"多边形 {pi} 必须为非空线性环数组")
            for ri, ring in enumerate(polygon):
                self._validate_ring(ring, pi, ri)
        return self

    @staticmethod
    def _validate_ring(ring: Any, pi: int, ri: int) -> None:
        where = f"多边形 {pi} 环 {ri}"
        if not isinstance(ring, list) or len(ring) < 4:
            raise ValueError(f"{where}：线性环至少包含 4 个位置（首尾闭合）")
        for i, pos in enumerate(ring):
            if (
                not isinstance(pos, list)
                or len(pos) < 2
                or isinstance(pos[0], bool)
                or isinstance(pos[1], bool)
                or not isinstance(pos[0], (int, float))
                or not isinstance(pos[1], (int, float))
            ):
                raise ValueError(
                    f"{where} 位置 {i}：必须为 [lon, lat] 数值数组"
                )
            lon, lat = float(pos[0]), float(pos[1])
            if not -90.0 <= lat <= 90.0:
                raise ValueError(f"{where} 位置 {i}：纬度须在 [-90, 90]")
            if not -180.0 <= lon <= 180.0:
                raise ValueError(f"{where} 位置 {i}：经度须在 [-180, 180]")
        if list(ring[0])[:2] != list(ring[-1])[:2]:
            raise ValueError(f"{where}：首尾位置必须相同（线性环须闭合）")


class Geofence(BaseModel):
    """一个围栏：编号、优先级与 GeoJSON Polygon/MultiPolygon 几何。"""

    zone_id: str = Field(..., min_length=1, description="围栏唯一编号")
    priority: int = Field(
        ..., description="优先级：多围栏同时命中时数值越大越优先（可负）"
    )
    geometry: GeoJSONGeometry = Field(..., description="围栏几何（Polygon/MultiPolygon）")


class GeofenceVisitRequest(StayRequest):
    """围栏访问请求：沿用停留检测参数，追加围栏列表。"""

    geofences: List[Geofence] = Field(
        ..., description="围栏列表（zone_id 须唯一，允许空列表表示不配置围栏）"
    )

    @model_validator(mode="after")
    def _unique_zone_ids(self) -> "GeofenceVisitRequest":
        ids = [g.zone_id for g in self.geofences]
        if len(ids) != len(set(ids)):
            raise ValueError("geofences 中的 zone_id 必须唯一")
        return self


class GeofenceStay(Stay):
    """带围栏归属的停留区段。"""

    zone_id: Optional[str] = Field(
        ..., description="命中围栏编号；未命中任何围栏为 null"
    )


class GeofenceZoneSummary(BaseModel):
    """单个围栏的访问汇总（未命中的围栏也会返回，计数为 0）。"""

    zone_id: str = Field(..., description="围栏编号")
    visit_count: int = Field(..., description="命中该围栏的停留段数（访问次数）")
    total_duration_s: float = Field(..., description="命中停留的累计停留时长（秒）")


class GeofenceVisitResponse(BaseModel):
    stays: List[GeofenceStay]
    zones: List[GeofenceZoneSummary]
    stats: Stats
