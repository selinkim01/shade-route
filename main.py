"""
그늘길 추천 - 백엔드 (FastAPI)

흐름:
  1) 시각 -> 태양 위치(고도, 방위각)
  2) 태양 위치 + 건물 높이/가로수 -> 그림자
  3) 경로를 10m 구간으로 나눠 구간별 그늘 비율 -> 체감온도
  4) 경로별 비용 = Σ 시간 x (체감온도 - 25)^2  +  λ x 총 소요시간
  5) 비용이 낮은 순서대로 추천
"""
import math
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).parent
app = FastAPI(title="그늘길 추천 API")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

# ---------------- 설정값 ----------------
LAT, LON = 37.56, 126.98   # 서울
TARGET = 25.0              # 적정 체감온도
WALK_SPEED = 1.3           # 보행속도 (m/s)
SEG_LEN = 10.0             # 경로를 10m 구간으로 나눔
K_SUN = 8.0                # 직사광선 아래에서 체감온도 상승폭 (°C, 맑은 날 정오 기준)
TREE_SHADE = 0.7           # 가로수 그늘은 햇빛을 70% 막음

# ---------------- 1. 더미 지도 데이터 (단위: m, x=동쪽, y=북쪽) ----------------
START, GOAL = (20, 150), (380, 150)
BOUNDS = {"xmin": 0, "xmax": 400, "ymin": 50, "ymax": 250}

# (x, y, 폭, 깊이, 높이) : (x, y)는 건물의 남서쪽 모서리
BUILDINGS = [
    # 대로와 골목 사이: 높은 빌딩 -> 북쪽 골목에 그림자
    (70, 158, 40, 36, 35), (118, 160, 38, 34, 42), (164, 158, 44, 36, 30),
    (216, 160, 40, 34, 45), (264, 158, 60, 36, 38),
    # 골목 북쪽: 낮은 건물
    (75, 210, 50, 30, 15), (140, 212, 60, 28, 20), (215, 210, 50, 30, 12),
    (280, 212, 45, 28, 18),
    # 대로 남쪽: 중간 높이 건물
    (70, 110, 45, 32, 14), (130, 108, 50, 34, 18), (200, 112, 40, 30, 10),
    (255, 110, 60, 32, 16),
]

# (x, y, 수관 높이, 수관 반지름)
TREES = (
    [(x, 97, 6.0, 4.0) for x in range(66, 336, 8)]       # 공원길 남쪽 가로수
    + [(x, 103, 6.0, 4.0) for x in range(70, 336, 16)]   # 공원길 북쪽 가로수
    + [(x, 146, 6.0, 3.0) for x in range(90, 331, 40)]   # 대로 가로수 (드문드문)
)
PARK = [(60, 55), (340, 55), (340, 96), (60, 96)]

ROUTES = [
    {"id": "main", "name": "대로 (최단거리)", "desc": "짧지만 그늘이 거의 없음",
     "points": [(20, 150), (380, 150)]},
    {"id": "alley", "name": "북쪽 빌딩 골목", "desc": "높은 빌딩 그림자 아래",
     "points": [(20, 150), (60, 150), (60, 200), (340, 200), (340, 150), (380, 150)]},
    {"id": "park", "name": "남쪽 공원 가로수길", "desc": "가로수 그늘이 이어짐",
     "points": [(20, 150), (60, 150), (60, 100), (340, 100), (340, 150), (380, 150)]},
]


# ---------------- 2. 태양 위치 ----------------
def sun_position(day_of_year: int, clock_hour: float):
    """반환: (고도°, 방위각°)  방위각은 북쪽=0, 시계방향(동=90, 남=180)"""
    solar_hour = clock_hour + (LON - 135) / 15          # 한국 표준시 -> 태양시 (약 -32분)
    decl = math.radians(23.45) * math.sin(math.radians(360 / 365 * (284 + day_of_year)))
    H = math.radians(15 * (solar_hour - 12))            # 시간각
    lat = math.radians(LAT)
    sin_alt = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(H)
    alt = math.asin(max(-1.0, min(1.0, sin_alt)))
    az = math.atan2(math.sin(H), math.cos(H) * math.sin(lat) - math.tan(decl) * math.cos(lat)) + math.pi
    return math.degrees(alt), math.degrees(az) % 360


# ---------------- 3. 그림자 계산 ----------------
def convex_hull(pts):
    pts = sorted(set(pts))
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]   # 반시계 방향


def in_convex(p, poly):
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        if (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) < 0:
            return False
    return True


def shadows(alt, az):
    """건물 그림자 다각형 + 가로수 그림자 원 목록"""
    if alt <= 0.5:
        return None   # 해가 졌음
    t = math.tan(math.radians(alt))
    sx, sy = -math.sin(math.radians(az)), -math.cos(math.radians(az))  # 그림자 방향 (태양 반대)
    polys = []
    for x, y, w, d, h in BUILDINGS:
        L = min(h / t, 400)
        corners = [(x, y), (x + w, y), (x + w, y + d), (x, y + d)]
        moved = [(cx + sx * L, cy + sy * L) for cx, cy in corners]
        polys.append(convex_hull(corners + moved))
    circles = []
    for x, y, h, r in TREES:
        L = min(h / t, 200)
        circles.append((x + sx * L, y + sy * L, r))
    return polys, circles


