import streamlit as st
import json
import os
import time
from datetime import datetime, timedelta, timezone
from statistics import stdev
from collections import Counter
import requests
import re
import pandas as pd
import warnings
import csv
from hashlib import sha256

warnings.filterwarnings("ignore", category=Warning)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    class ZoneInfo:
        def __init__(self, name):
            self.name = name

from dataclasses import dataclass

# ==================== LOGIN ====================
CORRECT_USERNAME = "admin"
CORRECT_PASSWORD = "snc2006"

LOGIN_TOKEN = sha256((CORRECT_USERNAME + CORRECT_PASSWORD).encode()).hexdigest()[:16]

if 'token' in st.query_params and st.query_params['token'][0] == LOGIN_TOKEN:
    if 'logged_in' not in st.session_state:
        st.session_state.logged_in = True
else:
    if 'logged_in' not in st.session_state:
        st.session_state.logged_in = False

if not st.session_state.logged_in:
    st.title("Login to Wethr Helper")
    with st.form(key="login_form"):
        username = st.text_input("Username", placeholder="Enter username")
        password = st.text_input("Password", type="password", placeholder="Enter password")
        submit = st.form_submit_button("Login")

        if submit:
            if username == CORRECT_USERNAME and password == CORRECT_PASSWORD:
                st.session_state.logged_in = True
                st.query_params["token"] = LOGIN_TOKEN
                st.success("Logged in! Refreshing...")
                st.rerun()
            else:
                st.error("Incorrect credentials.")
    st.stop()

# ==================== DASHBOARD ====================
st.set_page_config(page_title="Wethr Helper", layout="wide")
st.title("Wethr Helper Dashboard")
st.caption("Latest weather blends, NWS backup, and Kalshi markets. All cities shown automatically. GREEN expand on load. Refreshes on page load or button press.")

# CONFIG
API_KEY = "570b45680d41097ee46550e36f7c1290754081becee8955529b0d197cf9d8efd"
OBS_URL = "https://wethr.net/api/v2/observations.php"
FORECASTS_URL = "https://wethr.net/api/v2/forecasts.php"
NWS_URL = "https://wethr.net/api/v2/nws_forecasts.php"
TARGET_MODELS = ["HRRR", "NAM", "NBM", "ECMWF-IFS"]
MODEL_WEIGHTS_BASE = {'HRRR': 0.35, 'NAM': 0.25, 'NBM': 0.25, 'ECMWF-IFS': 0.15}
LOG_FILE = "prediction_log.csv"

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
    CityConfig("New York City", "KNYC", "KNYC", "America/New_York", "OKX/97,71", "40.7789,-73.9692"),
    CityConfig("Chicago", "KORD", "KORD", "America/Chicago", "LOT/41,74", "41.8781,-87.6298"),
    CityConfig("Boston", "KBOS", "KBOS", "America/New_York", "BOX/90,71", "42.3601,-71.0589"),
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

