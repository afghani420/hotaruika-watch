#!/usr/bin/env python3
"""
天気・月齢・潮汐予報生成スクリプト
- 富山県滑川市（ホタルイカミュージアム周辺）基準
- 天気: Open-Meteo API（無料・APIキー不要）
- 月齢・潮汐種別: 天文計算（APIキー不要）
- 満潮・干潮時刻: 月の南中 + 伏木港時差（近似値、±1〜2時間の誤差あり）
"""

import json
import logging
import requests
import ephem
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
FORECAST_FILE = Path(__file__).parent.parent / "data" / "forecast.json"

# 滑川市（ホタルイカミュージアム付近）座標
LAT = 36.77
LON = 137.17

# 伏木港 潮汐パラメータ（近似値）
# 月の上中天から満潮までの時差: 約9時間
LUNITIDAL_HW = 9.0   # hours
HALF_TIDAL = 6.2     # hours (M2半周期 ≒ 12.4h / 2)

SYNODIC_MONTH = 29.530588853
REF_NEW_MOON = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)

MOON_ICONS = [
    "🌑", "🌒", "🌒", "🌒", "🌓", "🌔", "🌔", "🌔",
    "🌕", "🌖", "🌖", "🌖", "🌗", "🌘", "🌘", "🌘",
]

WEATHER_MAP = {
    0:  ("☀️", "快晴"),
    1:  ("🌤", "晴れ"),
    2:  ("⛅", "薄曇り"),
    3:  ("☁️", "曇り"),
    45: ("🌫", "霧"),
    48: ("🌫", "霧氷"),
    51: ("🌦", "霧雨"),
    53: ("🌦", "霧雨"),
    55: ("🌦", "霧雨(強)"),
    61: ("🌧", "小雨"),
    63: ("🌧", "雨"),
    65: ("🌧", "大雨"),
    71: ("❄️", "小雪"),
    73: ("❄️", "雪"),
    75: ("❄️", "大雪"),
    77: ("❄️", "霰"),
    80: ("🌦", "にわか雨"),
    81: ("🌦", "にわか雨"),
    82: ("🌧", "強にわか雨"),
    85: ("🌨", "にわか雪"),
    86: ("🌨", "強にわか雪"),
    95: ("⛈", "雷雨"),
    96: ("⛈", "雷雨"),
    99: ("⛈", "激しい雷雨"),
}
WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]


def moon_age(dt_jst: datetime) -> float:
    """月齢（新月からの日数）を返す"""
    dt_utc = dt_jst.astimezone(timezone.utc)
    elapsed = (dt_utc - REF_NEW_MOON).total_seconds() / 86400
    return elapsed % SYNODIC_MONTH


def moon_icon(age: float) -> str:
    idx = int(age / SYNODIC_MONTH * 16) % 16
    return MOON_ICONS[idx]


def tide_type(age: float) -> str:
    """月齢から潮汐名を返す"""
    a = age % SYNODIC_MONTH
    if a < 1.5 or a >= 28.0:
        return "大潮"
    elif a < 3.5 or (12.5 <= a < 16.5) or (26.5 <= a < 28.0):
        if 26.5 <= a < 28.0:
            return "中潮"
        if a < 1.5 or (12.5 <= a < 16.5):
            return "大潮"
        return "中潮"
    elif a < 5.5 or (17.5 <= a < 20.5):
        return "小潮"
    elif a < 6.5 or (20.5 <= a < 21.5):
        return "長潮"
    elif a < 7.5 or (21.5 <= a < 22.5):
        return "若潮"
    elif a < 12.5 or (22.5 <= a < 26.5):
        return "中潮"
    elif a < 17.5:
        return "大潮"
    else:
        return "中潮"


