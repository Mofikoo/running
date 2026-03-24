#!/usr/bin/env python3
"""
RunCoach — Strava → Supabase sync
Importe activités + streams (FC/allure/cadence par seconde)
"""
import os, json, time, logging, requests
from datetime import datetime, timedelta, date

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

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

def get_access_token():
    r = requests.post('https://www.strava.com/oauth/token', data={
        'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET,
        'refresh_token': REFRESH_TOKEN, 'grant_type': 'refresh_token',
    })
    data = r.json()
    if 'access_token' not in data:
        raise Exception(f"Auth Strava échouée: {data}")
    new_refresh = data.get('refresh_token', REFRESH_TOKEN)
    if new_refresh != REFRESH_TOKEN:
        update_github_secret('STRAVA_REFRESH_TOKEN', new_refresh)
    log.info("Token Strava OK ✓")
    return data['access_token']

def update_github_secret(name, value):
    if not GITHUB_TOKEN or not GITHUB_REPO: return
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

def map_type(name, avg_hr):
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

def get_streams(token, activity_id):
    """Récupère les streams par seconde depuis Strava."""
    keys = 'time,heartrate,velocity_smooth,cadence,altitude,distance'
    r = requests.get(
        f'https://www.strava.com/api/v3/activities/{activity_id}/streams',
        headers={"Authorization": f"Bearer {token}"},
        params={'keys': keys, 'key_by_type': 'true'}
    )
    if r.status_code != 200:
        log.warning(f"Streams non disponibles pour {activity_id}: {r.status_code}")
        return None
    data = r.json()
    if not data:
        return None

    # Extraire les arrays
    time_arr = data.get('time', {}).get('data', [])
    hr_arr   = data.get('heartrate', {}).get('data', [])
    vel_arr  = data.get('velocity_smooth', {}).get('data', [])
    cad_arr  = data.get('cadence', {}).get('data', [])
    alt_arr  = data.get('altitude', {}).get('data', [])
    dist_arr = data.get('distance', {}).get('data', [])

    if not time_arr:
        return None

    # Sous-échantillonner à 1 point toutes les 5 secondes pour réduire la taille
    step = 5
    n = len(time_arr)
    indices = list(range(0, n, step))

    def safe_get(arr, i):
        return arr[i] if arr and i < len(arr) else None

    # Convertir vitesse m/s → allure sec/km
    def vel_to_pace(v):
        if v and v > 0:
            return round(1000 / v)
        return None

    streams_data = {
        'time':     [time_arr[i] for i in indices],
        'hr':       [safe_get(hr_arr, i) for i in indices],
        'pace':     [vel_to_pace(safe_get(vel_arr, i)) for i in indices],
        'cadence':  [safe_get(cad_arr, i) for i in indices],
        'altitude': [round(safe_get(alt_arr, i), 1) if safe_get(alt_arr, i) else None for i in indices],
        'distance': [round(safe_get(dist_arr, i), 0) if safe_get(dist_arr, i) else None for i in indices],
    }

    # Calcul temps par zone FC
    zone_minutes = compute_zone_times(hr_arr)

    return {
        'streams': streams_data,
        'zone_minutes': zone_minutes,
        'total_points': len(indices),
        'duration_sec': time_arr[-1] if time_arr else None,
    }

def compute_zone_times(hr_arr):
    """Calcule le temps réel passé dans chaque zone FC."""
    if not hr_arr:
        return None
    zones = {'Z1':0, 'Z2':0, 'Z3':0, 'Z4':0, 'Z5':0}
    bounds = [
        ('Z1', FC_MAX*0.60, FC_MAX*0.70),
        ('Z2', FC_MAX*0.70, FC_MAX*0.80),
        ('Z3', FC_MAX*0.80, FC_MAX*0.88),
        ('Z4', FC_MAX*0.88, FC_MAX*0.93),
        ('Z5', FC_MAX*0.93, FC_MAX*1.01),
    ]
    for hr in hr_arr:
        if hr is None: continue
        for name, low, high in bounds:
            if low <= hr < high:
                zones[name] += 1  # 1 seconde par point
                break
    # Convertir en minutes
    return {k: round(v/60, 1) for k, v in zones.items()}

def get_existing_ids():
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
        log.error(f"Upsert error: {r.status_code} {r.text[:200]}")
    else:
        log.info(f"✓ {session['date']} {session['type']} {session.get('distance_km','?')}km")

def sync(days_back=7):
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    existing = get_existing_ids()
    after = int((datetime.now() - timedelta(days=days_back)).timestamp())
    page, total = 1, 0

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
            if not aid:
                continue

            name     = act.get('name', '')
            date_str = act.get('start_date_local', '')[:10]
            dist_km  = round((act.get('distance') or 0) / 1000, 2) or None
            dur_min  = round((act.get('moving_time') or 0) / 60) or None
            avg_hr   = act.get('average_heartrate')
            elev     = act.get('total_elevation_gain')
            avg_spd  = act.get('average_speed')
            pace_sec = round(1000 / avg_spd) if avg_spd and avg_spd > 0 else None
            cadence  = act.get('average_cadence')
            stype    = map_type(name, avg_hr)

            effort = 3
            if avg_hr:
                fc = float(avg_hr)
                if fc >= FC_MAX*0.93:   effort = 5
                elif fc >= FC_MAX*0.88: effort = 4
                elif fc >= FC_MAX*0.80: effort = 3
                else:                   effort = 2

            # Streams (FC/allure par seconde) — même pour les réimports
            streams_data = None
            time.sleep(0.5)  # rate limiting Strava
            try:
                streams_data = get_streams(token, aid)
                if streams_data:
                    log.info(f"  → {streams_data['total_points']} points, zones: {streams_data['zone_minutes']}")
            except Exception as e:
                log.warning(f"  Streams erreur: {e}")

            session = {
                "date":               date_str,
                "type":               stype,
                "distance_km":        dist_km,
                "duration_minutes":   dur_min,
                "avg_pace_seconds":   pace_sec,
                "avg_hr":             int(avg_hr) if avg_hr else None,
                "perceived_effort":   effort,
                "pain_level":         0,
                "notes":              f"{name} · Strava",
                "completed":          True,
                "garmin_activity_id": aid,
                "elevation_gain":     round(elev, 1) if elev else None,
                "cadence_avg":        int(cadence * 2) if cadence else None,
                "streams":            streams_data,
            }

            upsert_session(session)
            existing.add(aid)
            total += 1
            time.sleep(0.3)

        if len(activities) < 50:
            break
        page += 1
        time.sleep(1)

    log.info(f"Sync terminé — {total} activités ✓")

if __name__ == '__main__':
    days = int(os.environ.get('DAYS_BACK', '7'))
    sync(days_back=days)
