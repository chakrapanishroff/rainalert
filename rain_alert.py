#!/usr/bin/env python3
"""
Rain Alert Chatbot
==================

A conversational assistant that:
  1. Stores your home address (persisted to disk, so you set it once).
  2. Understands natural commands like "today rain", "set location ...",
     "set threshold ...", "help", "exit".
  3. Uses the free Open-Meteo APIs (no API key required) to:
       - geocode your address into latitude/longitude
       - pull hourly precipitation-probability forecasts for today, tomorrow,
         or the next 7 days
       - tell you exactly which hours rain is expected on each day, and
         alert you if rain is likely soon.

Run it with:
    python3 rain_alert_chatbot.py

Dependencies:
    pip install requests
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
import time as time_module
from dataclasses import dataclass, asdict, field
from datetime import datetime, date, timedelta, time as dt_time
from pathlib import Path
from typing import Optional

import requests

try:
    import schedule  # type: ignore
    HAS_SCHEDULE = True
except ImportError:
    HAS_SCHEDULE = False

try:
    from plyer import notification as desktop_notification  # type: ignore
    HAS_PLYER = True
except ImportError:
    HAS_PLYER = False

# --------------------------------------------------------------------------- #
# Configuration & logging
# --------------------------------------------------------------------------- #

CONFIG_PATH = Path.home() / ".rain_alert_chatbot.json"
LOG_PATH = Path.home() / ".rain_alert_chatbot.log"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_RAIN_THRESHOLD = 50  # percent probability considered "rain expected"
REQUEST_TIMEOUT = 10  # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rain_alert_chatbot")


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class Location:
    address: str
    latitude: float
    longitude: float
    timezone: str = "auto"


@dataclass
class AppConfig:
    location: Optional[Location] = None
    rain_threshold: int = DEFAULT_RAIN_THRESHOLD
    alerts_enabled: bool = False
    daily_alert_time: str = "07:00"  # HH:MM, 24-hour

    def save(self, path: Path = CONFIG_PATH) -> None:
        data = {
            "location": asdict(self.location) if self.location else None,
            "rain_threshold": self.rain_threshold,
            "alerts_enabled": self.alerts_enabled,
            "daily_alert_time": self.daily_alert_time,
        }
        path.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "AppConfig":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text())
            loc = Location(**data["location"]) if data.get("location") else None
            return cls(
                location=loc,
                rain_threshold=data.get("rain_threshold", DEFAULT_RAIN_THRESHOLD),
                alerts_enabled=data.get("alerts_enabled", False),
                daily_alert_time=data.get("daily_alert_time", "07:00"),
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning("Could not read saved config (%s); starting fresh.", exc)
            return cls()


# --------------------------------------------------------------------------- #
# Weather service
# --------------------------------------------------------------------------- #

class WeatherServiceError(RuntimeError):
    """Raised when geocoding or forecast retrieval fails."""


class WeatherService:
    """Wraps the Open-Meteo geocoding and forecast APIs."""

    @staticmethod
    def geocode(address: str) -> Location:
        try:
            resp = requests.get(
                GEOCODE_URL,
                params={"name": address, "count": 1, "language": "en", "format": "json"},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise WeatherServiceError(f"Could not reach geocoding service: {exc}") from exc

        results = resp.json().get("results")
        if not results:
            raise WeatherServiceError(f"No location found for '{address}'. Try a more specific address.")

        top = results[0]
        return Location(
            address=f"{top.get('name')}, {top.get('admin1', '')}, {top.get('country', '')}".strip(", "),
            latitude=top["latitude"],
            longitude=top["longitude"],
        )

    @staticmethod
    def get_hourly_forecast(location: Location, forecast_days: int = 2, past_days: int = 0) -> dict:
        """Fetch hourly forecast, optionally including recent past days.

        forecast_days: how many days ahead to include (today counts as day 1).
        past_days: how many days before today to include (0-92). Open-Meteo
        fills these with recent-past reanalysis data — what its weather
        model estimates actually happened, based on observations — not a
        record of what was predicted in advance.
        """
        try:
            resp = requests.get(
                FORECAST_URL,
                params={
                    "latitude": location.latitude,
                    "longitude": location.longitude,
                    "hourly": "precipitation_probability,precipitation,weathercode",
                    "timezone": location.timezone,
                    "forecast_days": forecast_days,
                    "past_days": past_days,
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise WeatherServiceError(f"Could not reach forecast service: {exc}") from exc

        return resp.json()


# --------------------------------------------------------------------------- #
# Rain-alert logic
# --------------------------------------------------------------------------- #

@dataclass
class RainWindow:
    time: datetime
    probability: int
    precipitation_mm: float


def find_rain_windows(forecast: dict, threshold: int, on_date: date) -> list[RainWindow]:
    hourly = forecast.get("hourly", {})
    times = hourly.get("time", [])
    probs = hourly.get("precipitation_probability", [])
    precip = hourly.get("precipitation", [])

    windows: list[RainWindow] = []
    for t, p, mm in zip(times, probs, precip):
        dt = datetime.fromisoformat(t)
        if dt.date() != on_date:
            continue
        if p is not None and p >= threshold:
            windows.append(RainWindow(time=dt, probability=p, precipitation_mm=mm or 0.0))
    return windows


def format_rain_report(
    windows: list[RainWindow],
    location: Location,
    threshold: int,
    day_label: str = "today",
    is_today: bool = True,
) -> str:
    """Format the rain windows for a single day.

    day_label is used in the text ("today", "tomorrow", "Friday", ...).
    The "carry an umbrella now" nudge only makes sense for today, so it is
    suppressed when is_today is False.
    """
    if not windows:
        return f"No rain expected {day_label} in {location.address} (threshold: {threshold}% probability). ☀️"

    lines = [f"🌧️  Rain expected {day_label} in {location.address} (threshold: {threshold}%):"]
    for w in windows:
        lines.append(f"   - {w.time.strftime('%I:%M %p')}: {w.probability}% chance, ~{w.precipitation_mm} mm")

    first = windows[0]
    if is_today:
        now = datetime.now()
        if first.time.hour <= now.hour + 2:
            lines.append(f"⚠️  Heads up: rain likely starting around {first.time.strftime('%I:%M %p')} — carry an umbrella!")
    else:
        total = sum(w.precipitation_mm for w in windows)
        lines.append(f"   ➜ {len(windows)} rainy hour(s), ~{total:.1f} mm total — plan {day_label} accordingly.")
    return "\n".join(lines)


def format_two_day_report(forecast: dict, location: Location, threshold: int) -> str:
    """Build a single report covering both today and tomorrow."""
    today = date.today()
    tomorrow = today + timedelta(days=1)

    today_windows = find_rain_windows(forecast, threshold, today)
    tomorrow_windows = find_rain_windows(forecast, threshold, tomorrow)

    today_part = format_rain_report(today_windows, location, threshold, "today", is_today=True)
    tomorrow_part = format_rain_report(tomorrow_windows, location, threshold, "tomorrow", is_today=False)

    return (
        f"📅 TODAY ({today.strftime('%a, %d %b')})\n"
        f"{today_part}\n\n"
        f"📅 TOMORROW ({tomorrow.strftime('%a, %d %b')})\n"
        f"{tomorrow_part}"
    )


def format_past_day_report(forecast: dict, location: Location, threshold: int, on_date: date, day_label: str) -> str:
    """Report what the model estimates actually happened on a past date.
    Framed in past tense; no 'carry an umbrella' style nudges since the
    day is already over."""
    windows = find_rain_windows(forecast, threshold, on_date)
    if not windows:
        return f"No rain recorded {day_label} in {location.address} (threshold: {threshold}% probability). ☀️"

    lines = [f"🌧️  Rain recorded {day_label} in {location.address} (threshold: {threshold}%):"]
    for w in windows:
        lines.append(f"   - {w.time.strftime('%I:%M %p')}: {w.probability}% chance, ~{w.precipitation_mm} mm")
    total = sum(w.precipitation_mm for w in windows)
    lines.append(f"   ➜ {len(windows)} rainy hour(s), ~{total:.1f} mm total.")
    return "\n".join(lines)
    """Returns (label, is_today) for a date offset days from today.
    offset 0 -> "today", offset 1 -> "tomorrow", offset 2+ -> "on <Weekday, D Mon>".
    """
    if offset == 0:
        return "today", True
    if offset == 1:
        return "tomorrow", False
    return f"on {d.strftime('%A, %d %b')}", False


def format_week_report(forecast: dict, location: Location, threshold: int, days: int = 7) -> str:
    """Build a single report covering `days` consecutive days starting today
    (default 7, i.e. a full week). Requires the forecast to have been fetched
    with forecast_days >= days."""
    today = date.today()
    sections = []
    for offset in range(days):
        d = today + timedelta(days=offset)
        label, is_today = day_label_for_offset(d, offset)
        windows = find_rain_windows(forecast, threshold, d)
        part = format_rain_report(windows, location, threshold, label, is_today=is_today)
        header = "TODAY" if offset == 0 else "TOMORROW" if offset == 1 else d.strftime("%A").upper()
        sections.append(f"📅 {header} ({d.strftime('%a, %d %b')})\n{part}")
    return "\n\n".join(sections)


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #

def _clip(text: str, limit: int) -> str:
    """Collapse newlines and truncate to a safe length for OS notification APIs
    (Windows' NOTIFYICONDATAW caps the message around 256 chars and will raise
    ValueError, in a background thread we can't catch, if given more)."""
    flat = " | ".join(line.strip() for line in text.splitlines() if line.strip())
    if len(flat) <= limit:
        return flat
    return flat[: max(0, limit - 1)].rstrip() + "…"


def send_notification(title: str, message: str) -> None:
    """Try a real desktop notification; gracefully fall back to console output.
    Always prints the full, untruncated message to the console/log regardless,
    since the desktop notification itself must be clipped to a safe length."""
    if HAS_PLYER:
        try:
            desktop_notification.notify(
                title=_clip(title, 60),
                message=_clip(message, 200),
                timeout=10,
            )
        except Exception as exc:  # plyer backends vary a lot by OS; never crash on this
            log.debug("Desktop notification failed (%s); falling back to console.", exc)
    print(f"\n🔔 {title}: {message}\n")


# --------------------------------------------------------------------------- #
# Background alert scheduler
# --------------------------------------------------------------------------- #

class AlertScheduler:
    """
    Runs in a daemon thread and proactively checks the forecast:
      - once a day at a configured time (a full "today rain" summary)
      - every 30 minutes during daytime hours, to catch rain that becomes
        likely later in the day (a short-range nowcast), deduplicated so
        it only notifies once per rain "episode" per day.
    """

    NOWCAST_INTERVAL_MINUTES = 30
    NOWCAST_WINDOW_HOURS = 2  # "rain coming soon" horizon

    def __init__(self, config: AppConfig, weather: WeatherService) -> None:
        self.config = config
        self.weather = weather
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_nowcast_alert_date: Optional[date] = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> str:
        if not HAS_SCHEDULE:
            return ("⚠️  The 'schedule' library isn't installed, so I can't run background "
                    "alerts. Install it with: pip install schedule")
        if not self.config.location:
            return "I need a saved location first. Use: set location <your address>"
        if self.is_running:
            return "Alerts are already running."

        schedule.clear()
        schedule.every().day.at(self.config.daily_alert_time).do(self._daily_check)
        schedule.every(self.NOWCAST_INTERVAL_MINUTES).minutes.do(self._nowcast_check)

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        self.config.alerts_enabled = True
        self.config.save()
        return (f"✅ Background alerts started — daily summary at {self.config.daily_alert_time}, "
                f"plus a rain nowcast every {self.NOWCAST_INTERVAL_MINUTES} min "
                f"({'desktop notifications' if HAS_PLYER else 'console alerts, since plyer is not installed'}).")

    def stop(self) -> str:
        if not self.is_running:
            self.config.alerts_enabled = False
            self.config.save()
            return "Alerts were not running."
        self._stop_event.set()
        self._thread.join(timeout=5)
        schedule.clear()
        self.config.alerts_enabled = False
        self.config.save()
        return "🛑 Background alerts stopped."

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            schedule.run_pending()
            time_module.sleep(1)

    def _daily_check(self) -> None:
        try:
            forecast = self.weather.get_hourly_forecast(self.config.location, forecast_days=2)
            report = format_two_day_report(forecast, self.config.location, self.config.rain_threshold)
            send_notification("Rain Forecast: Today & Tomorrow", report)
        except WeatherServiceError as exc:
            log.warning("Daily rain check failed: %s", exc)

    def _nowcast_check(self) -> None:
        now = datetime.now()
        if not (6 <= now.hour <= 21):
            return  # don't buzz people at night
        today = date.today()
        if self._last_nowcast_alert_date == today:
            return  # already alerted once today; avoid spamming every 30 min
        try:
            forecast = self.weather.get_hourly_forecast(self.config.location)
            windows = find_rain_windows(forecast, self.config.rain_threshold, today)
        except WeatherServiceError as exc:
            log.warning("Nowcast check failed: %s", exc)
            return

        upcoming = [w for w in windows if 0 <= (w.time - now).total_seconds() / 3600 <= self.NOWCAST_WINDOW_HOURS]
        if upcoming:
            first = upcoming[0]
            send_notification(
                "Rain Coming Soon",
                f"{first.probability}% chance of rain around {first.time.strftime('%I:%M %p')} "
                f"near {self.config.location.address}. Carry an umbrella! ☔",
            )
            self._last_nowcast_alert_date = today


# --------------------------------------------------------------------------- #
# Chatbot
# --------------------------------------------------------------------------- #

class RainAlertChatBot:
    def __init__(self) -> None:
        self.config = AppConfig.load()
        self.weather = WeatherService()
        self.scheduler = AlertScheduler(self.config, self.weather)

    # -- command handlers ---------------------------------------------------

    def handle_set_location(self, address: str) -> str:
        if not address:
            return "Please provide an address, e.g. 'set location Koramangala, Bengaluru'."
        try:
            location = self.weather.geocode(address)
        except WeatherServiceError as exc:
            return (f"⚠️  {exc}\n"
                    f"Tip: if this is a small locality the geocoder doesn't know, use "
                    f"'set coordinates <lat> <lon> [label]' instead — get the lat/lon by "
                    f"right-clicking the spot in Google Maps.")
        self.config.location = location
        self.config.save()
        return f"✅ Location saved: {location.address} ({location.latitude:.4f}, {location.longitude:.4f})"

    def handle_set_coordinates(self, args: str) -> str:
        parts = args.split(maxsplit=2)
        if len(parts) < 2:
            return "Usage: set coordinates <lat> <lon> [label]  e.g. set coordinates 12.8817 77.5250 Thurahalli"
        try:
            lat = float(parts[0])
            lon = float(parts[1])
            assert -90 <= lat <= 90 and -180 <= lon <= 180
        except (ValueError, AssertionError):
            return "Latitude must be -90 to 90 and longitude -180 to 180, e.g. set coordinates 12.8817 77.5250"
        label = parts[2] if len(parts) == 3 else f"({lat:.4f}, {lon:.4f})"
        self.config.location = Location(address=label, latitude=lat, longitude=lon)
        self.config.save()
        return f"✅ Location saved: {label} ({lat:.4f}, {lon:.4f})"

    def handle_set_threshold(self, value: str) -> str:
        try:
            threshold = int(value)
            assert 0 <= threshold <= 100
        except (ValueError, AssertionError):
            return "Please give a whole number between 0 and 100, e.g. 'set threshold 60'."
        self.config.rain_threshold = threshold
        self.config.save()
        return f"✅ Rain alert threshold set to {threshold}%."

    def handle_rain(self, scope: str = "both") -> str:
        """scope: 'today', 'tomorrow', 'both', 'week', or 'yesterday'."""
        if not self.config.location:
            return ("I don't have your location yet. Set it first with:\n"
                     "  set location <your address>")

        loc = self.config.location
        threshold = self.config.rain_threshold

        if scope == "yesterday":
            try:
                forecast = self.weather.get_hourly_forecast(loc, forecast_days=1, past_days=1)
            except WeatherServiceError as exc:
                return f"⚠️  {exc}"
            yesterday = date.today() - timedelta(days=1)
            return format_past_day_report(forecast, loc, threshold, yesterday, "yesterday")

        forecast_days = 7 if scope == "week" else 2
        try:
            forecast = self.weather.get_hourly_forecast(loc, forecast_days=forecast_days)
        except WeatherServiceError as exc:
            return f"⚠️  {exc}"

        if scope == "week":
            return format_week_report(forecast, loc, threshold, days=7)
        if scope == "both":
            return format_two_day_report(forecast, loc, threshold)

        on_date = date.today() if scope == "today" else date.today() + timedelta(days=1)
        windows = find_rain_windows(forecast, threshold, on_date)
        return format_rain_report(windows, loc, threshold, scope, is_today=(scope == "today"))

    def handle_today_rain(self) -> str:
        return self.handle_rain("today")

    def handle_tomorrow_rain(self) -> str:
        return self.handle_rain("tomorrow")

    def handle_week_rain(self) -> str:
        return self.handle_rain("week")

    def handle_yesterday_rain(self) -> str:
        return self.handle_rain("yesterday")

    def handle_show_location(self) -> str:
        if not self.config.location:
            return "No location set yet. Use: set location <your address>"
        loc = self.config.location
        return f"📍 {loc.address}  ({loc.latitude:.4f}, {loc.longitude:.4f})  | threshold: {self.config.rain_threshold}%"

    def handle_set_alert_time(self, value: str) -> str:
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", value):
            return "Please use 24-hour HH:MM format, e.g. 'set alert time 07:30'."
        self.config.daily_alert_time = value
        self.config.save()
        restart_note = ""
        if self.scheduler.is_running:
            self.scheduler.stop()
            restart_note = " " + self.scheduler.start()
        return f"✅ Daily alert time set to {value}.{restart_note}"

    def handle_start_alerts(self) -> str:
        return self.scheduler.start()

    def handle_stop_alerts(self) -> str:
        return self.scheduler.stop()

    def handle_alert_status(self) -> str:
        state = "running" if self.scheduler.is_running else "stopped"
        return (f"Alerts are {state}. Daily summary at {self.config.daily_alert_time}, "
                f"threshold {self.config.rain_threshold}%, "
                f"notifications via {'desktop (plyer)' if HAS_PLYER else 'console fallback'}.")

    @staticmethod
    def handle_help() -> str:
        return (
            "Commands I understand:\n"
            "  today rain                 - check if/when rain is expected today\n"
            "  tomorrow rain              - check if/when rain is expected tomorrow\n"
            "  rain forecast              - show both today and tomorrow together\n"
            "  week rain                  - show the next 7 days, one day at a time\n"
            "  yesterday rain             - show what the model estimates fell yesterday\n"
            "  set location <address>     - save your address for forecasting\n"
            "  set coordinates <lat> <lon> [label] - pin an exact spot (bypasses geocoding)\n"
            "  set threshold <0-100>      - set rain-probability alert threshold (default 50)\n"
            "  set alert time HH:MM       - set the daily proactive-alert time (24h, default 07:00)\n"
            "  start alerts                - begin proactive background alerts\n"
            "  stop alerts                 - stop proactive background alerts\n"
            "  alert status                - show whether alerts are running\n"
            "  where am i / my location   - show saved location\n"
            "  help                       - show this message\n"
            "  exit / quit                - leave the chat"
        )

    # -- routing --------------------------------------------------------

    def route(self, message: str) -> str:
        text = message.strip().lower()

        if not text:
            return "Say something! Type 'help' to see what I can do."
        if text in ("exit", "quit", "bye"):
            return "__EXIT__"
        if text in ("help", "?"):
            return self.handle_help()
        if text in ("where am i", "my location", "show location"):
            return self.handle_show_location()
        if re.match(r"^set\s+location\s+", text):
            address = re.sub(r"^set\s+location\s+", "", message.strip(), flags=re.IGNORECASE)
            return self.handle_set_location(address)
        if re.match(r"^set\s+coordinates\s+", text):
            args = re.sub(r"^set\s+coordinates\s+", "", message.strip(), flags=re.IGNORECASE)
            return self.handle_set_coordinates(args)
        if re.match(r"^set\s+threshold\s+", text):
            value = re.sub(r"^set\s+threshold\s+", "", text)
            return self.handle_set_threshold(value)
        if re.match(r"^set\s+alert\s+time\s+", text):
            value = re.sub(r"^set\s+alert\s+time\s+", "", text).strip()
            return self.handle_set_alert_time(value)
        if text == "start alerts":
            return self.handle_start_alerts()
        if text == "stop alerts":
            return self.handle_stop_alerts()
        if text == "alert status":
            return self.handle_alert_status()
        if "rain" in text:
            wants_yesterday = "yesterday" in text
            wants_week = "week" in text or "7 day" in text or "seven day" in text
            wants_today = "today" in text or "now" in text
            wants_tomorrow = "tomorrow" in text or "tmrw" in text
            if wants_yesterday:
                return self.handle_rain("yesterday")
            if wants_week:
                return self.handle_rain("week")
            if wants_today and wants_tomorrow:
                return self.handle_rain("both")
            if wants_tomorrow:
                return self.handle_rain("tomorrow")
            if wants_today:
                return self.handle_rain("today")
            # bare "rain" / "rain forecast" / "will it rain" -> show both days
            return self.handle_rain("both")

        return "I didn't quite get that. Type 'help' to see available commands."

    # -- REPL -------------------------------------------------------------

    def run(self) -> None:
        print("🤖 Rain Alert Chatbot — type 'help' to get started, 'exit' to quit.\n")
        if self.config.location:
            print(f"(Using saved location: {self.config.location.address})\n")
        if self.config.alerts_enabled:
            print(f"bot> {self.scheduler.start()}\n")

        while True:
            try:
                message = input("you> ")
            except (EOFError, KeyboardInterrupt):
                print("\nbot> Goodbye!")
                self.scheduler.stop()
                break
            reply = self.route(message)
            if reply == "__EXIT__":
                print("bot> Goodbye! Stay dry. ☔")
                self.scheduler.stop()
                break
            print(f"bot> {reply}\n")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="Rain Alert Chatbot")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help=("Run headless: no chat prompt, just background alerts. "
              "Requires a location already saved via interactive mode. "
              "Intended for use with Windows Task Scheduler (run with pythonw.exe)."),
    )
    args = parser.parse_args()

    if args.daemon:
        # No console window in daemon mode (e.g. launched via pythonw.exe), so
        # route logs to a file instead of stdout.
        file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        log.handlers.clear()
        log.addHandler(file_handler)
        log.propagate = False

        config = AppConfig.load()
        if not config.location:
            log.error("No saved location. Run the app once without --daemon and use "
                       "'set location <address>' before running in daemon mode.")
            return 1

        weather = WeatherService()
        scheduler = AlertScheduler(config, weather)
        result = scheduler.start()
        log.info(result)
        if not scheduler.is_running:
            return 1

        log.info("Daemon running. Logging to %s. Press Ctrl+C to stop if run interactively.", LOG_PATH)
        try:
            while True:
                time_module.sleep(60)
        except KeyboardInterrupt:
            scheduler.stop()
        return 0

    bot = RainAlertChatBot()
    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
