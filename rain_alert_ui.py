#!/usr/bin/env python3
"""
Rain Alert — Streamlit UI
=========================

A web front-end for rain_alert.py. All forecast logic (geocoding, hourly
lookup, rain-window detection) is imported from rain_alert.py, so the CLI
and the UI can never drift apart.

Run it with:
    streamlit run rain_alert_ui.py

Dependencies:
    pip install streamlit requests
"""

from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

import rain_alert as ra

st.set_page_config(page_title="Rain Alert", page_icon="🌧️", layout="centered")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def load_config() -> ra.AppConfig:
    """Reuse the same on-disk config the CLI writes, so both stay in sync."""
    if "config" not in st.session_state:
        st.session_state.config = ra.AppConfig.load()
    return st.session_state.config


@st.cache_data(ttl=600, show_spinner=False)
def fetch_forecast(lat: float, lon: float, address: str) -> dict:
    """Cached for 10 minutes so slider changes don't re-hit the API.

    Plain floats/strings as args (not the Location dataclass) keep the
    cache key hashable.
    """
    loc = ra.Location(address=address, latitude=lat, longitude=lon)
    return ra.WeatherService.get_hourly_forecast(loc, forecast_days=2)


def render_day_text(forecast: dict, on_date: date, threshold: int, day_label: str, is_today: bool) -> None:
    windows = ra.find_rain_windows(forecast, threshold, on_date)
    report = ra.format_rain_report(windows, config.location, threshold, day_label, is_today=is_today)
    st.text(report)


# --------------------------------------------------------------------------- #
# Sidebar — location & threshold
# --------------------------------------------------------------------------- #

config = load_config()

with st.sidebar:
    st.header("⚙️ Settings")

    mode = st.radio("Set location by", ["Address", "Coordinates"], horizontal=True)

    if mode == "Address":
        address = st.text_input("Address", placeholder="Koramangala, Bengaluru")
        if st.button("Save location", use_container_width=True):
            if not address.strip():
                st.error("Enter an address first.")
            else:
                try:
                    with st.spinner("Looking up…"):
                        config.location = ra.WeatherService.geocode(address)
                    config.save()
                    fetch_forecast.clear()
                    st.success(f"Saved: {config.location.address}")
                except ra.WeatherServiceError as exc:
                    st.error(f"{exc}\n\nTip: switch to Coordinates for small localities.")
    else:
        col_a, col_b = st.columns(2)
        lat = col_a.number_input("Latitude", value=12.8817, format="%.4f", min_value=-90.0, max_value=90.0)
        lon = col_b.number_input("Longitude", value=77.5250, format="%.4f", min_value=-180.0, max_value=180.0)
        label = st.text_input("Label (optional)", placeholder="Thurahalli")
        if st.button("Save location", use_container_width=True):
            config.location = ra.Location(
                address=label.strip() or f"({lat:.4f}, {lon:.4f})",
                latitude=lat,
                longitude=lon,
            )
            config.save()
            fetch_forecast.clear()
            st.success(f"Saved: {config.location.address}")

    st.divider()

    threshold = st.slider(
        "Rain threshold (%)",
        min_value=0,
        max_value=100,
        value=config.rain_threshold,
        step=5,
        help="An hour counts as 'rainy' at or above this probability.",
    )
    if threshold != config.rain_threshold:
        config.rain_threshold = threshold
        config.save()

    if st.button("🔄 Refresh forecast", use_container_width=True):
        fetch_forecast.clear()
        st.rerun()

    if config.location:
        st.caption(
            f"📍 {config.location.address}\n\n"
            f"{config.location.latitude:.4f}, {config.location.longitude:.4f}"
        )


# --------------------------------------------------------------------------- #
# Main panel
# --------------------------------------------------------------------------- #

st.title("🌧️ Rain Alert")

if not config.location:
    st.info("👈 Set a location in the sidebar to get started.")
    st.stop()

st.caption(f"Forecast for **{config.location.address}** · threshold {config.rain_threshold}%")

try:
    with st.spinner("Fetching forecast…"):
        forecast = fetch_forecast(
            config.location.latitude,
            config.location.longitude,
            config.location.address,
        )
except ra.WeatherServiceError as exc:
    st.error(f"⚠️ {exc}")
    st.stop()

today = date.today()
tomorrow = today + timedelta(days=1)

st.subheader(f"TODAY — {today.strftime('%a, %d %b')}")
render_day_text(forecast, today, config.rain_threshold, "today", is_today=True)

st.divider()

st.subheader(f"TOMORROW — {tomorrow.strftime('%a, %d %b')}")
render_day_text(forecast, tomorrow, config.rain_threshold, "tomorrow", is_today=False)

