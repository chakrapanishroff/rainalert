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
from pathlib import Path

import streamlit as st

import rain_alert as ra

st.set_page_config(page_title="Rain Alert", page_icon="🌧️", layout="centered")

# Open-Meteo limits: up to 92 past days and 16 forecast days (today counts as day 1).
MAX_PAST_DAYS = 92
MAX_FORECAST_DAYS = 16

# Every location you save is appended here (next to this script), one per line.
LOCATIONS_FILE = Path(__file__).resolve().parent / "saved_locations.txt"
LOCATIONS_HEADER = "# Rain Alert saved locations — one per line: label | latitude | longitude\n"
SAVED_CHOICE_KEY = "saved_loc_choice"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def load_config() -> ra.AppConfig:
    """Reuse the same on-disk config the CLI writes, so both stay in sync."""
    if "config" not in st.session_state:
        st.session_state.config = ra.AppConfig.load()
    return st.session_state.config


@st.cache_data(ttl=1800, show_spinner=False)  # Cache forecasts for 30 minutes
def fetch_forecast(lat: float, lon: float, address: str, forecast_days: int = 2, past_days: int = 0) -> dict:
    """Cached for 10 minutes so slider/toggle changes don't re-hit the API.

    Plain floats/strings as args (not the Location dataclass) keep the
    cache key hashable. forecast_days/past_days are part of the key so
    switching views re-fetches only when actually needed.
    """
    loc = ra.Location(address=address, latitude=lat, longitude=lon)
    return ra.WeatherService.get_hourly_forecast(loc, forecast_days=forecast_days, past_days=past_days)


def _loc_key(loc: ra.Location) -> tuple[float, float]:
    return (round(loc.latitude, 4), round(loc.longitude, 4))


def location_option_label(loc: ra.Location) -> str:
    return f"{loc.address} ({loc.latitude:.4f}, {loc.longitude:.4f})"


def load_saved_locations() -> list[ra.Location]:
    """Read saved_locations.txt; blank lines, # comments and malformed lines are skipped."""
    if not LOCATIONS_FILE.exists():
        return []
    locs: list[ra.Location] = []
    for line in LOCATIONS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.rsplit("|", 2)]
        if len(parts) != 3:
            continue
        try:
            lat, lon = float(parts[1]), float(parts[2])
        except ValueError:
            continue
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            locs.append(ra.Location(address=parts[0] or f"({lat:.4f}, {lon:.4f})", latitude=lat, longitude=lon))
    return locs


def add_saved_location(loc: ra.Location) -> bool:
    """Append to the text file (or update the label if the same coordinates exist)."""
    locs = load_saved_locations()
    for i, existing in enumerate(locs):
        if _loc_key(existing) == _loc_key(loc):
            locs[i] = loc
            break
    else:
        locs.append(loc)
    lines = [
        f"{l.address.replace('|', '-')} | {l.latitude:.4f} | {l.longitude:.4f}"
        for l in locs
    ]
    try:
        LOCATIONS_FILE.write_text(LOCATIONS_HEADER + "\n".join(lines) + "\n", encoding="utf-8")
        return True
    except OSError as exc:
        st.warning(f"Could not write {LOCATIONS_FILE.name}: {exc}")
        return False


def remember_location(loc: ra.Location) -> None:
    """Persist a newly saved location and make the dropdown show it as selected."""
    add_saved_location(loc)
    st.session_state[SAVED_CHOICE_KEY] = location_option_label(loc)


def _on_pick_saved() -> None:
    """Dropdown callback: switch the active location to the chosen saved one."""
    choice = st.session_state.get(SAVED_CHOICE_KEY)
    for loc in load_saved_locations():
        if location_option_label(loc) == choice:
            cfg = st.session_state.config
            cfg.location = loc
            cfg.save()
            break


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

    # Filled in after the save forms below, so a fresh save can pre-select itself.
    saved_slot = st.container()

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
                    remember_location(config.location)
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
            remember_location(config.location)
            fetch_forecast.clear()
            st.success(f"Saved: {config.location.address}")

    # Saved-locations dropdown (rendered at the top of the sidebar via saved_slot).
    # First run: seed the file with the currently active location, if it isn't there.
    saved = load_saved_locations()
    if config.location and _loc_key(config.location) not in {_loc_key(l) for l in saved}:
        add_saved_location(config.location)
        saved = load_saved_locations()

    with saved_slot:
        if saved:
            options = [location_option_label(l) for l in saved]
            if st.session_state.get(SAVED_CHOICE_KEY) not in options + [None]:
                del st.session_state[SAVED_CHOICE_KEY]  # stale after the file was edited
            kwargs = {}
            if SAVED_CHOICE_KEY not in st.session_state:
                active = _loc_key(config.location) if config.location else None
                kwargs["index"] = next((i for i, l in enumerate(saved) if _loc_key(l) == active), None)
            st.selectbox(
                "📌 Saved locations",
                options,
                key=SAVED_CHOICE_KEY,
                on_change=_on_pick_saved,
                placeholder="Choose a saved location",
                **kwargs,
            )
            st.caption(f"Stored in `{LOCATIONS_FILE.name}` next to the app.")
        else:
            st.caption("No saved locations yet — save one below and it will appear here.")

    st.divider()

    forecast_range = st.radio(
        "Forecast range",
        ["Yesterday", "Today & Tomorrow", "Next 7 Days", "Custom range"],
        index=1,
    )

    custom_range = None
    if forecast_range == "Custom range":
        _today = date.today()
        custom_range = st.date_input(
            "Date range (from – to)",
            value=(_today, _today + timedelta(days=2)),
            min_value=_today - timedelta(days=MAX_PAST_DAYS),
            max_value=_today + timedelta(days=MAX_FORECAST_DAYS - 1),
            format="DD/MM/YYYY",
            help=(
                f"Pick a start and end date on the calendar. Supports up to "
                f"{MAX_PAST_DAYS} days back and {MAX_FORECAST_DAYS - 1} days ahead. "
                "Past dates are model estimates, not saved forecasts."
            ),
        )

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

