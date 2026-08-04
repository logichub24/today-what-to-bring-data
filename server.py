"""Local server for the Today Carry prototype.

The service key is read only from .env or the process environment; it is never
sent to the browser or committed to Git.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen

CACHE_SECONDS = 1800
ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
AIR_CACHE_SECONDS = 3600
KMA_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtNcst"
AIR_URL = "https://apis.data.go.kr/B552584/ArpltnInforInqireSvc/getCtprvnRltmMesureDnsty"
REGIONS = {
    "seoul": ("서울특별시", 60, 127), "busan": ("부산광역시", 98, 76),
    "daegu": ("대구광역시", 89, 90), "incheon": ("인천광역시", 55, 124),
    "gwangju": ("광주광역시", 58, 74), "daejeon": ("대전광역시", 67, 100),
    "ulsan": ("울산광역시", 102, 84), "sejong": ("세종특별자치시", 66, 103),
    "gyeonggi": ("경기도", 60, 120), "gangwon": ("강원특별자치도", 73, 134),
    "chungbuk": ("충청북도", 69, 107), "chungnam": ("충청남도", 68, 100),
    "jeonbuk": ("전북특별자치도", 63, 89), "jeonnam": ("전라남도", 51, 67),
    "gyeongbuk": ("경상북도", 89, 91), "gyeongnam": ("경상남도", 91, 77),
    "jeju": ("제주특별자치도", 53, 38),
}
cache: tuple[float, dict] | None = None
air_cache: tuple[float, dict[str, float]] | None = None
AIR_NAMES = {key: name for key, name in zip(REGIONS, ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주"))}


def load_local_env() -> None:
    """Load the one local secret without adding a dotenv dependency."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key and key not in os.environ:
            os.environ[key] = value.strip()


def base_time() -> tuple[str, str]:
    # Observations are issued after the half hour. Use the completed hour.
    now = datetime.now() - timedelta(minutes=40)
    return now.strftime("%Y%m%d"), now.strftime("%H00")


def fetch_region(key: str, region: tuple[str, int, int]) -> tuple[str, dict]:
    name, nx, ny = region
    try:
        base_date, base_hour = base_time()
        query = urlencode({
            "serviceKey": os.environ["PUBLIC_DATA_SERVICE_KEY"], "pageNo": 1, "numOfRows": 10,
            "dataType": "JSON", "base_date": base_date, "base_time": base_hour, "nx": nx, "ny": ny,
        })
        with urlopen(f"{KMA_URL}?{query}", timeout=5) as response:
            payload = json.load(response)
        values = {item["category"]: item["obsrValue"] for item in payload["response"]["body"]["items"]["item"]}
        return key, {"name": name, "temp": float(values["T1H"]), "rain": float(values.get("RN1", 0)), "humidity": float(values["REH"])}
    except (KeyError, OSError, TypeError, ValueError):
        return key, {"name": name}


def fetch_air_region(key: str) -> tuple[str, float | None]:
    query = urlencode({"serviceKey": os.environ["PUBLIC_DATA_SERVICE_KEY"], "returnType": "json", "numOfRows": 100, "pageNo": 1, "sidoName": AIR_NAMES[key], "ver": "1.0"})
    try:
        with urlopen(f"{AIR_URL}?{query}", timeout=10) as response:
            items = json.load(response)["response"]["body"]["items"]
        values = [float(item["pm25Value"]) for item in items if str(item.get("pm25Value") or "").replace(".", "", 1).isdigit()]
        return key, round(sum(values) / len(values), 1) if values else None
    except (KeyError, OSError, TypeError, ValueError):
        return key, None


def air_data() -> dict[str, float]:
    global air_cache
    if air_cache and time.time() - air_cache[0] < AIR_CACHE_SECONDS:
        return air_cache[1]
    with ThreadPoolExecutor(max_workers=6) as pool:
        measurements = {key: pm25 for key, pm25 in pool.map(fetch_air_region, AIR_NAMES) if pm25 is not None}
    air_cache = (time.time(), measurements)
    return measurements


def live_data() -> dict:
    global cache
    if cache and time.time() - cache[0] < CACHE_SECONDS:
        return cache[1]
    if not os.environ.get("PUBLIC_DATA_SERVICE_KEY"):
        raise RuntimeError("공공데이터 인증키가 설정되지 않았습니다.")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = dict(pool.map(lambda pair: fetch_region(*pair), REGIONS.items()))
    for key, pm25 in air_data().items():
        results[key]["pm25"] = pm25
    payload = {"source": "제공: 기상청 30분 · 한국환경공단 에어코리아 1시간", "updatedAt": datetime.now().isoformat(timespec="minutes"), "locations": results}
    cache = (time.time(), payload)
    return payload


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        if urlparse(self.path).path != "/api/today":
            return super().do_GET()
        try:
            body = json.dumps(live_data(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
        except (KeyError, RuntimeError, OSError, ValueError) as error:
            body = json.dumps({"error": str(error)}, ensure_ascii=False).encode("utf-8")
            self.send_response(503)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def translate_path(self, path: str) -> str:
        source = Path(super().translate_path(path))
        try:
            built = DIST / source.resolve().relative_to(ROOT)
        except ValueError:
            return str(source)
        return str(built if built.is_file() else source)


if __name__ == "__main__":
    load_local_env()
    print("http://127.0.0.1:48627")
    port = int(os.environ.get("PORT", "48627"))
    ThreadingHTTPServer((os.environ.get("HOST", "127.0.0.1"), port), Handler).serve_forever()
