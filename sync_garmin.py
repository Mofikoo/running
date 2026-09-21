#!/usr/bin/env python3
"""
RunCoach — Garmin Connect → Supabase sync
Importe activités + streams (FC/allure/cadence par seconde)

Auth: garminconnect (API non-officielle, connexion via email/mot de passe).
Les tokens de session sont mis en cache dans le secret GitHub GARMIN_TOKENS
pour éviter de se reconnecter (et de retomber sur la 2FA) à chaque run.
"""
import os, time, logging
from datetime import datetime, timedelta

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

GARMIN_EMAIL    = os.environ['GARMIN_EMAIL']
GARMIN_PASSWORD = os.environ['GARMIN_PASSWORD']
GARMIN_TOKENS   = os.environ.get('GARMIN_TOKENS', '')
SUPABASE_URL    = os.environ['SUPABASE_URL']
SUPABASE_KEY    = os.environ['SUPABASE_KEY']
GITHUB_TOKEN    = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO     = os.environ.get('GITHUB_REPOSITORY', '')

FC_MAX  = 208
FC_REPO = 55   # FC repos pour calcul Karvonen
SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=minimal",
}


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


def login():
    client = Garmin(email=GARMIN_EMAIL, password=GARMIN_PASSWORD)
    try:
        client.login(tokenstore=GARMIN_TOKENS or None)
    except (GarminConnectAuthenticationError, GarminConnectConnectionError, GarminConnectTooManyRequestsError) as e:
        raise Exception(f"Auth Garmin échouée: {e}")

    # Persiste les tokens (rafraîchis) pour éviter une reconnexion complète au prochain run
    try:
        new_tokens = client.client.dumps()
        if new_tokens and new_tokens != GARMIN_TOKENS:
            update_github_secret('GARMIN_TOKENS', new_tokens)
    except Exception as e:
        log.warning(f"Impossible de sauvegarder les tokens Garmin: {e}")

    log.info("Connexion Garmin OK ✓")
    return client


def map_type(name, avg_hr):
    n = (name or '').lower()
    if any(x in n for x in ['vma','6x6','5x6','4x6','x6\'','3x2','4x2','x2km','x1km','fractionné','interval','répétition']):
        return 'VMA'
    if any(x in n for x in ['seuil','tempo','4x8','3x8','x8\'','x10\'','x15\'']):
        return 'Seuil'
    if any(x in n for x in ['longue','long run','sortie longue','sl ','sl-',' sl',' sl ']) or n.startswith('sl') or n.endswith(' sl'):
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


def compute_zone_times(hr_arr):
    """Calcule le temps réel passé dans chaque zone FC."""
    if not hr_arr:
        return None
    zones = {'Z1':0, 'Z2':0, 'Z3':0, 'Z4':0, 'Z5':0}
    res = FC_MAX - FC_REPO
    bounds = [
        ('Z1', FC_REPO + res*0.50, FC_REPO + res*0.60),
        ('Z2', FC_REPO + res*0.60, FC_REPO + res*0.70),
        ('Z3', FC_REPO + res*0.70, FC_REPO + res*0.80),
        ('Z4', FC_REPO + res*0.80, FC_REPO + res*0.90),
        ('Z5', FC_REPO + res*0.90, FC_MAX*1.01),
    ]
    for hr in hr_arr:
        if hr is None: continue
        for name, low, high in bounds:
            if low <= hr < high:
                zones[name] += 1  # 1 point ≈ 1 seconde (sous-échantillonné ensuite)
                break
    return {k: round(v/60, 1) for k, v in zones.items()}


def get_streams(client, activity_id):
    """Récupère les métriques par point depuis Garmin Connect (activity details)."""
    try:
        details = client.get_activity_details(activity_id)
    except Exception as e:
        log.warning(f"Détails/streams non disponibles pour {activity_id}: {e}")
        return None

    descriptors = details.get('metricDescriptors', [])
    metrics = details.get('activityDetailMetrics', [])
    if not descriptors or not metrics:
        return None

    # metricsIndex -> clé (ex: 'directHeartRate', 'directSpeed', ...)
    idx_by_key = {d.get('key'): d.get('metricsIndex') for d in descriptors}

    def col(key):
        i = idx_by_key.get(key)
        if i is None:
            return []
        return [m['metrics'][i] if m.get('metrics') and i < len(m['metrics']) else None for m in metrics]

    time_arr = col('sumDuration') or col('directTimestamp')
    hr_arr   = col('directHeartRate')
    vel_arr  = col('directSpeed')            # m/s
    cad_arr  = col('directRunCadence') or col('directDoubleCadence')
    alt_arr  = col('directElevation')
    dist_arr = col('sumDistance')
    watt_arr = col('directPower')

    if not time_arr:
        return None

    n = len(time_arr)
    step = max(1, round(n / max(1, (time_arr[-1] or n) / 5)))  # ~1 point / 5s
    indices = list(range(0, n, step))

    def safe_get(arr, i):
        return arr[i] if arr and i < len(arr) else None

    def vel_to_pace(v):
        if v and v > 0:
            return round(1000 / v)
        return None

    streams_data = {
        'time':     [round(safe_get(time_arr, i) or 0) for i in indices],
        'hr':       [round(safe_get(hr_arr, i)) if safe_get(hr_arr, i) is not None else None for i in indices],
        'pace':     [vel_to_pace(safe_get(vel_arr, i)) for i in indices],
        'cadence':  [round(safe_get(cad_arr, i)) if safe_get(cad_arr, i) is not None else None for i in indices],
        'altitude': [round(safe_get(alt_arr, i), 1) if safe_get(alt_arr, i) is not None else None for i in indices],
        'distance': [round(safe_get(dist_arr, i), 0) if safe_get(dist_arr, i) is not None else None for i in indices],
        'power':    [int(safe_get(watt_arr, i)) if safe_get(watt_arr, i) else None for i in indices],
    }

    zone_minutes = compute_zone_times(hr_arr)

    return {
        'streams': streams_data,
        'zone_minutes': zone_minutes,
        'total_points': len(indices),
        'duration_sec': round(time_arr[-1]) if time_arr and time_arr[-1] else None,
    }


