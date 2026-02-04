# NEW: GEFS Status & Current Adjusted Prediction Summary
st.markdown("### GEFS Status & Current Adjusted Bin Prediction")
st.write(f"**Last GEFS Run Time**: {last_gefs_run}")

for city_name in selected_cities:
    # Get GEFS probs and mean from earlier (you already have gefs_probs from loop)
    # In practice, you'd store them per city in a dict earlier in the loop
    # For simplicity here, re-fetch or use stored value - adjust as needed
    lat, lon = [c.lat_lon for c in CITY_PRESETS if c.name == city_name][0].split(',')
    gefs_probs, _, gefs_mean = fetch_gefs_probs(float(lat), float(lon))

    if not gefs_probs:
        st.write(f"{city_name}: No GEFS data available")
        continue

    # Simple bias correction: shift all GEFS bins toward your blend + current obs
    bias = 0
    if obs_high_f is not None:
        bias += 0.4 * (obs_high_f - gefs_mean)  # pull toward current obs
    if rise_rate > 0:
        bias += 0.2 * rise_rate * 1.5  # project forward a bit
    bias = min(max(bias, -3), 3)  # cap bias adjustment

    adjusted_probs = {}
    for bin_range, prob in gefs_probs.items():
        low, high = map(int, bin_range.split('-'))
        new_low = round(low + bias)
        new_high = round(high + bias)
        new_bin = f"{new_low}-{new_high}"
        adjusted_probs[new_bin] = adjusted_probs.get(new_bin, 0) + prob

    # Sort and get top bins
    sorted_adjusted = sorted(adjusted_probs.items(), key=lambda x: x[1], reverse=True)[:5]
    top_text = "<br>".join([f"{bin_range}°F: {prob:.0f}%" for bin_range, prob in sorted_adjusted])

    # Most likely range (cover ~60–70% probability)
    cumulative = 0
    range_low = None
    range_high = None
    for bin_range, prob in sorted_adjusted:
        low, high = map(int, bin_range.split('-'))
        if range_low is None:
            range_low = low
        range_high = high
        cumulative += prob
        if cumulative >= 65:  # ~65% coverage
            break
    likely_range = f"{range_low}–{range_high}°F ({cumulative:.0f}% prob)"

    st.markdown(f"**{city_name}**")
    st.markdown(f"**Likely Range**: {likely_range}")
    st.markdown(f"**Top Adjusted Bins**:<br>{top_text}")
    st.markdown("---")
