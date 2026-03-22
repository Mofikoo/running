#!/usr/bin/env python3
"""
RunCoach — Strava → Supabase sync
Tourne automatiquement via GitHub Actions 2x/jour
"""
import os, json, time, logging, requests
from datetime import datetime, timedelta, date

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
CLIENT_ID     = os.environ['STRAVA_CLIENT_ID']
CLIENT_SECRET = os.environ['STRAVA_CLIENT_SECRET']
REFRESH_TOKEN = os.environ['STRAVA_REFRESH_TOKEN']
SUPABASE_URL  = os.environ['SUPABASE_URL']
SUPABASE_KEY  = os.environ['SUPABASE_KEY']
GITHUB_TOKEN  = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO   = os.environ.get('GITHUB_REPOSITORY', '')

FC_MAX = 208
SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=minimal",
}

# ── Strava OAuth ───────────────────────────────────────────────────────────────
def get_access_token():
    """Échange le refresh token contre un access token frais."""
    r = requests.post('https://www.strava.com/oauth/token', data={
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'refresh_token': REFRESH_TOKEN,
        'grant_type': 'refresh_token',
    })
    data = r.json()
    if 'access_token' not in data:
        raise Exception(f"Auth Strava échouée: {data}")
    new_refresh = data.get('refresh_token', REFRESH_TOKEN)
    # Sauvegarde le nouveau refresh token si changé
    if new_refresh != REFRESH_TOKEN:
        update_github_secret('STRAVA_REFRESH_TOKEN', new_refresh)
    log.info("Access token Strava obtenu ✓")
    return data['access_token']

def update_github_secret(name, value):
    """Met à jour un secret GitHub."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return
    try:
        from nacl import encoding, public
        import base64
        pk_r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/actions/secrets/public-key",
            headers={"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
        )
        pk = pk_r.json()
        box = public.SealedBox(public.PublicKey(pk["key"].encode(), encoding.Base64Encoder()))
        encrypted = base64.b64encode(box.encrypt(value.encode())).decode()
        requests.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/actions/secrets/{name}",
            headers={"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
            json={"encrypted_value": encrypted, "key_id": pk["key_id"]}
        )
        log.info(f"Secret {name} mis à jour ✓")
    except Exception as e:
        log.warning(f"Mise à jour secret échouée: {e}")

# ── Type détection ─────────────────────────────────────────────────────────────
def map_type(name, avg_hr, suffer_score):
    n = (name or '').lower()
    if any(x in n for x in ['vma','6x6','5x6','4x6','x6\'','3x2','4x2','x2km','x1km','fractionné','interval','répétition']):
        return 'VMA'
    if any(x in n for x in ['seuil','tempo','4x8','3x8','x8\'','x10\'','x15\'']):
        return 'Seuil'
    if any(x in n for x in ['longue','long run','sortie longue']):
        return 'Long'
    if any(x in n for x in ['récup','recup','recovery']):
        return 'Récup'
    if avg_hr:
        fc = float(avg_hr)
        if fc >= FC_MAX * 0.93: return 'VMA'
        if fc >= FC_MAX * 0.88: return 'Seuil'
        if fc >= FC_MAX * 0.80: return 'Aérobie Z3'
        return 'EF'
    return 'EF'

# ── Supabase ───────────────────────────────────────────────────────────────────
def get_existing_strava_ids():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/sessions?select=garmin_activity_id&garmin_activity_id=not.is.null",
        headers=SUPABASE_HEADERS
    )
    return {row['garmin_activity_id'] for row in r.json()} if r.status_code == 200 else set()

def upsert_session(session):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/sessions",
        headers={**SUPABASE_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=session
    )
    if r.status_code not in (200, 201):
        log.error(f"Upsert error: {r.status_code} {r.text}")
    else:
        log.info(f"✓ {session['date']} {session['type']} {session.get('distance_km','?')}km")

# ── Sync ───────────────────────────────────────────────────────────────────────
def sync(days_back=7):
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    existing = get_existing_strava_ids()

    after = int((datetime.now() - timedelta(days=days_back)).timestamp())
    page = 1
    total = 0

    while True:
        r = requests.get(
            'https://www.strava.com/api/v3/athlete/activities',
            headers=headers,
            params={'after': after, 'per_page': 50, 'page': page}
        )
        activities = r.json()
        if not activities or not isinstance(activities, list):
            break

        running = [a for a in activities if a.get('type') == 'Run' or a.get('sport_type') == 'Run']
        log.info(f"Page {page}: {len(running)} courses")

        for act in running:
            aid = act.get('id')
            if not aid or aid in existing:
                log.info(f"Skip {aid}")
                continue

            name       = act.get('name', '')
            date_str   = act.get('start_date_local', '')[:10]
            dist_km    = round((act.get('distance') or 0) / 1000, 2) or None
            dur_min    = round((act.get('moving_time') or 0) / 60) or None
            avg_hr     = act.get('average_heartrate')
            max_hr     = act.get('max_heartrate')
            suffer     = act.get('suffer_score')
            elev       = act.get('total_elevation_gain')
            avg_spd    = act.get('average_speed')  # m/s
            pace_sec   = round(1000 / avg_spd) if avg_spd and avg_spd > 0 else None
            cadence    = act.get('average_cadence')

            stype = map_type(name, avg_hr, suffer)

            effort = 3
            if avg_hr:
                fc = float(avg_hr)
                if fc >= FC_MAX * 0.93:   effort = 5
                elif fc >= FC_MAX * 0.88: effort = 4
                elif fc >= FC_MAX * 0.80: effort = 3
                else:                     effort = 2

            session = {
                "date":               date_str,
                "type":               stype,
                "distance_km":        dist_km,
                "duration_minutes":   dur_min,
                "avg_pace_seconds":   pace_sec,
                "avg_hr":             int(avg_hr) if avg_hr else None,
                "perceived_effort":   effort,
                "pain_level":         0,
                "notes":              f"{name} · Strava import",
                "completed":          True,
                "garmin_activity_id": aid,  # on réutilise ce champ pour l'ID Strava
                "elevation_gain":     round(elev, 1) if elev else None,
                "cadence_avg":        int(cadence * 2) if cadence else None,  # Strava = cadence/jambe × 2
            }

            upsert_session(session)
            existing.add(aid)
            total += 1
            time.sleep(0.3)

        if len(activities) < 50:
            break
        page += 1
        time.sleep(1)

    log.info(f"Sync terminé — {total} nouvelles activités importées ✓")

if __name__ == '__main__':
    days = int(os.environ.get('DAYS_BACK', '7'))
    sync(days_back=days)
