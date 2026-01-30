import streamlit as st
import json
import os
from datetime import datetime, timedelta
from statistics import stdev
import requests
import re
import pandas as pd
import warnings

warnings.filterwarnings("ignore", category=Warning)

# ZoneInfo fallback for compatibility
try:
    from zoneinfo import ZoneInfo
except ImportError:
    class ZoneInfo(str):
        def __new__(cls, name):
            return str.__new__(cls, name)

from dataclasses import dataclass  # FIX: Missing import causing NameError

# ============= CONFIG =============
API_KEY = "570b45680d41097ee46550e36f7c1290754081becee8955529b0d197cf9d8efd"
OBS_URL = "https://wethr.net/api/v2/observations.php"
FORECASTS_URL = "https://wethr.net/api/v2/forecasts.php"
NWS_URL = "https://wethr.net/api/v2/nws_forecasts.php"
TARGET_MODELS = ["HRRR", "NAM", "NBM", "ECMWF-IFS"]
MODEL_WEIGHTS_BASE = {'HRRR': 0.35, 'NAM': 0.25, 'NBM': 0.25, 'ECMWF-IFS': 0.15}

@dataclass
class CityConfig:
    name: str
    station_code: str
    location_name: str
    timezone: str
    gridpoint: str
    lat_lon: str

CITY_PRESETS = [
    CityConfig("Seattle", "KSEA", "KSEA", "America/Los_Angeles", "SEW/125,131", "47.6062,-122.3321"),
    CityConfig("San Francisco", "KSFO", "KSFO", "America/Los_Angeles", "MTR/94,70", "37.7749,-122.4194"),
    CityConfig("Washington DC", "KDCA", "KDCA", "America/New_York", "LWX/97,71", "38.9072,-77.0369"),
    CityConfig("New Orleans", "KMSY", "KMSY", "America/Chicago", "LIX/76,34", "29.9511,-90.0715"),
    CityConfig("Las Vegas", "KLAS", "KLAS", "America/Los_Angeles", "VEF/127,101", "36.1699,-115.1398"),
    CityConfig("Miami", "KMIA", "KMIA", "America/New_York", "MFL/64,31", "25.7617,-80.1918"),
]

# ============= HELPERS =============
def auth_headers():
    return {"X-API-Key": API_KEY}

def todays_local_day_range_utc(tz_name):
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    start_local = datetime(now_local.year, now_local.month, now_local.day, 0, 0, 0, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc).replace(microsecond=0)
    end_utc = end_local.astimezone(timezone.utc).replace(microsecond=0)
    return start_utc.isoformat().replace("+00:00", "Z"), end_utc.isoformat().replace("+00:00", "Z")