def calc_tide_times(target: date) -> list[dict]:
    """伏木港の近似満潮・干潮時刻を計算"""
    obs = ephem.Observer()
    obs.lat = str(LAT)
    obs.lon = str(LON)
    obs.elevation = 0
    obs.pressure = 0

    # 前日21時(JST)=当日0時UTC付近から探索
    start = (
        datetime(target.year, target.month, target.day, 0, 0, tzinfo=JST)
        .astimezone(timezone.utc) - timedelta(hours=6)
    )
    obs.date = start.strftime("%Y/%m/%d %H:%M:%S")
    moon = ephem.Moon()

    events = []
    for transit_fn in (obs.next_transit, obs.next_antitransit):
        search = obs.date
        for _ in range(3):
            try:
                t = transit_fn(moon, start=search)
                t_jst = ephem.Date(t).datetime().replace(tzinfo=timezone.utc).astimezone(JST)
                hw = t_jst + timedelta(hours=LUNITIDAL_HW)
                for ev_time, ev_type in [
                    (hw - timedelta(hours=HALF_TIDAL), "干潮"),
                    (hw,                                "満潮"),
                    (hw + timedelta(hours=HALF_TIDAL),  "干潮"),
                ]:
                    if ev_time.date() == target:
                        events.append({"type": ev_type, "time": ev_time.strftime("%H:%M")})
                search = t + ephem.hour * 13
            except Exception:
                break

    # 重複排除・時刻順ソート
    seen, unique = set(), []
    for e in sorted(events, key=lambda x: x["time"]):
        key = (e["type"], e["time"])
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


def fetch_weather() -> dict:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
        "precipitation_sum,wind_speed_10m_max"
        "&timezone=Asia%2FTokyo&forecast_days=8"
    )
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"Open-Meteo取得エラー: {e}")
        return {}


def generate() -> list[dict]:
    logger.info("天気データ取得中...")
    wd = fetch_weather()
    daily = wd.get("daily", {})
    dates    = daily.get("time", [])
    codes    = daily.get("weather_code", [])
    t_max    = daily.get("temperature_2m_max", [])
    t_min    = daily.get("temperature_2m_min", [])
    precip   = daily.get("precipitation_sum", [])

    result = []
    for i, ds in enumerate(dates):
        d = date.fromisoformat(ds)
        dt_jst = datetime(d.year, d.month, d.day, 12, 0, tzinfo=JST)

        age = moon_age(dt_jst)
        tt  = tide_type(age)
        mi  = moon_icon(age)

        logger.info(f"  {ds} 月齢{age:.1f} {tt} 潮汐計算中...")
        tides = calc_tide_times(d)

        morning_hw = [t for t in tides if t["type"] == "満潮" and t["time"] < "06:00"]

        code = codes[i] if i < len(codes) else 0
        w_icon, w_label = WEATHER_MAP.get(code, ("🌡", "不明"))

        is_spring = tt in ("大潮", "中潮")
        has_morning_hw = bool(morning_hw)
        is_fine = code in (0, 1, 2)
        watch_score = sum([is_spring, has_morning_hw, is_fine])

        result.append({
            "date":       ds,
            "weekday":    WEEKDAYS[d.weekday()],
            "weather_icon":  w_icon,
            "weather":    w_label,
            "temp_max":   round(t_max[i]) if i < len(t_max) and t_max[i] is not None else None,
            "temp_min":   round(t_min[i]) if i < len(t_min) and t_min[i] is not None else None,
            "precip":     round(precip[i], 1) if i < len(precip) and precip[i] is not None else 0.0,
            "moon_age":   round(age, 1),
            "moon_icon":  mi,
            "tide_type":  tt,
            "tide_times": tides,
            "morning_hw": morning_hw,
            "watch_score": watch_score,
        })
    return result


def main():
    logger.info("=== 予報生成開始 ===")
    days = generate()
    out = {
        "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        "location": "富山県滑川市（ホタルイカミュージアム周辺）",
        "note": "満潮・干潮は伏木港基準の近似値（±1〜2時間の誤差あり）",
        "days": days,
    }
    FORECAST_FILE.parent.mkdir(exist_ok=True)
    FORECAST_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"=== 予報生成完了: {len(days)}日分 ===")


if __name__ == "__main__":
    main()
