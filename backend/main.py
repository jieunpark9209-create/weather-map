"""
전국 측정소 미세먼지 백엔드 (FastAPI)
  에어코리아 ① 측정소 목록(좌표)  ② 실시간 측정값(전국)  → 합쳐서 /air/stations 로 제공
  - 인증키(DATA_GO_KR_KEY)는 서버 안에만 둔다. 프론트는 이 서버만 호출.
  - 키가 없으면 연습(demo) 모드: 주요 도시 30곳 가짜 측정소를 돌려준다.
  - 호출 횟수 절약: 실시간 값 60분, 측정소 좌표 24시간 캐시 (개발계정 일 500회 제한 대비)
"""
import math
import os
import random
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()
KEY = os.getenv("DATA_GO_KR_KEY", "").strip()
DEMO = os.getenv("DEMO_MODE", "0") == "1" or not KEY
KST = timezone(timedelta(hours=9))
BASE = "https://apis.data.go.kr/B552584"
SIDOS = ["서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원",
         "충북", "충남", "전북", "전남", "경북", "경남", "제주"]
REALTIME_TTL, STATION_TTL = 60 * 60, 24 * 60 * 60

app = FastAPI(title="전국 미세먼지 백엔드")
# 공개 측정값만 주므로 GET 은 모두 허용 (키는 응답에 포함되지 않음)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

_cache: dict[str, tuple[float, object]] = {}
last_error: str | None = None


def cached(name: str, ttl: int, fn):
    hit = _cache.get(name)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[name] = (time.time(), val)
    return val


def call(path: str, **params) -> list[dict]:
    """공공데이터 API 호출 → items. 오류 메시지에 키가 새지 않게 직접 처리."""
    p = {"serviceKey": unquote(KEY), "returnType": "json", "pageNo": "1", **params}
    r = httpx.get(f"{BASE}/{path}", params=p, timeout=30)
    if r.status_code >= 400 or not r.text.strip().startswith("{"):
        raise RuntimeError(f"{path} HTTP {r.status_code}: {r.text.strip()[:150]}")
    body = r.json()["response"]
    if body["header"].get("resultCode") != "00":
        raise RuntimeError(f"{path}: {body['header'].get('resultMsg')}")
    return body["body"].get("items") or []


def num(v):
    try:
        x = float(v)
        return x if 0 <= x <= 1000 else None  # 음수·터무니없는 값은 결측 처리
    except (TypeError, ValueError):
        return None  # '-' 등


def grade(v, cuts):
    if v is None:
        return None
    for g, c in zip(["좋음", "보통", "나쁨"], cuts):
        if v <= c:
            return g
    return "매우나쁨"


def parse_time(s: str) -> str | None:
    """'2026-11-10 24:00' → '2026-11-11 00:00' (에어코리아는 자정을 24:00 으로 보낸다)"""
    try:
        day, hm = str(s).split(" ")
        t = datetime.strptime(day, "%Y-%m-%d") + timedelta(hours=int(hm.split(":")[0]))
        return t.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


# ---------- ① 측정소 좌표 ----------
def load_station_coords() -> dict[str, dict]:
    out = {}
    for sido in SIDOS:
        for it in call("MsrstnInfoInqireSvc/getMsrstnList", addr=sido, numOfRows="500"):
            lat, lon = num(it.get("dmX")), num(it.get("dmY"))
            if lat is None or lon is None:
                continue
            if lat > 90:  # 위도/경도가 뒤바뀌어 오는 경우 방어
                lat, lon = lon, lat
            if not (30 < lat < 40 and 120 < lon < 135):  # 한반도 밖은 오류 데이터
                continue
            out[it["stationName"]] = {"sido": sido, "addr": it.get("addr"), "lat": lat, "lon": lon}
    return out


# ---------- ② 실시간 측정값 ----------
def load_realtime() -> list[dict]:
    try:
        return call("ArpltnInforInqireSvc/getCtprvnRltmMesureDnsty",
                    sidoName="전국", numOfRows="1000", ver="1.0")
    except Exception:  # '전국' 이 거부되면 시도별로 모은다
        items = []
        for sido in SIDOS:
            items += call("ArpltnInforInqireSvc/getCtprvnRltmMesureDnsty",
                          sidoName=sido, numOfRows="1000", ver="1.0")
        return items