def fetch_data(url, params):
    try:
        r = requests.get(url, params=params, headers=auth_headers(), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def fetch_observed_high(city):
    params = {"station_code": city.station_code, "mode": "wethr_high", "logic": "nws"}
    return fetch_data(OBS_URL, params)

def fetch_nws_high(city):
    params = {"station_code": city.station_code, "mode": "latest"}
    return fetch_data(NWS_URL, params)

def fetch_model_forecasts(city):
    start_iso, end_iso = todays_local_day_range_utc(city.timezone)
    params = {
        "location_name": city.location_name,
        "start_valid_time": start_iso,
        "end_valid_time": end_iso,
        "mode": "hourly"
    }
    data = fetch_data(FORECASTS_URL, params)
    records = data.get("data", data) if isinstance(data, dict) else data
    model_highs = {m: None for m in TARGET_MODELS}
    for rec in records:
        model = rec.get("model")
        if model not in TARGET_MODELS: continue
        temp_raw = rec.get("temperature_f")
        if temp_raw is None: continue
        try:
            temp = float(temp_raw)
        except: continue
        if model_highs[model] is None or temp > model_highs[model]:
            model_highs[model] = temp
    return {m: v for m, v in model_highs.items() if v is not None}

def fetch_nws_gridpoint(city):
    try:
        point_url = f"https://api.weather.gov/points/{city.lat_lon}"
        headers = {"User-Agent": "WethrHelper"}
        r = requests.get(point_url, headers=headers, timeout=10)
        r.raise_for_status()
        point_data = r.json()["properties"]
        forecast_url = point_data["forecast"]
        r = requests.get(forecast_url, headers=headers, timeout=10)
        r.raise_for_status()
        periods = r.json()["properties"]["periods"]
        today_high = None
        for p in periods:
            if "temperature" in p and ("afternoon" in p["name"].lower() or "today" in p["name"].lower()):
                today_high = p["temperature"]
                break
        return float(today_high) if today_high else None
    except:
        return None

# ============= STREAMLIT APP =============
st.set_page_config(page_title="Wethr Helper", layout="wide")
st.title("Wethr Helper Dashboard")
st.caption("Latest weather blends, NWS backup, and Kalshi comparison. Refreshes on page load or button press.")

selected_cities = st.multiselect("Select Cities", [c.name for c in CITY_PRESETS], default=["Miami", "Seattle"])

if st.button("Refresh Data Now"):
    st.rerun()

if not selected_cities:
    st.warning("Select at least one city.")
else:
    data = {}
    for city_name in selected_cities:
        city = next(c for c in CITY_PRESETS if c.name == city_name)
        with st.spinner(f"Loading {city.name}..."):
            obs = fetch_observed_high(city)
            nws = fetch_nws_high(city)
            model_highs = fetch_model_forecasts(city)
            nws_high = float(nws.get("high")) if nws and nws.get("high") else None
            nws_grid = fetch_nws_gridpoint(city)
            obs_high = obs.get("wethr_high") if obs else None
            obs_high_f = float(obs_high) if obs_high else None

            if len(model_highs) < 3:
                data[city_name] = {"error": "Insufficient models"}
                continue

            vals = list(model_highs.values())
            weights = MODEL_WEIGHTS_BASE.copy()
            now_hour = datetime.now(ZoneInfo(city.timezone)).hour
            if now_hour > 12:
                weights['HRRR'] += 0.1
                weights['NAM'] += 0.1
                total = sum(weights.values())
                weights = {k: v/total for k, v in weights.items()}
            blend = sum(weights.get(m, 0) * model_highs.get(m, 0) for m in TARGET_MODELS)
            spread = max(vals) - min(vals)
            std = stdev(vals) if len(vals) > 1 else 0

            if nws_grid:
                blend = 0.7 * blend + 0.3 * nws_grid

            diff_nws = abs(blend - nws_high) if nws_high else None
            status = "GREEN" if spread <= 3.0 and (diff_nws or 999) <= 1.5 else \
                     "YELLOW" if spread <= 4.0 and (diff_nws or 999) <= 2.0 else "RED"

            center = round(blend)
            band = (center - 1, center + 1)
            prob_in_band = 68 if std < 1.5 else 50 if std < 2.5 else 30

            kalshi_data = "Kalshi fetch not implemented in this demo version."

            data[city_name] = {
                "blend": round(blend, 1),
                "spread": round(spread, 1),
                "status": status,
                "band": band,
                "prob": prob_in_band,
                "observed": obs_high_f,
                "kalshi": kalshi_data
            }

    df = pd.DataFrame([
        {
            "City": name,
            "Blend": f"{d['blend']}°F",
            "Spread": f"{d['spread']}°F",
            "Status": d['status'],
            "Band": f"{d['band'][0]}–{d['band'][1]}°F",
            "Confidence": f"~{d['prob']}%",
            "Observed": f"{d.get('observed', 'N/A')}°F",
            "Kalshi": d.get('kalshi', 'N/A')
        }
        for name, d in data.items()
    ])

    st.dataframe(df, use_container_width=True)

st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (refreshes on page load)")