def shade_at(p, sh):
    if sh is None:
        return 1.0
    polys, circles = sh
    if any(in_convex(p, poly) for poly in polys):
        return 1.0
    if any((p[0] - cx) ** 2 + (p[1] - cy) ** 2 <= r * r for cx, cy, r in circles):
        return TREE_SHADE
    return 0.0


# ---------------- 4. 기상 (더미) ----------------
def air_temp(hour, tmax, tmin):
    """하루 기온 곡선: 15시 최고, 3시 최저"""
    return (tmax + tmin) / 2 + (tmax - tmin) / 2 * math.cos(2 * math.pi * (hour - 15) / 24)


def irradiance(alt, cloud):
    """일사량 (W/m²), 구름량 0~1"""
    if alt <= 0:
        return 0.0
    return 1000 * math.sin(math.radians(alt)) ** 1.2 * (1 - 0.75 * cloud ** 3.4)


# ---------------- 5. 경로 시뮬레이션 ----------------
def subdivide(points):
    segs = []
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        n = max(1, round(math.hypot(x2 - x1, y2 - y1) / SEG_LEN))
        for k in range(n):
            a = (x1 + (x2 - x1) * k / n, y1 + (y2 - y1) * k / n)
            b = (x1 + (x2 - x1) * (k + 1) / n, y1 + (y2 - y1) * (k + 1) / n)
            segs.append((a, b))
    return segs


def simulate_route(route, doy, hour, tmax, tmin, cloud, lam):
    t = hour                       # 현재 시각 (구간을 지날 때마다 증가)
    D = naive = length = 0.0
    max_T = -99
    out = []
    for a, b in subdivide(route["points"]):
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        alt, az = sun_position(doy, t)
        sh = shadows(alt, az)
        samples = [(a[0] + (b[0] - a[0]) * (k + .5) / 5, a[1] + (b[1] - a[1]) * (k + .5) / 5) for k in range(5)]
        s = sum(shade_at(p, sh) for p in samples) / 5
        T = air_temp(t, tmax, tmin) + K_SUN * irradiance(alt, cloud) / 1000 * (1 - s)
        dt_min = L / WALK_SPEED / 60
        D += dt_min * (T - TARGET) ** 2
        naive += L * T
        length += L
        max_T = max(max_T, T)
        out.append({"a": a, "b": b, "T": round(T, 2), "shade": round(s, 2)})
        t += dt_min / 60
    minutes = length / WALK_SPEED / 60
    return {
        "id": route["id"], "name": route["name"], "desc": route["desc"],
        "length": round(length), "minutes": round(minutes, 1),
        "mean_T": round(naive / length, 2), "max_T": round(max_T, 2),
        "shade_ratio": round(sum(o["shade"] for o in out) / len(out), 2),
        "discomfort": round(D, 1), "naive_sum": round(naive),
        "cost": round(D + lam * minutes, 1), "segments": out,
    }


# ---------------- 6. API ----------------
@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/simulate")
def simulate(
    month: int = Query(7, ge=1, le=12),
    day: int = Query(20, ge=1, le=28),
    hour: float = Query(14.0, ge=0, le=23.99),
    tmax: float = Query(32.0, ge=-20, le=45),
    tmin: float = Query(24.0, ge=-30, le=40),
    cloud: float = Query(0.1, ge=0, le=1),
    lam: float = Query(20.0, ge=0, le=500),
):
    doy = date(2026, month, day).timetuple().tm_yday
    alt, az = sun_position(doy, hour)
    sh = shadows(alt, az)
    routes = [simulate_route(r, doy, hour, tmax, tmin, cloud, lam) for r in ROUTES]
    routes.sort(key=lambda r: r["cost"])
    for i, r in enumerate(routes):
        r["rank"] = i + 1
    return {
        "sun": {"alt": round(alt, 1), "az": round(az, 1)},
        "air_temp": round(air_temp(hour, tmax, tmin), 1),
        "target": TARGET,
        "map": {
            "bounds": BOUNDS, "start": START, "goal": GOAL, "park": PARK,
            "buildings": [{"x": x, "y": y, "w": w, "d": d, "h": h} for x, y, w, d, h in BUILDINGS],
            "trees": [{"x": x, "y": y, "r": r} for x, y, _, r in TREES],
        },
        "shadows": {
            "buildings": sh[0] if sh else [],
            "trees": [{"x": x, "y": y, "r": r} for x, y, r in sh[1]] if sh else [],
            "night": sh is None,
        },
        "routes": routes,
    }


@app.get("/api/health")
def health():
    return {"ok": True}
