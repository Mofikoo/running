#!/usr/bin/env python3
"""
RunCoach — Garmin Connect → Supabase sync
Gère les tokens OAuth pour éviter le 429 de Garmin
"""

import os
import json
import time
import base64
import logging
import requests
from datetime import datetime, timedelta, date
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

GARMIN_EMAIL      = os.environ["GARMIN_EMAIL"]
GARMIN_PASSWORD   = os.environ["GARMIN_PASSWORD"]
SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_KEY      = os.environ["SUPABASE_KEY"]
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO       = os.environ.get("GITHUB_REPOSITORY", "")
GARMIN_TOKENS_B64 = os.environ.get("GARMIN_TOKENS", "")

TOKEN_DIR = Path("/tmp/garmin_tokens")

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",
}

def map_session_type(garmin_type, te):
    m = {"running":"EF","track_running":"VMA","trail_running":"Long","treadmill_running":"EF"}
    base = m.get(garmin_type, "EF")
    if base == "EF" and te:
        if te >= 4.0: return "VMA"
        if te >= 3.0: return "Seuil"
    return base

def extract_intervals(laps):
    out = []
    for i, lap in enumerate(laps or []):
        dur = lap.get("duration", 0)
        spd = lap.get("averageSpeed")
        p   = round(1000 / spd) if spd and spd > 0 else None
        out.append({
            "num": i+1,
            "duration": f"{int(dur//60)}'{int(dur%60):02d}\"" if dur else None,
            "distance_m": round(lap.get("distance",0)),
            "pace_seconds": p,
            "pace": f"{p//60}'{p%60:02d}\"" if p else None,
            "avg_hr": lap.get("averageHR"),
            "feel": "ok",
        })
    return out

def load_tokens():
    if not GARMIN_TOKENS_B64: return False
    try:
        TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        tokens = json.loads(base64.b64decode(GARMIN_TOKENS_B64).decode())
        for fname, content in tokens.items():
            (TOKEN_DIR / fname).write_text(content)
        log.info(f"Tokens chargés ({len(tokens)} fichiers)")
        return True
    except Exception as e:
        log.warning(f"Chargement tokens échoué: {e}")
        return False

def save_tokens():
    if not GITHUB_TOKEN or not GITHUB_REPO:
        log.warning("Pas de GITHUB_TOKEN → tokens non persistés")
        return
    try:
        from nacl import encoding, public
        files = {f.name: f.read_text() for f in TOKEN_DIR.iterdir() if f.is_file()}
        if not files: return
        encoded = base64.b64encode(json.dumps(files).encode()).decode()
        pk_r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/actions/secrets/public-key",
            headers={"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
        )
        pk = pk_r.json()
        box = public.SealedBox(public.PublicKey(pk["key"].encode(), encoding.Base64Encoder()))
        encrypted = base64.b64encode(box.encrypt(encoded.encode())).decode()
        r = requests.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/actions/secrets/GARMIN_TOKENS",
            headers={"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
            json={"encrypted_value": encrypted, "key_id": pk["key_id"]}
        )
        if r.status_code in (201, 204):
            log.info("✓ Tokens sauvegardés dans GitHub Secrets")
        else:
            log.warning(f"Sauvegarde échouée: {r.status_code}")
    except ImportError:
        log.warning("PyNaCl manquant, tokens non sauvegardés")
    except Exception as e:
        log.warning(f"Erreur save_tokens: {e}")

def get_existing_ids():
    r = requests.get(f"{SUPABASE_URL}/rest/v1/sessions?select=garmin_activity_id&garmin_activity_id=not.is.null", headers=SUPABASE_HEADERS)
    return {row["garmin_activity_id"] for row in r.json()} if r.status_code == 200 else set()

def upsert(session):
    h = {**SUPABASE_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"}
    r = requests.post(f"{SUPABASE_URL}/rest/v1/sessions", headers=h, json=session)
    if r.status_code not in (200,201): log.error(f"Upsert error: {r.status_code} {r.text}")
    else: log.info(f"✓ {session['date']} {session['type']} {session.get('distance_km','?')}km")

def sync_activities(days_back=7):
    from garminconnect import Garmin
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    has_tokens = load_tokens()
    client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)

    if has_tokens:
        try:
            client.login(tokenstore=str(TOKEN_DIR))
            log.info("Connecté via token ✓")
        except Exception as e:
            log.warning(f"Token invalide: {e} → re-login")
            has_tokens = False

    if not has_tokens:
        log.info("Login email/password...")
        client.login()
        log.info("Connecté ✓")

    try:
        client.garth.dump(str(TOKEN_DIR))
        save_tokens()
    except Exception as e:
        log.warning(f"Dump token échoué: {e}")

    existing = get_existing_ids()
    start = (date.today() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    end   = date.today().strftime("%Y-%m-%d")
    log.info(f"Récupération {start} → {end}...")
    activities = client.get_activities_by_date(start, end, "running")
    log.info(f"{len(activities)} activité(s)")

    for act in activities:
        aid = act.get("activityId")
        if not aid or aid in existing:
            continue
        atype    = act.get("activityType",{}).get("typeKey","running")
        date_str = (act.get("startTimeLocal") or str(date.today()))[:10]
        dist_km  = round((act.get("distance") or 0)/1000, 2) or None
        dur_min  = round((act.get("duration") or 0)/60) or None
        spd      = act.get("averageSpeed")
        pace_sec = round(1000/spd) if spd and spd > 0 else None
        te       = act.get("aerobicTrainingEffect") or 0
        stype    = map_session_type(atype, te)

        intervals = []
        if stype in ("VMA","Seuil") or te >= 3.0:
            try:
                time.sleep(1)
                details = client.get_activity_splits(aid) or {}
                intervals = extract_intervals(details.get("lapDTOs",[]))
                log.info(f"  {len(intervals)} intervalles")
            except Exception as e:
                log.warning(f"  Laps non récupérés: {e}")

        upsert({
            "date": date_str, "type": stype,
            "distance_km": dist_km, "duration_minutes": dur_min,
            "avg_pace_seconds": pace_sec, "avg_hr": act.get("averageHR"),
            "perceived_effort": min(5, max(1, round(te))) if te else 3,
            "pain_level": 0,
            "notes": f"Garmin import · TE:{te:.1f}",
            "completed": True, "garmin_activity_id": aid,
            "training_effect": te or None,
            "vo2max": act.get("vO2MaxValue"),
            "cadence_avg": int(act["averageRunningCadenceInStepsPerMinute"]) if act.get("averageRunningCadenceInStepsPerMinute") else None,
            "elevation_gain": round(act["elevationGain"],1) if act.get("elevationGain") else None,
            "intervals": intervals or None,
        })
        time.sleep(0.5)

    log.info("Sync terminé ✓")

if __name__ == "__main__":
    sync_activities(days_back=int(os.environ.get("DAYS_BACK","7")))
