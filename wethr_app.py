import streamlit as st
import json
import os
import time
from datetime import datetime, timedelta, timezone
from statistics import stdev
import requests
import re
import pandas as pd
import warnings

warnings.filterwarnings("ignore", category=Warning)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    class ZoneInfo(str):
        def __new__(cls, name):
            return str.__new__(cls, name)

from dataclasses import dataclass

# CONFIG
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
    model_hourly = {m: [] for m in TARGET_MODELS}
    for rec in records:
        model = rec.get("model")
        if model not in TARGET_MODELS: continue
        temp_raw = rec.get("temperature_f")
        if temp_raw is None: continue
        try:
            temp = float(temp_raw)
        except: continue
        valid_time = rec.get("valid_time")
        model_hourly[model].append((valid_time, temp))
        if model_highs[model] is None or temp > model_highs[model]:
            model_highs[model] = temp
    highs = {m: v for m, v in model_highs.items() if v is not None}
    return highs, model_hourly  # FIX: Return both to match unpack

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
    except Exception as e:
        st.warning(f"NWS gridpoint error: {e}")
        return None

# KALSHI FETCH
def fetch_kalshi_market(city_name, blend, status, exact_bin_str, safe_play_str, exact_grade):
    KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
    series_ticker_map = {
        "Seattle": "KXHIGHTSEA",
        "San Francisco": "KXHIGHTSFO",
        "Washington DC": "KXHIGHTDCA",
        "New Orleans": "KXHIGHTMSY",
        "Las Vegas": "KXHIGHTLAS",
        "Miami": "KXHIGHMIA",
    }
    series_ticker = series_ticker_map.get(city_name)
    if not series_ticker:
        return f"No ticker for {city_name}."

    try:
        url = f"{KALSHI_BASE}/markets?series_ticker={series_ticker}&status=open&limit=50"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        markets = r.json().get("markets", [])
        if not markets:
            return f"No open markets for {series_ticker}."

        bin_dict = {}
        implied_high = None
        for m in markets:
            title = m.get("title", "").lower()
            match = re.search(r'(\d+)[ -]to[ -](\d+)', title) or re.search(r'(\d+)-(\d+)', title)
            if match:
                low, high = int(match.group(1)), int(match.group(2))
                bin_key = f"{low}-{high}"
            else:
                continue
            last = m.get("last_price")
            bid = m.get("yes_bid", 0) / 100
            ask = m.get("yes_ask", 0) / 100
            yes_prob = last / 100 if last is not None else (bid + ask) / 2
            vol = m.get("volume", 0)
            if bin_key in bin_dict:
                old = bin_dict[bin_key]
                old['yes_prob'] = (old['yes_prob'] + yes_prob) / 2
                old['volume'] = max(old['volume'], vol)
                old['bid'] = (old['bid'] + bid) / 2
                old['ask'] = (old['ask'] + ask) / 2
            else:
                bin_dict[bin_key] = {'yes_prob': yes_prob, 'bid': bid, 'ask': ask, 'volume': vol}
            if yes_prob > (implied_high or 0):
                implied_high = (low + high) / 2
        if not bin_dict:
            return "No parsable bins."
        bin_data = sorted(bin_dict.items(), key=lambda x: int(x[0].split('-')[0]))
        snapshot = f"Live Kalshi Bins for {city_name}:\n\n"
        for bin_key, b in bin_data:
            prob = f"{b['yes_prob']:.0%}" if b['yes_prob'] > 0 else "N/A"
            ba = f"bid {b['bid']:.2f}–ask {b['ask']:.2f}" if b['bid'] or b['ask'] else ""
            snapshot += f"  {bin_key}°F: Yes {prob} {ba} (vol {b['volume']})\n"
        snapshot += f"\nMarket-implied high: ~{implied_high:.1f}°F\n"
        if status != "RED" and blend is not None:
            snapshot += f"→ Your blend: {blend:.1f}°F\n"
            if exact_bin_str != "--":
                target = exact_bin_str.split(' —')[0].replace('–', '-')
                if target in bin_dict:
                    m = bin_dict[target]
                    snapshot += f"→ Exact: {target} YES at {m['yes_prob']:.0%} ({exact_grade})\n"
                else:
                    closest_key = min(bin_dict, key=lambda k: abs((int(k.split('-')[0]) + int(k.split('-')[1])) / 2 - blend))
                    closest = bin_dict[closest_key]
                    diff = abs((int(closest_key.split('-')[0]) + int(closest_key.split('-')[1])) / 2 - blend)
                    snapshot += f"→ Closest: {closest_key} at {closest['yes_prob']:.0%} (Δ {diff:.1f}°F)\n"
            if safe_play_str != "--" and "below" in safe_play_str.lower():
                try:
                    cut = int(safe_play_str.split('°')[0])
                    covered = sum(v['yes_prob'] for k, v in bin_dict.items() if int(k.split('-')[1]) <= cut)
                    covered = min(1.0, covered)
                    snapshot += f"→ Safe ≤{cut}°F market {covered:.0%}\n"
                except:
                    pass
        return snapshot
    except Exception as e:
        return f"Kalshi error: {str(e)}"