def fetch_data(url, params, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=auth_headers(), timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            st.warning(f"Fetch error on {url} (attempt {attempt+1}): {e}")
            time.sleep(2)
    st.error(f"Failed {url} after {retries} attempts.")
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
    model_dew = {m: [] for m in TARGET_MODELS}
    model_wind = {m: [] for m in TARGET_MODELS}
    model_cloud = {m: [] for m in TARGET_MODELS}
    for rec in records:
        model = rec.get("model")
        if model not in TARGET_MODELS: continue
        temp_raw = rec.get("temperature_f")
        dew_raw = rec.get("dew_point_f")
        wind_raw = rec.get("wind_speed_kt")
        cloud_raw = rec.get("cloud_cover")
        if temp_raw is None: continue
        try:
            temp = float(temp_raw)
            dew = float(dew_raw) if dew_raw is not None else None
            wind = float(wind_raw) if wind_raw is not None else None
            cloud = float(cloud_raw) if cloud_raw is not None else None
        except: continue
        valid_time = rec.get("valid_time")
        if valid_time is None: continue
        try:
            valid_time = datetime.fromisoformat(valid_time.replace("Z", "+00:00"))
        except: continue
        model_hourly[model].append((valid_time, temp))
        if dew is not None: model_dew[model].append((valid_time, dew))
        if wind is not None: model_wind[model].append((valid_time, wind))
        if cloud is not None: model_cloud[model].append((valid_time, cloud))
        if model_highs[model] is None or temp > model_highs[model]:
            model_highs[model] = temp
    highs = {m: v for m, v in model_highs.items() if v is not None}
    return highs, model_hourly, model_dew, model_wind, model_cloud

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

def fetch_kalshi_market(city_name, blend, status, exact_bin_str, safe_play_str, exact_grade):
    KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
    series_ticker_map = {
        "Seattle": "KXHIGHTSEA",
        "San Francisco": "KXHIGHTSFO",
        "Washington DC": "KXHIGHTDC",
        "New Orleans": "KXHIGHTNOLA",
        "Las Vegas": "KXHIGHTLV",
        "Miami": "KXHIGHMIA",
        "New York City": "KXHIGHTNYC",
        "Chicago": "KXHIGHTCHI",
        "Boston": "KXHIGHTBOS",
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

def fetch_gefs_probs(lat, lon):
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&hourly=temperature_2m"
            f"&temperature_unit=fahrenheit"
            f"&forecast_days=1"
            f"&ensemble=true"
        )
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        hourly_temps = data.get("hourly", {}).get("temperature_2m", [])
        if not hourly_temps or len(hourly_temps) < 10:
            st.warning("GEFS returned empty or incomplete data")
            return {}, 0, None

        daily_maxes = []
        step = max(1, len(hourly_temps) // 30)  # Prevent zero step
        for i in range(0, len(hourly_temps), step):
            member_temps = hourly_temps[i:i+48]  # Approx 48 hours
            if member_temps:
                daily_maxes.append(max(member_temps))

        if not daily_maxes:
            return {}, 0, None

        bin_counts = Counter(round(max_temp) for max_temp in daily_maxes)
        total = len(daily_maxes)
        probs = {f"{k}-{k+1}": (count / total * 100) for k, count in sorted(bin_counts.items())}
        gefs_mean = sum(daily_maxes) / total  # GEFS average high
        return probs, total, gefs_mean
    except Exception as e:
        st.warning(f"GFS ensemble error: {e}")
        return {}, 0, None

def make_time_note(city: CityConfig, obs, band):
    tz = ZoneInfo(city.timezone)
    now_local = datetime.now(tz)
    obs_high = obs.get("wethr_high")
    time_high_utc = obs.get("time_of_high_utc")
    dt_high_utc = None

    if time_high_utc:
        try:
            if time_high_utc.endswith("Z"):
                time_high_utc = time_high_utc.replace("Z", "+00:00")
            dt_high_utc = datetime.fromisoformat(time_high_utc)
        except:
            dt_high_utc = None

    if obs_high is None:
        return "Observed high missing — check back later."

    try:
        obs_high_f = float(obs_high)
    except:
        return "Observed high not numeric."

    # Early day override (before 11 AM) — always upside potential
    if now_local.hour <= 11:
        if obs_high_f < (band[0] if band else 50) - 3:
            return "Early in the day and obs are still well below the suggested band; plenty of runway left."
        else:
            return "Early in the day; temps are starting to rise, but plenty of time left for the high."

    # Midday (11 AM – 4 PM)
    if 11 <= now_local.hour < 16:
        if obs_high_f < (band[0] if band else 50):
            return "Midday and obs are still below the suggested band; upside potential remains."
        elif band and band[0] <= obs_high_f <= band[1]:
            return "Midday and obs sit inside the suggested band; careful sizing / management warranted."
        else:
            return "Midday and obs have exceeded the suggested band; watch for overachievement risk."

    # Late day (after 4 PM) — check if high is already set
    if dt_high_utc is not None:
        dt_high_local = dt_high_utc.astimezone(tz)
        hours_since_high = (now_local - dt_high_local).total_seconds() / 3600.0
        if hours_since_high >= 3:
            return "Observed high occurred several hours ago; today's high is likely already set."
    
    if band and obs_high_f >= band[1]:
        return "Late in the day and obs are at/above the suggested band; high is likely in."
    elif band and obs_high_f < band[0]:
        return "Late in the day and obs never reached the suggested band; underperformance vs guidance."
    else:
        return "Late in the day and obs are inside the suggested band; high is likely close to final."

# ============= STREAMLIT APP =============
col_refresh1, col_refresh2 = st.columns([3, 1])
with col_refresh1:
    refresh_interval = st.selectbox(
        "Auto-refresh",
        options=["Off", "5 min", "10 min", "15 min", "30 min"],
        index=0,
        label_visibility="collapsed",
        key="refresh_select"
    )

if refresh_interval != "Off":
    interval_map = {"5 min": 300, "10 min": 600, "15 min": 900, "30 min": 1800}
    interval_seconds = interval_map[refresh_interval]
    countdown_placeholder = st.empty()
    countdown_placeholder.markdown(f"Next refresh in...")

    countdown_js = f"""
    <script>
    const seconds = {interval_seconds};
    let remaining = seconds;
    const timer = setInterval(() => {{
        remaining--;
        document.getElementById("countdown").innerText = Math.floor(remaining / 60) + " min " + (remaining % 60) + " sec";
        if (remaining <= 0) {{
            clearInterval(timer);
            window.location.reload();
        }}
    }}, 1000);
    </script>
    <div id="countdown"></div>
    """
    countdown_placeholder.markdown(countdown_js, unsafe_allow_html=True)

selected_cities = [c.name for c in CITY_PRESETS]

if st.button("Refresh Data Now"):
    st.rerun()

summary_data = []
gefs_summary = []
log_rows = []

for city_name in selected_cities:
    city = next(c for c in CITY_PRESETS if c.name == city_name)

    obs = fetch_observed_high(city)
    nws = fetch_nws_high(city)
    model_highs, model_hourly, model_dew, model_wind, model_cloud = fetch_model_forecasts(city)
    nws_high = float(nws.get("high")) if nws and nws.get("high") else None
    nws_grid = fetch_nws_gridpoint(city)
    obs_high = obs.get("wethr_high") if obs else None
    obs_high_f = float(obs_high) if obs_high else None

    if len(model_highs) < 3:
        summary_data.append({"City": city_name, "Original Blend": "N/A", "Blended Model": "N/A", "Spread": "N/A", "Status": "ERROR", "Band": "N/A", "Confidence": "N/A", "Observed": "N/A", "Kalshi": "N/A"})
        gefs_summary.append({"City": city_name, "GEFS Top Probs": "N/A", "Members": 0})
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

    rise_rate = 0
    if 'HRRR' in model_hourly and len(model_hourly['HRRR']) >= 3:
        recent = sorted(model_hourly['HRRR'][-3:], key=lambda x: x[0])
        time_diff = (recent[-1][0] - recent[0][0]).total_seconds() / 3600
        if time_diff > 0:
            rise_rate = (recent[-1][1] - recent[0][1]) / time_diff
    if rise_rate > 1.5:
        adjustment = min(1.0, rise_rate * 0.3)
        blend += adjustment
        st.info(f"Rise rate {rise_rate:.1f}°F/hr — blend adjusted +{adjustment:.1f}°F")

    dew_bias = wind_bias = cloud_bias = 0
    for m in TARGET_MODELS:
        if model_dew.get(m) and len(model_dew[m]) > 0:
            avg_dew = sum(d[1] for d in model_dew[m]) / len(model_dew[m])
            if avg_dew > 65: dew_bias -= 0.5
            elif avg_dew < 50: dew_bias += 0.3
        if model_wind.get(m) and len(model_wind[m]) > 0:
            avg_wind = sum(w[1] for w in model_wind[m]) / len(model_wind[m])
            if avg_wind > 15: wind_bias -= 0.5
        if model_cloud.get(m) and len(model_cloud[m]) > 0:
            avg_cloud = sum(c[1] for c in model_cloud[m]) / len(model_cloud[m])
            if avg_cloud > 70: cloud_bias -= 0.5
    blend += dew_bias + wind_bias + cloud_bias
    if dew_bias or wind_bias or cloud_bias:
        st.info(f"Biases applied: Dew {dew_bias:.1f}°F, Wind {wind_bias:.1f}°F, Cloud {cloud_bias:.1f}°F")

    lat, lon = city.lat_lon.split(',')
    gefs_probs, num_members, gefs_mean = fetch_gefs_probs(lat, lon)
    blended_mean = blend
    blended_shift = 0.0
    if gefs_mean is not None:
        blended_mean = 0.7 * blend + 0.3 * gefs_mean
        blended_shift = blended_mean - blend
        shift_note = f"+{blended_shift:.1f}°F" if blended_shift > 0 else f"{blended_shift:.1f}°F"
        st.info(f"Blended mean: {blended_mean:.1f}°F ({shift_note} from original — 70% your model + 30% GEFS)")
    gefs_text = "N/A"
    if gefs_probs:
        sorted_probs = sorted(gefs_probs.items(), key=lambda x: x[1], reverse=True)[:5]
        gefs_text = "<br>".join([f"{bin_range}°F: {prob:.0f}%" for bin_range, prob in sorted_probs])
    gefs_summary.append({"City": city_name, "GEFS Top Probs": gefs_text, "Members": num_members})

    diff_nws = abs(blend - nws_high) if nws_high else None
    status = "GREEN" if spread <= 3.0 and (diff_nws or 999) <= 1.5 else \
             "YELLOW" if spread <= 4.0 and (diff_nws or 999) <= 2.0 else "RED"

    center = round(blended_mean)
    band = (center - 1, center + 1)
    prob_in_band = 68 if std < 1.5 else 50 if std < 2.5 else 30

    kalshi_snapshot = fetch_kalshi_market(city_name, blended_mean, status, "TODO exact", "TODO safe", "TODO grade")

    actual_high = obs_high_f if obs_high_f else "Unknown"
    error_original = blend - float(actual_high) if actual_high != "Unknown" else "N/A"
    error_blended = blended_mean - float(actual_high) if actual_high != "Unknown" else "N/A"
    bin_hit = "Yes" if band[0] <= float(actual_high) <= band[1] else "No" if actual_high != "Unknown" else "N/A"
    log_row = {
        "Date": datetime.now().strftime("%Y-%m-%d"),
        "Time": datetime.now().strftime("%H:%M:%S"),
        "City": city_name,
        "Original Blend": round(blend, 1),
        "Blended Model": round(blended_mean, 1),
        "Actual High": actual_high,
        "Original Error (°F)": round(error_original, 1) if isinstance(error_original, (int, float)) else error_original,
        "Blended Error (°F)": round(error_blended, 1) if isinstance(error_blended, (int, float)) else error_blended,
        "Status": status,
        "Confidence": prob_in_band,
        "Bin Hit": bin_hit,
        "Spread": round(spread, 1),
        "NWS Diff": round(diff_nws, 1) if diff_nws is not None else "N/A"
    }
    log_rows.append(log_row)

    summary_data.append({
        "City": city_name,
        "Original Blend": f"{blend:.1f}°F",
        "Blended Model": f"{blended_mean:.1f}°F",
        "Spread": f"{spread:.1f}°F",
        "Status": status,
        "Band": f"{band[0]}–{band[1]}°F",
        "Confidence": f"~{prob_in_band}%",
        "Observed": f"{obs_high_f or 'N/A'}°F",
        "Kalshi": kalshi_snapshot[:200] + "..." if kalshi_snapshot else "N/A"
    })

    with st.expander(f"📍 {city.name} - Detailed Report", expanded=(status == "GREEN")):
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Original Blend", f"{blend:.1f}°F")
        col2.metric("Blended Model", f"{blended_mean:.1f}°F")
        col3.metric("Spread", f"{spread:.1f}°F")
        col4.metric("Observed", f"{obs_high_f or 'N/A'}°F")

        st.markdown(f"**Status:** {status}")
        if status == "GREEN":
            st.success("✅ GREEN — models + NWS tightly aligned.")
        elif status == "YELLOW":
            st.warning("🟡 YELLOW — usable but not ideal.")
        else:
            st.error("🔴 RED — noisy setup.")

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
        note = make_time_note(city, obs, band)
        st.markdown(note)

        st.markdown("**Full Kalshi Snapshot**")
        st.markdown(kalshi_snapshot)

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
    st.dataframe(styled_df, width='stretch')

# Consolidated GEFS summary table
if gefs_summary:
    st.markdown("### GEFS Ensemble Probabilities – All Cities")
    gefs_df = pd.DataFrame(gefs_summary)
    st.dataframe(gefs_df, width='stretch')

# Auto-log predictions to CSV (appends new rows each run)
if log_rows:
    file_exists = os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 0

    with open(LOG_FILE, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=log_rows[0].keys())
        if not file_exists:
            writer.writeheader()
        writer.writerows(log_rows)

    st.success(f"Logged {len(log_rows)} cities to prediction_log.csv")

    with open(LOG_FILE, 'rb') as f:
        st.download_button(
            label="Download Full Prediction Log (CSV)",
            data=f,
            file_name="prediction_log.csv",
            mime="text/csv",
            key="download_log"
        )

st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (refreshes on page load)")