def build_real() -> list[dict]:
    coords = cached("coords", STATION_TTL, load_station_coords)
    rows = []
    for it in cached("realtime", REALTIME_TTL, load_realtime):
        c = coords.get(it.get("stationName"))
        if not c:
            continue
        flag10, flag25 = it.get("pm10Flag"), it.get("pm25Flag")
        pm10 = None if flag10 else num(it.get("pm10Value"))  # 장비 이상 표시가 있으면 버린다
        pm25 = None if flag25 else num(it.get("pm25Value"))
        if pm10 is None and pm25 is None:
            continue
        rows.append(station_row(it["stationName"], c["sido"], c["addr"], c["lat"], c["lon"],
                                pm10, pm25, parse_time(it.get("dataTime"))))
    return rows


def station_row(name, sido, addr, lat, lon, pm10, pm25, t):
    return {"station": name, "sido": sido, "addr": addr, "lat": lat, "lon": lon,
            "pm10": None if pm10 is None else round(pm10),
            "pm25": None if pm25 is None else round(pm25),
            "pm10_grade": grade(pm10, (30, 80, 150)), "pm25_grade": grade(pm25, (15, 35, 75)),
            "time": t}


# ---------- 연습 모드 ----------
DEMO_POINTS = [("서울", "서울", 37.5665, 126.9780), ("부산", "부산", 35.1796, 129.0756),
               ("대구", "대구", 35.8714, 128.6014), ("인천", "인천", 37.4563, 126.7052),
               ("광주", "광주", 35.1595, 126.8526), ("대전", "대전", 36.3504, 127.3845),
               ("울산", "울산", 35.5384, 129.3114), ("수원", "경기", 37.2636, 127.0286),
               ("춘천", "강원", 37.8813, 127.7298), ("청주", "충북", 36.6424, 127.4890),
               ("전주", "전북", 35.8242, 127.1480), ("제주", "제주", 33.4996, 126.5312)]


def build_demo() -> list[dict]:
    now = datetime.now(KST).replace(minute=0, second=0, microsecond=0)
    rows = []
    for name, sido, lat, lon in DEMO_POINTS:
        rnd = random.Random(f"{name}-{now:%Y%m%d%H}")
        pm10 = max(5, 40 + rnd.gauss(0, 25))
        rows.append(station_row(name + "(연습)", sido, "연습 데이터", lat, lon, pm10, pm10 * 0.5,
                                now.strftime("%Y-%m-%d %H:%M")))
    return rows


# ---------- API ----------
@app.get("/")
def home():
    return {"service": "전국 미세먼지 백엔드", "mode": "demo" if DEMO else "airkorea", "docs": "/docs"}


@app.get("/health")
def health():
    return {"ok": True, "mode": "demo" if DEMO else "airkorea", "key_set": bool(KEY),
            "last_error": last_error}


@app.get("/air/stations")
def stations(sido: str | None = None):
    """전국(또는 시도별) 측정소의 좌표 + 최신 PM10/PM2.5. 실패해도 200 + error 로 알려준다."""
    global last_error
    try:
        rows = build_demo() if DEMO else build_real()
        last_error = None
    except Exception as e:
        last_error = str(e).replace(KEY, "***")[:300] if KEY else str(e)[:300]
        return {"mode": "airkorea", "count": 0, "stations": [], "error": last_error}
    if sido:
        rows = [r for r in rows if r["sido"] == sido]
    return {"mode": "demo" if DEMO else "airkorea", "count": len(rows), "stations": rows}


def _km(a_lat, a_lon, b_lat, b_lon):
    p = math.pi / 180
    h = (math.sin((b_lat - a_lat) * p / 2) ** 2
         + math.cos(a_lat * p) * math.cos(b_lat * p) * math.sin((b_lon - a_lon) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(h))


@app.get("/air/nearest")
def nearest(lat: float, lon: float, limit: int = 3):
    """좌표에서 가장 가까운 측정소 (거리 km 포함)"""
    data = stations()
    rows = sorted(data["stations"], key=lambda r: _km(lat, lon, r["lat"], r["lon"]))[:limit]
    return [{**r, "distance_km": round(_km(lat, lon, r["lat"], r["lon"]), 1)} for r in rows]