# ============= STREAMLIT APP =============
st.set_page_config(page_title="Wethr Helper", layout="wide")
st.title("Wethr Helper Dashboard")
st.caption("Latest weather blends, NWS backup, and Kalshi markets. Refreshes on page load or button press.")

# Sidebar auto-refresh
st.sidebar.header("Auto-Refresh")
refresh_interval = st.sidebar.selectbox(
    "Refresh every",
    options=["Off", "5 minutes", "10 minutes", "15 minutes", "30 minutes"],
    index=3  # Default 15 min
)

if refresh_interval != "Off":
    interval_map = {"5 minutes": 300, "10 minutes": 600, "15 minutes": 900, "30 minutes": 1800}
    st.sidebar.info(f"Auto-refreshing every {refresh_interval}. Next in ~{interval_map[refresh_interval]//60} min.")
    time.sleep(interval_map[refresh_interval])
    st.rerun()

selected_cities = st.multiselect("Select Cities", [c.name for c in CITY_PRESETS], default=["Miami", "Seattle"])

if st.button("Refresh Data Now"):
    st.rerun()

if not selected_cities:
    st.warning("Select at least one city.")
else:
    summary_data = []
    for city_name in selected_cities:
        city = next(c for c in CITY_PRESETS if c.name == city_name)
        with st.expander(f"📍 {city.name} - Detailed Report", expanded=True):
            obs = fetch_observed_high(city)
            nws = fetch_nws_high(city)
            model_highs, model_hourly = fetch_model_forecasts(city)
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

            kalshi_snapshot = fetch_kalshi_market(city_name, blend, status, "TODO exact", "TODO safe", "TODO grade")

            # Metrics
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Blend", f"{blend:.1f}°F")
            col2.metric("Spread", f"{spread:.1f}°F")
            col3.metric("Confidence", f"~{prob_in_band}%")
            col4.metric("Observed", f"{obs_high_f or 'N/A'}°F")

            st.markdown(f"**Status:** {status}")
            if status == "GREEN":
                st.success("✅ GREEN — models + NWS tightly aligned.")
            elif status == "YELLOW":
                st.warning("🟡 YELLOW — usable but not ideal.")
            else:
                st.error("🔴 RED — noisy setup.")

            # Expander for extra notes
            with st.expander("Extra Details & Notes"):
                st.markdown("**Model Highs**")
                model_df = pd.DataFrame([
                    {"Model": m, "High": f"{model_highs.get(m, 'N/A'):.1f}°F" if model_highs.get(m) else "N/A"}
                    for m in TARGET_MODELS
                ])
                st.table(model_df)

                st.markdown("**Suggested Range**")
                st.markdown(f"Comfort band: **{band[0]}–{band[1]}°F**")

                st.markdown("**Bin Lean Guide**")
                st.markdown("- Primary: LEAN YES")
                st.markdown("- Secondary: SMALL YES / avoid NO")

                st.markdown("**Exact & Safe**")
                st.markdown(f"Exact: **{center}–{center+1}°F YES** (A/B grade)")
                st.markdown(f"Safe: **{center+4}°F or below YES** (B SAFE)")

                st.markdown("**Timing Note**")
                st.markdown("Late in the day; high likely close to final.")

                st.markdown("**Full Kalshi Snapshot**")
                st.markdown(kalshi_snapshot)

            # Collect for bottom summary
            summary_data.append({
                "City": city_name,
                "Blend": blend,
                "Spread": spread,
                "Status": status,
                "Band": f"{band[0]}–{band[1]}°F",
                "Confidence": prob_in_band,
                "Observed": obs_high_f or "N/A",
                "Kalshi": kalshi_snapshot[:200] + "..." if kalshi_snapshot else "N/A"
            })

    # Bottom summary box (best to worst)
    if summary_data:
        st.markdown("### All Cities Summary (Best → Worst)")
        df = pd.DataFrame(summary_data)
        df['status_order'] = df['Status'].map({'GREEN': 0, 'YELLOW': 1, 'RED': 2})
        df = df.sort_values(['status_order', 'Spread'])
        df = df.drop(columns=['status_order'])

        def color_status(val):
            if val == 'GREEN': return 'background-color: #90EE90'
            elif val == 'YELLOW': return 'background-color: #FFFF99'
            elif val == 'RED': return 'background-color: #FF9999'
            return ''

        styled_df = df.style.applymap(color_status, subset=['Status'])
        st.dataframe(styled_df, use_container_width=True)

st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (refreshes on page load)")
