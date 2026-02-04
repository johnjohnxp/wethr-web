# Auto-log predictions to CSV (appends new rows each run)
if log_rows:
    file_exists = os.path.isfile(LOG_FILE) and os.path.getsize(LOG_FILE) > 0

    with open(LOG_FILE, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=log_rows[0].keys())

        # Write header only if file is new or empty
        if not file_exists:
            writer.writeheader()

        writer.writerows(log_rows)

    # Optional: show confirmation (can remove later)
    st.success(f"Logged {len(log_rows)} cities to prediction_log.csv")

    # Show download button
    with open(LOG_FILE, 'rb') as f:
        st.download_button(
            label="Download Full Prediction Log (CSV)",
            data=f,
            file_name="prediction_log.csv",
            mime="text/csv",
            key="download_log"
        )
