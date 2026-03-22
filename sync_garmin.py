#!/usr/bin/env python3
"""
RunCoach — Garmin Connect → Supabase sync
Tourne automatiquement via GitHub Actions 2x/jour
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta, date

import requests
from garminconnect import Garmin

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ── Config depuis variables d'environnement GitHub Secrets ──────────────────
GARMIN_EMAIL    = os.environ["GARMIN_EMAIL"]
GARMIN_PASSWORD = os.environ["GARMIN_PASSWORD"]
SUPABASE_URL    = os.environ["SUPABASE_URL"]      # https://xxx.supabase.co
SUPABASE_KEY    = os.environ["SUPABASE_KEY"]      # anon key

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",
}

# ── Types de séances ─────────────────────────────────────────────────────────
ACTIVITY_TYPE_MAP = {
    "running":           "EF",
    "track_running":     "VMA",
    "trail_running":     "Long",
    "treadmill_running": "EF",
    "cycling":           "Récup",
    "walking":           "Récup",
}

def map_activity_type(garmin_type: str, training_effect: float) -> str:
    """Devine le type de séance depuis le type Garmin + Training Effect."""
    base = ACTIVITY_TYPE_MAP.get(garmin_type, "EF")
    if base == "EF" and training_effect:
        if training_effect >= 4.0:
            return "VMA"
        elif training_effect >= 3.0:
            return "Seuil"
    return base

def pace_to_seconds(pace_min_per_km: float) -> int:
    """Convertit min/km (float) en secondes/km."""
    if not pace_min_per_km or pace_min_per_km <= 0:
        return None
    minutes = int(pace_min_per_km)
    seconds = round((pace_min_per_km - minutes) * 60)
    return minutes * 60 + seconds

def extract_intervals(laps: list) -> list:
    """Extrait les intervalles depuis les laps Garmin."""
    if not laps:
        return []
    intervals = []
    for i, lap in enumerate(laps):
        duration_sec = lap.get("duration", 0)
        distance_m   = lap.get("distance", 0)
        avg_hr       = lap.get("averageHR")
        avg_speed    = lap.get("averageSpeed")  # m/s

        pace_sec = None
        if avg_speed and avg_speed > 0:
            pace_sec = round(1000 / avg_speed)  # secondes/km

        intervals.append({
            "num":          i + 1,
            "duration":     f"{int(duration_sec//60)}min{int(duration_sec%60):02d}s" if duration_sec else None,
            "distance_m":   round(distance_m) if distance_m else None,
            "pace_seconds": pace_sec,
            "pace":         f"{pace_sec//60}'{pace_sec%60:02d}\"" if pace_sec else None,
            "avg_hr":       avg_hr,
            "feel":         "ok",
        })
    return intervals

def get_existing_garmin_ids() -> set:
    """Récupère les garmin_activity_id déjà en base pour éviter les doublons."""
    url = f"{SUPABASE_URL}/rest/v1/sessions?select=garmin_activity_id&garmin_activity_id=not.is.null"
    r = requests.get(url, headers=HEADERS)
    if r.status_code != 200:
        log.warning(f"Impossible de récupérer les IDs existants: {r.text}")
        return set()
    return {row["garmin_activity_id"] for row in r.json() if row.get("garmin_activity_id")}

def upsert_session(session: dict):
    """Insère ou met à jour une session dans Supabase."""
    url = f"{SUPABASE_URL}/rest/v1/sessions"
    headers = {**HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"}
    r = requests.post(url, headers=headers, json=session)
    if r.status_code not in (200, 201):
        log.error(f"Erreur upsert: {r.status_code} {r.text}")
    else:
        log.info(f"✓ Session {session['date']} ({session['type']}) insérée/mise à jour")

def sync_activities(days_back: int = 7):
    """Sync principal : récupère les N derniers jours depuis Garmin."""
    log.info(f"Connexion à Garmin Connect ({GARMIN_EMAIL})...")
    client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    client.login()
    log.info("Connecté ✓")

    existing_ids = get_existing_garmin_ids()
    log.info(f"{len(existing_ids)} activités déjà en base")

    start_date = date.today() - timedelta(days=days_back)
    end_date   = date.today()

    log.info(f"Récupération des activités du {start_date} au {end_date}...")
    activities = client.get_activities_by_date(
        start_date.strftime("%Y-%m-%d"),
        end_date.strftime("%Y-%m-%d"),
        "running"
    )
    log.info(f"{len(activities)} activité(s) trouvée(s)")

    for act in activities:
        activity_id = act.get("activityId")
        if not activity_id:
            continue

        # Skip si déjà importé
        if activity_id in existing_ids:
            log.info(f"Skip {activity_id} (déjà importé)")
            continue

        activity_type_raw = act.get("activityType", {}).get("typeKey", "running")
        start_time        = act.get("startTimeLocal", "")
        activity_date     = start_time[:10] if start_time else str(date.today())

        distance_m   = act.get("distance", 0)
        distance_km  = round(distance_m / 1000, 2) if distance_m else None
        duration_sec = act.get("duration", 0)
        duration_min = round(duration_sec / 60) if duration_sec else None
        avg_hr       = act.get("averageHR")
        max_hr       = act.get("maxHR")
        avg_speed    = act.get("averageSpeed")  # m/s
        training_effect = act.get("aerobicTrainingEffect", 0) or 0
        vo2max       = act.get("vO2MaxValue")
        cadence      = act.get("averageRunningCadenceInStepsPerMinute")
        elevation    = act.get("elevationGain")

        # Allure moyenne en secondes/km
        avg_pace_sec = None
        if avg_speed and avg_speed > 0:
            avg_pace_sec = round(1000 / avg_speed)

        # Type de séance intelligent
        session_type = map_activity_type(activity_type_raw, training_effect)

        # Récupérer les laps (intervalles) pour les séances intensives
        laps = []
        intervals = []
        if session_type in ("VMA", "Seuil") or training_effect >= 3.0:
            try:
                time.sleep(0.5)  # rate limiting
                details = client.get_activity_splits(activity_id)
                laps = details.get("lapDTOs", []) if details else []
                intervals = extract_intervals(laps)
                log.info(f"  → {len(intervals)} intervalles récupérés")
            except Exception as e:
                log.warning(f"  Impossible de récupérer les laps: {e}")

        # Description automatique
        if intervals:
            desc = f"{len(intervals)} intervalles — auto-importé depuis Garmin"
        else:
            desc = f"Importé depuis Garmin · TE:{training_effect:.1f}"

        session = {
            "date":                 activity_date,
            "type":                 session_type,
            "distance_km":          distance_km,
            "duration_minutes":     duration_min,
            "avg_pace_seconds":     avg_pace_sec,
            "avg_hr":               avg_hr,
            "perceived_effort":     min(5, max(1, round(training_effect))) if training_effect else 3,
            "pain_level":           0,
            "notes":                desc,
            "completed":            True,
            "garmin_activity_id":   activity_id,
            "training_effect":      training_effect if training_effect else None,
            "vo2max":               vo2max,
            "cadence_avg":          int(cadence) if cadence else None,
            "elevation_gain":       round(elevation, 1) if elevation else None,
            "intervals":            intervals if intervals else None,
        }

        upsert_session(session)
        time.sleep(0.3)  # rate limiting Garmin

    log.info("Sync terminé ✓")

if __name__ == "__main__":
    # Par défaut sync les 7 derniers jours
    # Pour le premier run, on sync 30 jours
    days = int(os.environ.get("DAYS_BACK", "7"))
    sync_activities(days_back=days)