def parse_splits(client, activity_id):
    """Splits km par km fournis par Garmin."""
    try:
        data = client.get_activity_splits(activity_id)
    except Exception as e:
        log.warning(f"Splits non disponibles pour {activity_id}: {e}")
        return None
    laps = data.get('lapDTOs', []) if data else []
    if not laps:
        return None
    result = []
    for i, s in enumerate(laps, start=1):
        spd = s.get('averageSpeed')
        pace_sec = round(1000 / spd) if spd and spd > 0 else None
        result.append({
            'km':           i,
            'distance_m':   round(s.get('distance', 0)),
            'duration_sec': round(s.get('duration', 0)),
            'pace_sec':     pace_sec,
            'avg_hr':       int(s['averageHR']) if s.get('averageHR') else None,
            'elev_diff':    round((s.get('elevationGain') or 0) - (s.get('elevationLoss') or 0), 1),
        })
    return result


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
    client = login()
    existing = get_existing_ids()
    cutoff = datetime.now() - timedelta(days=days_back)

    start, limit, total = 0, 50, 0
    while True:
        activities = client.get_activities(start=start, limit=limit)
        if not activities:
            break

        running = [
            a for a in activities
            if (a.get('activityType') or {}).get('typeKey') in ('running', 'trail_running', 'track_running', 'treadmill_running')
        ]
        log.info(f"Page start={start}: {len(running)} courses sur {len(activities)} activités")

        stop = False
        for act in running:
            aid = act.get('activityId')
            if not aid:
                continue

            start_local = act.get('startTimeLocal', '')  # "YYYY-MM-DD HH:MM:SS"
            try:
                act_dt = datetime.strptime(start_local[:19], '%Y-%m-%d %H:%M:%S')
            except ValueError:
                act_dt = None
            if act_dt and act_dt < cutoff:
                stop = True
                continue

            if aid in existing:
                continue

            name     = act.get('activityName', '')
            date_str = start_local[:10]
            dist_km  = round((act.get('distance') or 0) / 1000, 2) or None
            dur_min  = round((act.get('duration') or 0) / 60) or None
            avg_hr   = act.get('averageHR')
            elev     = act.get('elevationGain')
            avg_spd  = act.get('averageSpeed')
            pace_sec = round(1000 / avg_spd) if avg_spd and avg_spd > 0 else None
            cadence  = act.get('averageRunningCadenceInStepsPerMinute')
            stype    = map_type(name, avg_hr)

            effort = 3
            if avg_hr:
                fc = float(avg_hr)
                if fc >= FC_MAX*0.93:   effort = 5
                elif fc >= FC_MAX*0.88: effort = 4
                elif fc >= FC_MAX*0.80: effort = 3
                elif fc >= FC_MAX*0.70: effort = 2
                else:                   effort = 1  # EF → effort faible

            time.sleep(0.3)

            splits_data = parse_splits(client, aid)
            log.info(f"  → splits: {len(splits_data) if splits_data else 0} km")

            # Type recalculé avec Karvonen
            res = FC_MAX - FC_REPO
            if avg_hr and stype == 'EF':
                fc = float(avg_hr)
                if   fc >= FC_REPO + res*0.90: stype = 'VMA'
                elif fc >= FC_REPO + res*0.80: stype = 'Seuil'
                elif fc >= FC_REPO + res*0.70: stype = 'Aérobie Z3'

            streams_data = None
            try:
                streams_data = get_streams(client, aid)
                if streams_data:
                    log.info(f"  → {streams_data['total_points']} pts | zones: {streams_data['zone_minutes']}")
            except Exception as e:
                log.warning(f"  Streams erreur: {e}")

            power_avg = act.get('avgPower')
            kilojoules = act.get('calories')  # kcal, pas kJ — pas d'équivalent kJ direct chez Garmin

            session = {
                "date":               date_str,
                "type":               stype,
                "distance_km":        dist_km,
                "duration_minutes":   dur_min,
                "avg_pace_seconds":   pace_sec,
                "avg_hr":             int(avg_hr) if avg_hr else None,
                "perceived_effort":   effort,
                "pain_level":         0,
                "strava_name":        name,
                "notes":              f"{name} · Garmin",
                "completed":          True,
                "garmin_activity_id": aid,
                "elevation_gain":     round(elev, 1) if elev else None,
                "cadence_avg":        int(cadence) if cadence else None,
                "streams":            streams_data,
                "splits":             splits_data,
                "best_efforts":       None,
                "suffer_score":       None,
                "power_avg":          int(power_avg) if power_avg else None,
                "power_weighted":     None,
                "kilojoules":         None,
                "avg_temp":           None,
            }

            upsert_session(session)
            existing.add(aid)
            total += 1
            time.sleep(0.3)

        if stop or len(activities) < limit:
            break
        start += limit
        time.sleep(0.5)

    log.info(f"Sync terminé — {total} activités ✓")


if __name__ == '__main__':
    days = int(os.environ.get('DAYS_BACK', '7'))
    sync(days_back=days)