today = date.today()

forecast_days = 7 if forecast_range == "Next 7 Days" else (1 if forecast_range == "Yesterday" else 2)
past_days = 1 if forecast_range == "Yesterday" else 0

range_start = range_end = None
if forecast_range == "Custom range":
    # st.date_input returns a 1-tuple while the user has only clicked the start date.
    if not isinstance(custom_range, (tuple, list)) or len(custom_range) != 2:
        st.info("📅 Pick both a start and an end date on the calendar (sidebar).")
        st.stop()
    range_start, range_end = custom_range
    # Translate the chosen dates into Open-Meteo's past_days / forecast_days.
    past_days = max(0, (today - range_start).days)
    forecast_days = max(1, (range_end - today).days + 1)

try:
    with st.spinner("Fetching forecast…"):
        forecast = fetch_forecast(
            config.location.latitude,
            config.location.longitude,
            config.location.address,
            forecast_days,
            past_days,
        )
except ra.WeatherServiceError as exc:
    st.error(f"⚠️ {exc}")
    st.stop()

if forecast_range == "Custom range":
    num_days = (range_end - range_start).days + 1
    st.subheader(f"{range_start.strftime('%a, %d %b %Y')} → {range_end.strftime('%a, %d %b %Y')}")
    st.caption(f"{num_days} day(s). Past dates are model estimates; future dates are forecasts.")

    # Summary: count rainy days across the range
    rainy_days = sum(
        1 for i in range(num_days)
        if ra.find_rain_windows(forecast, config.rain_threshold, range_start + timedelta(days=i))
    )
    st.metric("Rainy days in range", f"{rainy_days} / {num_days}")

    for i in range(num_days):
        d = range_start + timedelta(days=i)
        if d < today:
            header = "YESTERDAY" if d == today - timedelta(days=1) else d.strftime("%A").upper()
            st.subheader(f"{header} — {d.strftime('%a, %d %b')}")
            label = "yesterday" if d == today - timedelta(days=1) else f"on {d.strftime('%A, %d %b')}"
            st.text(ra.format_past_day_report(forecast, config.location, config.rain_threshold, d, label))
        else:
            offset = (d - today).days
            label, is_today = ra.day_label_for_offset(d, offset)
            header = "TODAY" if offset == 0 else "TOMORROW" if offset == 1 else d.strftime("%A").upper()
            st.subheader(f"{header} — {d.strftime('%a, %d %b')}")
            render_day_text(forecast, d, config.rain_threshold, label, is_today=is_today)
        if i < num_days - 1:
            st.divider()
elif forecast_range == "Yesterday":
    yesterday = today - timedelta(days=1)
    st.subheader(f"YESTERDAY — {yesterday.strftime('%a, %d %b')}")
    st.caption("Recent-past estimate from the weather model, not a saved earlier forecast.")
    report = ra.format_past_day_report(forecast, config.location, config.rain_threshold, yesterday, "yesterday")
    st.text(report)
elif forecast_range == "Next 7 Days":
    for offset in range(7):
        d = today + timedelta(days=offset)
        label, is_today = ra.day_label_for_offset(d, offset)
        header = "TODAY" if offset == 0 else "TOMORROW" if offset == 1 else d.strftime("%A").upper()
        st.subheader(f"{header} — {d.strftime('%a, %d %b')}")
        render_day_text(forecast, d, config.rain_threshold, label, is_today=is_today)
        if offset < 6:
            st.divider()
else:
    tomorrow = today + timedelta(days=1)

    st.subheader(f"TODAY — {today.strftime('%a, %d %b')}")
    render_day_text(forecast, today, config.rain_threshold, "today", is_today=True)

    st.divider()

    st.subheader(f"TOMORROW — {tomorrow.strftime('%a, %d %b')}")
    render_day_text(forecast, tomorrow, config.rain_threshold, "tomorrow", is_today=False)
