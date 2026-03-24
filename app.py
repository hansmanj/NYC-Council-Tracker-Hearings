"""
TRMNL Plugin: NYC Council Hearings
Flask webhook — deploy anywhere that runs Python.

TRMNL polls this endpoint on your chosen schedule (recommended: daily at 6am).
Returns JSON that the Liquid template renders on the e-ink display.

Legistar API base: https://webapi.legistar.com/v1/nyc
No API key required for read access.
"""

from flask import Flask, jsonify
from datetime import datetime, timedelta
import requests
import pytz
import logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

LEGISTAR_BASE = "https://webapi.legistar.com/v1/nyc"
NYC_TZ = pytz.timezone("America/New_York")

# Exact Legistar body names — casing matters
WATCHED_COMMITTEES = [
    "Committee on Health",
    "Committee on Mental Health, Disabilities and Addiction",
    "Committee on Hospitals",
]

# "Stated Meeting" body name for the callout bar
STATED_BODY = "Stated Meeting"

# Short display labels (committee name → display string)
COMMITTEE_LABELS = {
    "Committee on Health":                                    "Health",
    "Committee on Mental Health, Disabilities and Addiction": "Mental Health & Addiction",
    "Committee on Hospitals":                                 "Hospitals",
}

# CSS key used for dot styling in the Liquid template
COMMITTEE_KEYS = {
    "Committee on Health":                                    "health",
    "Committee on Mental Health, Disabilities and Addiction": "mental",
    "Committee on Hospitals":                                 "hospitals",
}

# How many days ahead to search for hearings
LOOKAHEAD_DAYS = 60

# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def today_nyc():
    """Return today's date in NYC time."""
    return datetime.now(NYC_TZ).date()


def fmt_api_date(d):
    """Format a date as the OData datetime string Legistar expects."""
    return f"datetime'{d.isoformat()}'"


def fmt_display_date(dt_str):
    """
    Parse an ISO datetime string from Legistar and return a short display date.
    e.g. '2026-03-26T00:00:00' → 'Mar 26'
    """
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.strftime("%b %-d")  # 'Mar 26' — Linux/Mac
    except Exception:
        return dt_str[:10]


def fmt_display_time(time_str):
    """
    Convert Legistar time string to readable format.
    Legistar returns time as e.g. '10:00 AM' or '10:00:00' — normalise both.
    """
    if not time_str:
        return ""
    time_str = time_str.strip()
    # Already formatted (e.g. '10:00 AM')
    if "AM" in time_str.upper() or "PM" in time_str.upper():
        return time_str
    # Parse HH:MM:SS or HH:MM
    try:
        parts = time_str.split(":")
        h, m = int(parts[0]), int(parts[1])
        ampm = "PM" if h >= 12 else "AM"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {ampm}"
    except Exception:
        return time_str


def shorten_location(loc):
    """Abbreviate common NYC Council locations for the compact display."""
    if not loc:
        return ""
    loc = loc.strip()
    lower = loc.lower()
    if "250 broadway" in lower:
        return "250 Bway"
    if "city hall" in lower and "chamber" in lower:
        return "Chambers"
    if "city hall" in lower:
        return "City Hall"
    # Return first 14 chars if nothing matched
    return loc[:14]


def fetch_events(body_names, date_from, date_to):
    """
    Fetch upcoming events from the Legistar API for the given body names
    within the given date range. Returns a list of event dicts.
    """
    # Build OData filter
    date_filter = (
        f"EventDate ge {fmt_api_date(date_from)} "
        f"and EventDate lt {fmt_api_date(date_to)}"
    )
    committee_clauses = " or ".join(
        f"EventBodyName eq '{name}'" for name in body_names
    )
    odata_filter = f"({date_filter}) and ({committee_clauses})"

    params = {
        "$filter": odata_filter,
        "$orderby": "EventDate asc",
        "$top": 30,
        "$select": (
            "EventId,EventBodyName,EventDate,EventTime,"
            "EventLocation,EventAgendaStatusName"
        ),
    }

    resp = requests.get(
        f"{LEGISTAR_BASE}/events",
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_next_stated(date_from, date_to):
    """
    Fetch the next Stated Meeting from Legistar.
    Returns a single event dict or None.
    """
    date_filter = (
        f"EventDate ge {fmt_api_date(date_from)} "
        f"and EventDate lt {fmt_api_date(date_to)}"
    )
    odata_filter = (
        f"({date_filter}) and (EventBodyName eq '{STATED_BODY}')"
    )

    params = {
        "$filter": odata_filter,
        "$orderby": "EventDate asc",
        "$top": 1,
        "$select": "EventBodyName,EventDate,EventTime,EventLocation",
    }

    resp = requests.get(
        f"{LEGISTAR_BASE}/events",
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json()
    return results[0] if results else None


# ─────────────────────────────────────────────────────────────
# ROUTE
# ─────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
@app.route("/hearings", methods=["GET"])
def hearings():
    """
    Main webhook endpoint. TRMNL polls this and passes the JSON
    payload to the Liquid template as `variables`.
    """
    try:
        today = today_nyc()
        future = today + timedelta(days=LOOKAHEAD_DAYS)

        # ── Fetch committee hearings ──────────────────────────
        raw_events = fetch_events(WATCHED_COMMITTEES, today, future)

        hearings_list = []
        for e in raw_events:
            event_date_str = e.get("EventDate", "")
            event_date = datetime.fromisoformat(
                event_date_str.replace("Z", "+00:00")
            ).date()

            body_name = e.get("EventBodyName", "")

            hearings_list.append({
                "date":             fmt_display_date(event_date_str),
                "is_today":         event_date == today,
                "is_soon":          0 < (event_date - today).days <= 5,
                "committee_full":   body_name,
                "committee_label":  COMMITTEE_LABELS.get(body_name, body_name),
                "committee_key":    COMMITTEE_KEYS.get(body_name, "other"),
                "time":             fmt_display_time(e.get("EventTime", "")),
                "location":         shorten_location(e.get("EventLocation", "")),
                "agenda_published": e.get("EventAgendaStatusName", "") == "Final",
            })

        # ── Fetch next Stated Meeting ─────────────────────────
        stated_raw = fetch_next_stated(today, future)
        stated = None
        if stated_raw:
            stated = {
                "date":     fmt_display_date(stated_raw.get("EventDate", "")),
                "time":     fmt_display_time(stated_raw.get("EventTime", "")),
                "location": shorten_location(stated_raw.get("EventLocation", "")),
            }

        # ── Timestamp for header ──────────────────────────────
        now_nyc = datetime.now(NYC_TZ)
        updated = now_nyc.strftime("%a %-d %b · %-I:%M %p").upper()

        payload = {
            "hearings":      hearings_list,
            "count":         len(hearings_list),
            "stated":        stated,
            "updated":       updated,
        }

        return jsonify(payload)

    except requests.exceptions.RequestException as exc:
        logging.error("Legistar API error: %s", exc)
        return jsonify({
            "hearings": [],
            "count":    0,
            "stated":   None,
            "updated":  "—",
            "error":    str(exc),
        }), 502

    except Exception as exc:
        logging.error("Unexpected error: %s", exc)
        return jsonify({
            "hearings": [],
            "count":    0,
            "stated":   None,
            "updated":  "—",
            "error":    str(exc),
        }), 500


# ─────────────────────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
