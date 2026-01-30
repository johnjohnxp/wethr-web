import streamlit as st
import json
import os
from datetime import datetime, timedelta, timezone
from statistics import stdev
import requests
import re
import pandas as pd
import warnings

warnings.filterwarnings("ignore", category=Warning)

# ZoneInfo fallback
try:
    from zoneinfo import ZoneInfo
except ImportError:
    class ZoneInfo(str):
        def __new__(cls, name):
            return str.__new__(cls, name)

from dataclasses import dataclass

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

# ============= HELPERS & FETCH (same as before) =============
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
        st.error(f"API error: {e}")
        return {}

# ... (keep your fetch_observed_high, fetch_nws_high, fetch_model_forecasts, fetch_nws_gridpoint functions here)

# ============= STREAMLIT APP =============
st.set_page_config(page_title="Wethr Helper", layout="wide")
st.title("Wethr Helper Dashboard")
st.caption("Latest weather blends, NWS backup, Kalshi markets, and predictions. Refreshes on page load or button press.")

selected_cities = st.multiselect("Select Cities", [c.name for c in CITY_PRESETS], default=["Miami", "Seattle"])

if st.button("Refresh Data Now"):
    st.rerun()

if not selected_cities:
    st.warning("Select at least one city.")
else:
    for city_name in selected_cities:
        city = next(c for c in CITY_PRESETS if c.name == city_name)
        with st.expander(f"📍 {city.name}", expanded=True):
            obs = fetch_observed_high(city)
            nws = fetch_nws_high(city)
            model_highs = fetch_model_forecasts(city)
            nws_high = float(nws.get("high")) if nws and nws.get("high") else None
            nws_grid = fetch_nws_gridpoint(city)
            obs_high = obs.get("wethr_high") if obs else None
            obs_high_f = float(obs_high) if obs_high else None

            if len(model_highs) < 3:
                st.error("Insufficient models for blend.")
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

            # ============= FULL SUMMARY IN BOXES =============
            col1, col2, col3 = st.columns(3)
            col1.metric("Blend (weighted)", f"{blend:.1f}°F", delta=None)
            col2.metric("Spread / Std", f"{spread:.1f}°F / {std:.1f}°F")
            col3.metric("Confidence in band", f"~{prob_in_band}%")

            st.subheader("Status & Changes")
            st.markdown(f"**Status:** {status}")
            st.markdown(f"NWS vs blend diff: {diff_nws:.1f}°F" if diff_nws is not None else "N/A")

            if status == "GREEN":
                st.success("✅ GREEN — models + NWS tightly aligned.")
            elif status == "YELLOW":
                st.warning("🟡 YELLOW — usable but not ideal; size carefully.")
            else:
                st.error("🔴 RED — noisy setup; consider skipping or tiny size only.")

            st.subheader("Suggested Range & Bins")
            st.markdown(f"Comfort band: **{band[0]}–{band[1]}°F**")
            st.markdown(f"Primary bin: **{center}–{center+1}°F**")
            st.markdown(f"Secondary bin: **{center-1}–{center}°F**")

            st.subheader("Bin Lean Guide (YES/NO)")
            # Simple list (expand if needed)
            st.markdown("- Low bins below obs: INVALID")
            st.markdown(f"- Primary ({center}–{center+1}): LEAN YES")
            st.markdown(f"- Secondary: SMALL YES / avoid NO")

            st.subheader("Exact & Safe Calls")
            st.markdown(f"Exact: **71–72°F YES — A (centered)**")  # update dynamically if needed
            st.markdown(f"Safe: **75°F or below YES — B SAFE**")

            st.subheader("Kalshi Market Snapshot")
            # Add real fetch later; for now placeholder
            st.markdown("Kalshi data loading... (full snapshot coming soon)")

            st.subheader("Timing Note")
            # Add real timing note function if needed
            st.markdown("Late in the day; high likely close to final.")

    st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (refreshes on page load)")
