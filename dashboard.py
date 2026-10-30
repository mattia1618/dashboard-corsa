#!/usr/bin/env python3
"""
dashboard.py — Dashboard di allenamento self-contained a partire dai dati Garmin.

USO:
    python dashboard.py

Riusa i token OAuth già in cache in ~/.garminconnect (libreria `garminconnect`).
Non chiede mai la password. Rilancialo per aggiornare index.html.

Requisiti:
    pip install garminconnect requests

Note di progettazione / limiti onesti:
- CTL/ATL/TSB sono una STIMA (EWMA 42/7gg sul carico Garmin per attività),
  non un dato ufficiale Garmin/TrainingPeaks.
- Il dettaglio "percorso + battiti + passo + cadenza + dislivello" per singola
  attività usa un endpoint Garmin (get_activity_details) che restituisce
  molti punti al secondo; la struttura esatta dei campi può variare da
  account ad account. Lo script prova a riconoscere i campi per nome
  (latitudine/longitudine/battito/velocità/quota/cadenza) e mostra solo
  quello che trova davvero.
- "Oggi / Settimana / Mese" sui grafici ingranditi filtra rispettivamente
  l'ultimo punto, gli ultimi 7 e gli ultimi 30 punti della serie — non
  giorni di calendario in senso stretto, per funzionare in modo uniforme
  sia su serie giornaliere che settimanali.
- Chart.js è incorporato nell'HTML (scaricato una volta), non da CDN.
"""

import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta

# ----------------------------------------------------------------------------
# CONFIGURAZIONE
# ----------------------------------------------------------------------------
RACE_DATE = date(2026, 11, 8)
RACE_NAME = "10 km"

ATHLETE = {
    "height_cm": 182,
    "weight_kg": 80,
    "pb_5k_sec": 27 * 60 + 18,
    "pb_13k_sec": 1 * 3600 + 22 * 60,
}

TOKENSTORE = os.path.expanduser("~/.garminconnect")
OUTPUT_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
CHARTJS_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chart.umd.min.js")
CHARTJS_URL = "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"

WEEKS_TREND = 12
LOAD_LOOKBACK_DAYS = WEEKS_TREND * 7 + 60
RECOVERY_LOOKBACK_DAYS = 8 * 7
VO2_SAMPLE_EVERY_DAYS = 5
ACTIVITY_TABLE_WEEKS = 4
SPARKLINE_DAYS = 30
TIMESERIES_MAX_POINTS = 250

RUNNING_TYPE_KEYS = {
    "running", "track_running", "trail_running", "treadmill_running",
    "indoor_running", "street_running",
}

GIORNI_IT = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]

# ----------------------------------------------------------------------------
# LOGIN
# ----------------------------------------------------------------------------

def get_client():
    try:
        import garminconnect
    except ImportError:
        sys.exit("ERRORE: manca la libreria 'garminconnect'.\nInstallala con:  pip install garminconnect\n")
    try:
        client = garminconnect.Garmin()
        # Su GitHub i token arrivano dal segreto GARMINTOKENS, sul PC dalla cartella
        client.login(os.getenv("GARMINTOKENS") or TOKENSTORE)
        return client
    except Exception as err:
        sys.exit(
            "ERRORE: impossibile autenticarsi usando i token in cache in "
            f"'{TOKENSTORE}'.\nDettaglio: {err}\n\n"
            "Questo script non chiede mai la password. Per rigenerare i "
            "token una tantum, esegui in un terminale (una sola volta):\n\n"
            "  python -c \"import garminconnect, garth; "
            "g = garminconnect.Garmin(input('email: '), input('password: ')); "
            f"g.login(); g.garth.dump('{TOKENSTORE}')\"\n\n"
            "Poi rilancia semplicemente: python dashboard.py"
        )


def get_chartjs_source():
    if os.path.exists(CHARTJS_CACHE):
        with open(CHARTJS_CACHE, "r", encoding="utf-8") as f:
            return f.read()
    try:
        import requests
        print("  -> Prima esecuzione: scarico Chart.js (una tantum)...")
        resp = requests.get(CHARTJS_URL, timeout=20)
        resp.raise_for_status()
        with open(CHARTJS_CACHE, "w", encoding="utf-8") as f:
            f.write(resp.text)
        return resp.text
    except Exception as err:
        sys.exit(f"ERRORE: non riesco a scaricare Chart.js ({err}).\nControlla la connessione internet e riprova.")


# ----------------------------------------------------------------------------
# HELPERS GENERICI
# ----------------------------------------------------------------------------

def safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def daterange(start_d, end_d):
    d = start_d
    while d <= end_d:
        yield d
        d += timedelta(days=1)


def iso_week_key(d):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def linreg(xs, ys):
    n = len(xs)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    if den == 0:
        return 0.0, mean_y
    slope = num / den
    return slope, mean_y - slope * mean_x


def sec_to_pace_str(sec_per_km):
    if sec_per_km is None or sec_per_km <= 0 or math.isinf(sec_per_km):
        return None
    m = int(sec_per_km // 60)
    s = int(round(sec_per_km % 60))
    if s == 60:
        m += 1
        s = 0
    return f"{m}:{s:02d}/km"


def sec_to_hms(sec):
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_day(iso):
    """'2026-08-02' -> '02/08' per le etichette dei grafici."""
    return f"{iso[8:10]}/{iso[5:7]}"


def delta_7d(series, key="value"):
    """Differenza tra l'ultimo valore e quello di circa 7 giorni prima (None se manca)."""
    if not series:
        return None
    cutoff = (date.fromisoformat(series[-1]["date"]) - timedelta(days=7)).isoformat()
    older = [p for p in series if p["date"] <= cutoff]
    return round(series[-1][key] - older[-1][key], 1) if older else None


def sec_to_mmss(sec):
    m = int(sec // 60)
    s = int(round(sec % 60))
    return f"{m}:{s:02d}"


def downsample(seq, max_points):
    n = len(seq)
    if n <= max_points:
        return list(seq)
    stride = math.ceil(n / max_points)
    return seq[::stride]


# ----------------------------------------------------------------------------
# FETCH — dati aggregati
# ----------------------------------------------------------------------------

def fetch_activities(client, since_days):
    print(f"  -> Scarico attività (ultimi {since_days} giorni)...")
    raw = safe(client.get_activities, 0, 1000) or []
    cutoff = datetime.now() - timedelta(days=since_days)
    activities = []
    for a in raw:
        try:
            start_str = a.get("startTimeLocal")
            if not start_str:
                continue
            start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
            if start_dt < cutoff:
                continue
            activities.append(a)
        except Exception:
            continue
    print(f"     trovate {len(activities)} attività totali nel periodo")
    return activities


def activity_type_key(a):
    t = a.get("activityType") or {}
    return (t.get("typeKey") or "").lower()


SPORT_LABELS = {
    "running": "Corsa", "track_running": "Corsa (pista)", "trail_running": "Trail",
    "treadmill_running": "Corsa (tapis)", "indoor_running": "Corsa (indoor)", "street_running": "Corsa",
    "cycling": "Bici", "road_biking": "Bici (strada)", "mountain_biking": "MTB", "indoor_cycling": "Bici (indoor)",
    "lap_swimming": "Nuoto", "open_water_swimming": "Nuoto (acque libere)",
    "walking": "Camminata", "hiking": "Escursione",
    "strength_training": "Palestra", "fitness_equipment": "Palestra",
    "cardio_training": "Cardio", "yoga": "Yoga", "elliptical": "Ellittica",
    "other": "Altro",
}


def sport_label(a):
    key = activity_type_key(a)
    if key in SPORT_LABELS:
        return SPORT_LABELS[key]
    t = a.get("activityType") or {}
    typekey = t.get("typeKey") or "Altro"
    return typekey.replace("_", " ").capitalize()


def is_pace_sport(a):
    """Attività per cui il passo min/km ha senso (corsa/camminata)."""
    return activity_type_key(a) in RUNNING_TYPE_KEYS or activity_type_key(a) in {"walking", "hiking"}


def is_running(a):
    return activity_type_key(a) in RUNNING_TYPE_KEYS


def estimate_load(a):
    load = a.get("activityTrainingLoad")
    if load:
        return float(load)
    duration_min = (a.get("duration") or 0) / 60.0
    avg_hr = a.get("averageHR")
    if duration_min and avg_hr:
        return duration_min * max(avg_hr / 130.0, 0.3)
    if duration_min:
        return duration_min * 0.7
    return 0.0


def fetch_daily_wellness(client, since_days):
    print(f"  -> Scarico dati di recupero (RHR/sonno/HRV) ultimi {since_days} giorni...")
    today = date.today()
    start = today - timedelta(days=since_days)
    rhr_series, sleep_series, hrv_series = [], [], []
    found = {"rhr": 0, "sleep": 0, "hrv": 0}

    for d in daterange(start, today):
        ds = d.isoformat()
        stats = safe(client.get_stats, ds)
        rhr = stats.get("restingHeartRate") if stats else None
        if rhr:
            rhr_series.append({"date": ds, "value": rhr})
            found["rhr"] += 1

        sleep = safe(client.get_sleep_data, ds)
        if sleep:
            dto = sleep.get("dailySleepDTO") or {}
            secs = dto.get("sleepTimeSeconds")
            if secs:
                sleep_series.append({"date": ds, "hours": round(secs / 3600, 2)})
                found["sleep"] += 1

        hrv = safe(client.get_hrv_data, ds)
        if hrv:
            summary = hrv.get("hrvSummary") or {}
            val = summary.get("lastNightAvg") or summary.get("weeklyAvg")
            if val:
                hrv_series.append({"date": ds, "value": val})
                found["hrv"] += 1

        time.sleep(0.05)

    print(f"     RHR: {found['rhr']} giorni | Sonno: {found['sleep']} giorni | HRV: {found['hrv']} giorni")
    return rhr_series, sleep_series, hrv_series


def fetch_vo2max(client, since_days, every_days):
    print(f"  -> Scarico VO2max (campionato ogni {every_days} giorni)...")
    today = date.today()
    start = today - timedelta(days=since_days)
    series = []
    seen_dates = set()
    d = start
    while d <= today:
        res = safe(client.get_max_metrics, d.isoformat())
        if res:
            item = res[0] if isinstance(res, list) and res else res
            generic = (item or {}).get("generic") or {}
            val = generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")
            if val:
                series.append({"date": d.isoformat(), "value": round(float(val), 1)})
                seen_dates.add(d.isoformat())
        d += timedelta(days=every_days)
        time.sleep(0.05)

    # Il campionamento a intervalli può "saltare" oggi: aggiungo/aggiorno
    # sempre un punto per il valore più recente, così la card in alto non
    # mostra mai un dato vecchio.
    today_iso = today.isoformat()
    if today_iso not in seen_dates:
        res_today = safe(client.get_max_metrics, today_iso)
        if res_today:
            item = res_today[0] if isinstance(res_today, list) and res_today else res_today
            generic = (item or {}).get("generic") or {}
            val = generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")
            if val:
                series.append({"date": today_iso, "value": round(float(val), 1)})

    print(f"     trovati {len(series)} punti VO2max")
    return series


# ----------------------------------------------------------------------------
# FETCH — dettaglio per singola attività (dati grezzi + serie temporali)
# ----------------------------------------------------------------------------

def find_metric_index(key_to_idx, *substrings):
    for k, idx in key_to_idx.items():
        if all(s in k for s in substrings):
            return idx
    return None


def smooth_series(values, window=7, clip_min=120, clip_max=900):
    """Media mobile che ignora i buchi e scarta valori assurdi (rumore GPS)."""
    n = len(values)
    out = [None] * n
    half = window // 2
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        chunk = [v for v in values[lo:hi] if v is not None and clip_min <= v <= clip_max]
        out[i] = round(sum(chunk) / len(chunk), 1) if chunk else None
    return out


def parse_activity_timeseries(details_raw):
    """Prova a estrarre lat/lon, battito, passo, cadenza, quota, distanza e
    tempo da get_activity_details. Ritorna None se la struttura non è quella
    attesa o non ci sono dati punto-per-punto (es. attività senza GPS)."""
    if not details_raw:
        return None
    descriptors = details_raw.get("metricDescriptors") or []
    rows = details_raw.get("activityDetailMetrics") or []
    if not descriptors or not rows:
        return None

    key_to_idx = {}
    for desc in descriptors:
        k = (desc.get("key") or "").lower()
        idx = desc.get("metricsIndex")
        if k and idx is not None:
            key_to_idx[k] = idx

    idx_lat = find_metric_index(key_to_idx, "latitude")
    idx_lon = find_metric_index(key_to_idx, "longitude")
    idx_hr = find_metric_index(key_to_idx, "heartrate")
    idx_speed = find_metric_index(key_to_idx, "speed")
    idx_elev = find_metric_index(key_to_idx, "elevation") or find_metric_index(key_to_idx, "altitude")
    # "directFractionalCadence" è solo la parte decimale (0-0.5): va saltata
    idx_cad = find_metric_index(key_to_idx, "doublecadence")
    if idx_cad is None:
        idx_cad = next((i for k, i in key_to_idx.items() if "cadence" in k and "fractional" not in k), None)
    idx_dist = find_metric_index(key_to_idx, "distance")
    idx_time = find_metric_index(key_to_idx, "timestamp")

    if idx_time is None:
        return None

    lat, lon, hr, elevation, cadence, distance_m, time_s = [], [], [], [], [], [], []
    t0 = None

    def g(vals, idx):
        if idx is None or idx >= len(vals):
            return None
        return vals[idx]

    for row in rows:
        vals = row.get("metrics") or []
        ts = g(vals, idx_time)
        if ts is None:
            continue
        if t0 is None:
            t0 = ts
        t_rel = (ts - t0) / 1000.0 if ts > 1e12 else (ts - t0)

        la, lo = g(vals, idx_lat), g(vals, idx_lon)
        if la is not None and lo is not None:
            lat.append(round(la, 6))
            lon.append(round(lo, 6))

        h = g(vals, idx_hr)
        hr.append(h if h is not None else None)

        el = g(vals, idx_elev)
        elevation.append(round(el, 1) if el is not None else None)

        cd = g(vals, idx_cad)
        cadence.append(cd if cd else None)  # 0 = fermo: lo salto, altrimenti schiaccia l'asse a 0

        dm = g(vals, idx_dist)
        distance_m.append(dm if dm is not None else None)

        time_s.append(round(t_rel, 1))

    if not time_s:
        return None

    # cadenza implausibile (chiave sbagliata o unità diversa): meglio nessun grafico che uno falso
    cad_vals = sorted(v for v in cadence if v)
    if cad_vals and cad_vals[len(cad_vals) // 2] < 30:
        cadence = []

    # split per km (usa distanza+tempo a piena risoluzione, prima di scalare)
    km_splits = []
    if any(d is not None for d in distance_m):
        clean = [(t, dd) for t, dd in zip(time_s, distance_m) if dd is not None]
        target = 1000.0
        last_t = 0.0
        for i in range(1, len(clean)):
            t0i, d0 = clean[i - 1]
            t1i, d1 = clean[i]
            while d1 >= target:
                frac = (target - d0) / (d1 - d0) if d1 > d0 else 0
                t_at = t0i + frac * (t1i - t0i)
                km_splits.append(round(t_at - last_t, 1))
                last_t = t_at
                target += 1000.0

    n = len(time_s)

    pace_full = [None] * n
    for i in range(1, n):
        dt_ = time_s[i] - time_s[i - 1]
        d0, d1 = distance_m[i - 1], distance_m[i]
        if dt_ and d0 is not None and d1 is not None and d1 > d0:
            speed = (d1 - d0) / dt_
            if speed > 0:
                pace_full[i] = 1000.0 / speed
    pace_smoothed = smooth_series(pace_full)

    max_pts = TIMESERIES_MAX_POINTS
    stride = max(1, math.ceil(n / max_pts))

    def ds(seq):
        return seq[::stride] if seq else []

    return {
        "lat": ds(lat),
        "lon": ds(lon),
        "hr": ds(hr),
        "elevation": ds(elevation),
        "cadence": ds(cadence),
        "pace_sec_km": ds(pace_smoothed),
        "distance_km": ds([d / 1000.0 if d is not None else None for d in distance_m]),
        "time_s": ds(time_s),
        "km_splits_sec": km_splits,
    }


def fetch_activity_details(client, activities):
    """Scarica dettaglio completo + serie temporale (traccia GPS, battiti,
    passo, cadenza, quota) per ogni attività che finirà nella tabella."""
    print(f"  -> Scarico dettagli completi per {len(activities)} attività...")
    details = {}
    timeseries = {}
    ok_ts = 0
    for a in activities:
        aid = a.get("activityId")
        if not aid:
            continue
        full = safe(client.get_activity, aid)
        splits = safe(client.get_activity_splits, aid)
        merged = dict(full or a)
        if splits:
            merged["_splits"] = splits
        details[str(aid)] = merged

        raw_ts = safe(client.get_activity_details, aid)
        parsed = parse_activity_timeseries(raw_ts)
        if parsed:
            timeseries[str(aid)] = parsed
            ok_ts += 1

        time.sleep(0.05)
    print(f"     dettagli scaricati per {len(details)} attività ({ok_ts} con traccia/serie temporale)")
    return details, timeseries


# ----------------------------------------------------------------------------
# CALCOLI AGGREGATI
# ----------------------------------------------------------------------------

def compute_ctl_atl_tsb(activities, lookback_days):
    today = date.today()
    start = today - timedelta(days=lookback_days)
    daily_load = {}
    for a in activities:
        try:
            d = datetime.strptime(a.get("startTimeLocal"), "%Y-%m-%d %H:%M:%S").date()
        except Exception:
            continue
        if d < start:
            continue
        daily_load[d] = daily_load.get(d, 0.0) + estimate_load(a)

    ctl = atl = 0.0
    series = []
    for d in daterange(start, today):
        load = daily_load.get(d, 0.0)
        ctl = ctl + (load - ctl) / 42.0
        atl = atl + (load - atl) / 7.0
        series.append({"date": d.isoformat(), "ctl": round(ctl, 1), "atl": round(atl, 1), "tsb": round(ctl - atl, 1)})
    return series


def compute_weekly_mileage(activities, weeks):
    today = date.today()
    start = today - timedelta(weeks=weeks)
    weekly = {}
    for a in activities:
        if not is_running(a):
            continue
        try:
            d = datetime.strptime(a.get("startTimeLocal"), "%Y-%m-%d %H:%M:%S").date()
        except Exception:
            continue
        if d < start:
            continue
        wk = iso_week_key(d)
        weekly[wk] = weekly.get(wk, 0.0) + (a.get("distance") or 0) / 1000.0

    if not weekly:
        return {"labels": [], "values": [], "trend": []}
    # tutte le settimane, anche quelle a 0 km, così non spariscono barre
    mondays = []
    d = start - timedelta(days=start.weekday())
    while d <= today:
        mondays.append(d)
        d += timedelta(weeks=1)
    values = [round(weekly.get(iso_week_key(m), 0.0), 1) for m in mondays]
    slope, intercept = linreg(list(range(len(values))), values)
    trend = [round(intercept + slope * i, 1) for i in range(len(values))]
    return {"labels": [f"{m:%d/%m}" for m in mondays], "values": values, "trend": trend}


def classify_effort(a, pb_5k_pace_sec_km):
    aerobic_te = a.get("aerobicTrainingEffect") or 0
    anaerobic_te = a.get("anaerobicTrainingEffect") or 0
    if aerobic_te >= 3.5 or anaerobic_te >= 2.0:
        return "workout"
    if is_pace_sport(a):
        dist_m = a.get("distance") or 0
        dur_s = a.get("duration") or 0
        if dist_m and dur_s:
            pace_sec_km = dur_s / (dist_m / 1000.0)
            if pb_5k_pace_sec_km and pace_sec_km <= pb_5k_pace_sec_km * 1.12:
                return "workout"
    return "easy"


def build_recent_table(activities, weeks, pb_5k_pace_sec_km):
    today = date.today()
    start = today - timedelta(weeks=weeks)
    rows = []
    for a in activities:
        try:
            dt = datetime.strptime(a.get("startTimeLocal"), "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if dt.date() < start:
            continue
        dist_km = (a.get("distance") or 0) / 1000.0
        dur_s = a.get("duration") or 0
        pace = None
        if is_pace_sport(a) and dist_km:
            pace = sec_to_pace_str(dur_s / dist_km)
        effort = classify_effort(a, pb_5k_pace_sec_km)
        if effort == "easy" and not is_pace_sport(a):
            effort = "other"  # palestra/yoga/bici: "Facile" non ha senso
        rows.append({
            "activity_id": str(a.get("activityId")),
            "date": dt.strftime("%d/%m/%Y"),
            "sort_key": dt.isoformat(),
            "name": a.get("activityName") or sport_label(a),
            "sport": sport_label(a),
            "distance_km": round(dist_km, 2) if dist_km else None,
            "pace": pace,
            "avg_hr": round(a["averageHR"]) if a.get("averageHR") else None,
            "max_hr": round(a["maxHR"]) if a.get("maxHR") else None,
            "type": effort,
            "duration": sec_to_hms(dur_s) if dur_s else None,
        })
    rows.sort(key=lambda r: r["sort_key"], reverse=True)
    for r in rows:
        del r["sort_key"]
    return rows


def build_week_row(running_activities, pb_5k_pace_sec_km):
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    days = [monday + timedelta(days=i) for i in range(7)]
    by_day = {d: [] for d in days}
    for a in running_activities:
        try:
            dt = datetime.strptime(a.get("startTimeLocal"), "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if dt.date() in by_day:
            by_day[dt.date()].append(a)

    result = []
    for i, d in enumerate(days):
        acts = by_day[d]
        km = sum((a.get("distance") or 0) / 1000.0 for a in acts)
        if not acts:
            kind = "rest"
        else:
            main = max(acts, key=lambda a: a.get("distance") or 0)
            kind = classify_effort(main, pb_5k_pace_sec_km)
        result.append({"label": GIORNI_IT[i], "km": round(km, 1), "kind": kind, "date": d.isoformat()})
    return result


def compute_insights(load_series, mileage, vo2_series, days_left):
    insights = []
    taper = 0 < days_left <= 21
    if load_series:
        tsb = load_series[-1]["tsb"]
        if tsb < -15 and taper:
            insights.append({"color": "danger", "title": f"Stanco a {days_left} giorni dalla gara",
                              "text": f"Freschezza a {tsb}: è il momento di scaricare. Riduci il volume del 20-30% a settimana e tieni una sola seduta breve a ritmo gara: la freschezza risalirà in tempo per il {RACE_DATE.strftime('%d/%m')}."})
        elif tsb < -15:
            insights.append({"color": "danger", "title": "Fatica alta",
                              "text": f"Freschezza a {tsb}: la fatica è parecchio sopra la forma fisica. Meglio 2-3 giorni facili prima della prossima sessione di qualità."})
        elif taper:
            insights.append({"color": "good" if tsb > -5 else "info", "title": f"Scarico pre-gara: {days_left} giorni",
                              "text": f"Freschezza a {tsb}. Nelle ultime settimane riduci il volume e mantieni un po' di ritmo gara: l'obiettivo è arrivare al via con la freschezza sopra 0."})
        elif tsb > 5:
            insights.append({"color": "good", "title": "Sei fresco",
                              "text": f"Freschezza a +{tsb}: buon momento per una sessione impegnativa o per testarti in gara."})
        else:
            insights.append({"color": "info", "title": "Equilibrio ok",
                              "text": f"Freschezza a {tsb}: forma fisica e fatica sono bilanciate, puoi proseguire il piano come previsto."})

    # la settimana in corso è incompleta fino a domenica: confronto le ultime due complete
    values = mileage.get("values") or []
    if date.today().weekday() < 6:
        values = values[:-1]
    if len(values) >= 2 and values[-2] > 0:
        pct = (values[-1] - values[-2]) / values[-2] * 100
        change = f"{values[-2]} → {values[-1]} km, {pct:+.0f}%"
        if pct >= 20:
            insights.append({"color": "danger", "title": "Volume salito troppo in fretta",
                              "text": f"Ultima settimana completa contro quella prima: {change}. Sopra il +10% aumenta il rischio di infortuni."})
        elif pct >= 10:
            insights.append({"color": "warn", "title": "Volume in aumento",
                              "text": f"Ultima settimana completa contro quella prima: {change}. Restare intorno al +10% è più prudente."})
        elif pct <= -25:
            insights.append({"color": "info", "title": "Settimana di scarico",
                              "text": f"Ultima settimana completa contro quella prima: {change}."})
        else:
            insights.append({"color": "good", "title": "Volume stabile",
                              "text": f"Ultima settimana completa in linea con quella prima: {change}."})

    if len(vo2_series) >= 2:
        diff = vo2_series[-1]["value"] - vo2_series[0]["value"]
        if diff > 0.5:
            insights.append({"color": "good", "title": "VO2max in crescita",
                              "text": f"Da {vo2_series[0]['value']} a {vo2_series[-1]['value']} nel periodo osservato."})
        elif diff < -0.5:
            insights.append({"color": "info", "title": "VO2max in calo",
                              "text": f"Da {vo2_series[0]['value']} a {vo2_series[-1]['value']}: tienilo d'occhio."})

    # il consiglio più urgente per primo: è quello che la pagina mette in evidenza
    order = {"danger": 0, "warn": 1, "info": 2, "good": 3}
    insights.sort(key=lambda i: order[i["color"]])
    return insights


def predict_10k_from_5k(pb_5k_sec):
    return pb_5k_sec * (10.0 / 5.0) ** 1.06


def build_activity_summary(a):
    """Riepilogo numerico pulito (non il dump grezzo) per la card in alto
    nella schermata di dettaglio di un'attività."""
    # get_activity mette i numeri dentro summaryDTO; la lista attività li ha al primo livello
    a = a.get("summaryDTO") or a
    dist_km = (a.get("distance") or 0) / 1000.0
    dur_s = a.get("duration") or 0
    pace = sec_to_pace_str(dur_s / dist_km) if dist_km else None
    elevation_gain = a.get("elevationGain")
    avg_cadence = (
        a.get("averageRunningCadenceInStepsPerMinute")
        or a.get("averageRunCadence")
        or a.get("averageBikingCadenceInRevPerMinute")
    )
    return {
        "distanza": f"{dist_km:.2f} km" if dist_km else "—",
        "durata": sec_to_hms(dur_s) if dur_s else "—",
        "passo_medio": pace or "—",
        "fc_media": f"{round(a.get('averageHR'))} bpm" if a.get("averageHR") else "—",
        "fc_max": f"{round(a.get('maxHR'))} bpm" if a.get("maxHR") else "—",
        "dislivello": f"{round(elevation_gain)} m" if elevation_gain else "—",
        "cadenza_media": f"{round(avg_cadence)}" if avg_cadence else "—",
        "calorie": f"{round(a.get('calories'))} kcal" if a.get("calories") else "—",
    }


def js_escape(s):
    """Rende sicura una stringa da inserire dentro un attributo onclick con
    apici singoli in JS (es. nomi di allenamenti con apostrofi tipo 8x1')."""
    if s is None:
        return ""
    return str(s).replace("\\", "\\\\").replace("'", "\\'").replace('"', "&quot;")


def json_for_script(obj):
    """json.dumps + escape di '</' per evitare che testo libero (es. nomi di
    attività con caratteri strani) chiuda accidentalmente il tag <script>."""
    return json.dumps(obj).replace("</", "<\\/")


def flatten_for_json(obj):
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        return json.loads(json.dumps(obj, default=str))


# ----------------------------------------------------------------------------
# HTML TEMPLATE
# ----------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard allenamento · {race_name}</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Ccircle cx='8' cy='8' r='7' fill='%234fd1c5'/%3E%3C/svg%3E">
<script>
{chartjs_source}
</script>
<style>
  :root {{
    --bg: #000000;
    --panel: #0d0d0d;
    --panel-border: #232323;
    --panel-hover: #161616;
    --text: #e8eaf2;
    --muted: #8b93a9;
    --accent: #4fd1c5;
    --accent2: #f6ad55;
    --danger: #f56565;
    --good: #68d391;
    --info: #63b3ed;
    --hrv: #b794f4;
    color-scheme: dark;
  }}
  * {{ box-sizing: border-box; }}
  html {{ scrollbar-color: #2c2c2e var(--bg); }}
  body {{ margin: 0; background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; font-size: 14px; line-height: 1.45; padding: 22px; }}
  ::selection {{ background: rgba(79,209,197,0.35); color: var(--text); }}
  :focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
  .container {{ max-width: 1180px; margin: 0 auto; }}
  .num, .v, .km, table {{ font-variant-numeric: tabular-nums; }}


  .top-grid {{ display: grid; grid-template-columns: 1.1fr 2fr; gap: 16px; margin-bottom: 16px; }}
  .hero {{ background: var(--panel); border: 1px solid var(--panel-border); border-radius: 14px; padding: 22px; }}
  .hero h1 {{ margin: 0 0 6px 0; font-size: 17px; font-weight: 600; }}
  .hero .num {{ font-size: 56px; font-weight: 700; color: var(--accent); line-height: 1; letter-spacing: -0.02em; }}
  .hero .sub {{ color: var(--muted); font-size: 13px; margin-top: 4px; }}
  .hero .predict {{ margin-top: 14px; font-size: 13px; color: var(--muted); }}
  .hero .predict strong {{ color: var(--text); font-size: 15px; }}

  .stat-cards {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; }}
  .stat-card {{ background: var(--panel); border: 1px solid var(--panel-border); border-radius: 12px; padding: 12px; display: flex; flex-direction: column; min-width: 0; }}
  .stat-card .l {{ font-size: 12px; color: var(--muted); }}
  .stat-card .l abbr {{ text-decoration: none; font-size: 11px; }}
  .stat-card .v {{ font-size: 22px; font-weight: 700; margin: 2px 0 0 0; }}
  .stat-card .delta {{ font-size: 12px; color: var(--muted); margin-bottom: 6px; }}
  .stat-card .delta.good {{ color: var(--good); }}
  .stat-card .delta.bad {{ color: var(--danger); }}
  .stat-card canvas {{ max-height: 34px !important; height: 34px !important; margin-top: auto; }}

  .insights {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(260px, 100%), 1fr)); gap: 12px; margin-bottom: 16px; }}
  .insight {{ --c: var(--info); background: color-mix(in srgb, var(--c) 6%, var(--panel)); border: 1px solid color-mix(in srgb, var(--c) 28%, var(--panel-border)); border-radius: 12px; padding: 14px 16px; }}
  .insight.danger {{ --c: var(--danger); }}
  .insight.warn {{ --c: var(--accent2); }}
  .insight.good {{ --c: var(--good); }}
  .insight h2 {{ margin: 0 0 4px 0; font-size: 15px; color: var(--c); }}
  .insight p {{ margin: 0; font-size: 13px; color: var(--muted); }}
  .insight:first-child {{ grid-column: 1 / -1; padding: 18px 20px; }}
  .insight:first-child h2 {{ font-size: 19px; }}
  .insight:first-child p {{ font-size: 14px; color: var(--text); max-width: 75ch; }}

  .week-row {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 8px; }}
  .day-box {{ background: var(--panel); border: 1px solid var(--panel-border); border-radius: 10px; padding: 10px 4px; text-align: center; }}
  .day-box.today {{ border-color: var(--accent); }}
  .day-box .d {{ font-size: 12px; color: var(--muted); }}
  .day-box.today .d {{ color: var(--accent); font-weight: 600; }}
  .day-box .km {{ font-size: 16px; font-weight: 700; margin-top: 2px; }}
  .day-box.easy .km {{ color: var(--good); }}
  .day-box.workout .km {{ color: var(--accent2); }}
  .day-box.rest .km {{ color: var(--muted); }}
  .legend {{ display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--muted); margin: 8px 0 22px 0; }}
  .legend span::before {{ content: ''; display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; background: var(--muted); }}
  .legend .easy::before {{ background: var(--good); }}
  .legend .workout::before {{ background: var(--accent2); }}

  .tabs {{ display: flex; gap: 4px; border-bottom: 1px solid var(--panel-border); margin-bottom: 16px; }}
  .tabbtn {{ background: transparent; border: none; color: var(--muted); font-size: 14px; padding: 10px 16px; cursor: pointer; font-family: inherit; border-bottom: 2px solid transparent; margin-bottom: -1px; }}
  .tabbtn:hover {{ color: var(--text); }}
  .tabbtn[aria-selected="true"] {{ color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }}

  .section {{ display: none; }}
  .section.active {{ display: block; }}

  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(360px, 100%), 1fr)); gap: 18px; }}
  .grid.three {{ grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr)); }}
  .panel {{ background: var(--panel); border: 1px solid var(--panel-border); border-radius: 14px; padding: 20px; min-width: 0; }}
  .panel.chart-panel {{ cursor: zoom-in; transition: border-color .15s; }}
  .panel.chart-panel:hover {{ border-color: var(--accent); }}
  .panel h2 {{ margin: 0 0 4px 0; font-size: 18px; }}
  .panel h2 abbr {{ text-decoration: none; color: var(--muted); font-weight: 400; font-size: 14px; }}
  .panel .desc {{ color: var(--muted); font-size: 13px; margin-bottom: 14px; max-width: 75ch; }}
  .panel.full {{ grid-column: 1 / -1; }}
  .missing {{ color: var(--muted); font-size: 13px; font-style: italic; padding: 20px 0; text-align: center; }}
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--panel-border); }}
  th {{ color: var(--muted); font-weight: 500; font-size: 12px; }}
  tr.clickable {{ cursor: pointer; }}
  tr.clickable:hover td, tr.clickable:focus-visible td {{ background: var(--panel-hover); }}
  tr.clickable:focus-visible {{ outline-offset: -2px; }}
  .badge {{ padding: 2px 8px; border-radius: 20px; font-size: 12px; font-weight: 600; white-space: nowrap; }}
  .badge.easy {{ background: rgba(104,211,145,0.15); color: var(--good); }}
  .badge.workout {{ background: rgba(246,173,85,0.15); color: var(--accent2); }}
  .muted {{ color: var(--muted); }}
  canvas {{ max-height: 260px; }}

  .modal-overlay {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.75); z-index: 50; align-items: center; justify-content: center; opacity: 0; transition: opacity .18s; }}
  .modal-overlay.active {{ display: flex; opacity: 1; }}
  .modal-box {{ background: var(--panel); border: 1px solid var(--panel-border); border-radius: 14px; padding: 24px; width: 92%; overflow-y: auto; transform: scale(.96); transition: transform .18s cubic-bezier(.16,1,.3,1); }}
  .modal-overlay.active .modal-box {{ transform: scale(1); }}
  .modal-box.large {{ max-width: 980px; max-height: 88vh; }}
  .modal-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 12px; }}
  .modal-box h3 {{ margin: 0; font-size: 21px; }}
  .modal-close {{ flex: none; background: var(--panel-hover); border: 1px solid var(--panel-border); color: var(--text); border-radius: 8px; padding: 6px 14px; cursor: pointer; font-family: inherit; font-size: 13px; }}
  .detail-table td:first-child {{ color: var(--muted); width: 45%; }}

  .range-btns {{ display: flex; gap: 4px; margin-bottom: 12px; flex-wrap: wrap; }}
  .range-btn {{ background: var(--panel-hover); border: 1px solid var(--panel-border); color: var(--muted); font-size: 12px; padding: 5px 12px; border-radius: 8px; cursor: pointer; font-family: inherit; }}
  .range-btn[aria-pressed="true"] {{ color: var(--accent); border-color: var(--accent); }}

  .detail-stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 10px; margin-bottom: 14px; }}
  .detail-stats .dstat {{ background: var(--panel-hover); border-radius: 10px; padding: 10px; text-align: center; }}
  .detail-stats .dstat .v {{ font-size: 17px; font-weight: 700; }}
  .detail-stats .dstat .l {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
  .route-wrap {{ background: var(--panel-hover); border-radius: 10px; padding: 8px; margin-bottom: 10px; }}
  .route-wrap svg {{ width: 100%; height: 220px; display: block; }}
  .replay-btn {{ background: var(--panel-hover); border: 1px solid var(--panel-border); color: var(--accent); font-size: 13px; padding: 6px 12px; border-radius: 8px; cursor: pointer; font-family: inherit; margin-bottom: 14px; }}
  .mini-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(220px, 100%), 1fr)); gap: 12px; margin-top: 10px; }}
  .mini-panel {{ background: var(--panel-hover); border-radius: 10px; padding: 10px; cursor: zoom-in; transition: outline-color .15s; outline: 1px solid transparent; }}
  .mini-panel:hover {{ outline-color: var(--accent); }}
  .mini-panel:focus-visible {{ outline: 2px solid var(--accent); }}
  .mini-panel h4 {{ margin: 0 0 6px 0; font-size: 13px; color: var(--muted); font-weight: 600; }}
  .mini-panel canvas {{ max-height: 140px !important; }}
  .mini-stat-row {{ display: flex; gap: 16px; margin-bottom: 6px; }}
  .mini-stat-row .ms {{ font-size: 12px; color: var(--muted); }}
  .mini-stat-row .ms strong {{ display: block; font-size: 15px; color: var(--text); font-weight: 700; }}
  details.raw-toggle {{ margin-top: 14px; }}
  details.raw-toggle summary {{ cursor: pointer; color: var(--muted); font-size: 13px; }}

  @media (prefers-reduced-motion: reduce) {{
    .modal-overlay, .modal-box {{ transition: none; }}
  }}
  @media (max-width: 900px) {{
    .top-grid {{ grid-template-columns: 1fr; }}
  }}
  @media (max-width: 640px) {{
    body {{ padding: 16px; }}
    .hero {{ padding: 18px; }}
    .stat-cards {{ display: flex; overflow-x: auto; scroll-snap-type: x mandatory; scrollbar-width: none; margin: 0 -16px; padding: 0 16px; scroll-padding: 0 16px; }}
    .stat-card {{ flex: 0 0 150px; scroll-snap-align: start; }}
    .week-row {{ gap: 4px; }}
    .panel {{ padding: 16px; }}
    .tabbtn {{ padding: 10px 12px; }}
    .hide-sm {{ display: none; }}
    th, td {{ padding: 8px 6px; }}
    .modal-box {{ width: calc(100% - 20px); padding: 16px; }}
    .modal-box.large {{ max-height: 92vh; }}
  }}
</style>
</head>
<body>
<div class="container">

  <div class="top-grid">
    <div class="hero">
      <h1>Gara {race_name} · {race_date_str}</h1>
      <div class="num">{days_left}</div>
      <div class="sub">giorni alla gara · {weeks_left} settimane</div>
      <div class="predict">Stima sui 10 km: <strong>{predicted_10k}</strong> ({predicted_pace}), calcolata dal tuo PB sui 5 km</div>
    </div>
    <div class="stat-cards">
      <div class="stat-card"><div class="l">Forma fisica <abbr title="Chronic Training Load">CTL</abbr></div><div class="v">{card_ctl}</div>{delta_ctl}<canvas id="sparkCtl" aria-hidden="true"></canvas></div>
      <div class="stat-card"><div class="l">Fatica <abbr title="Acute Training Load">ATL</abbr></div><div class="v">{card_atl}</div>{delta_atl}<canvas id="sparkAtl" aria-hidden="true"></canvas></div>
      <div class="stat-card"><div class="l">VO2 max</div><div class="v">{card_vo2}</div>{delta_vo2}<canvas id="sparkVo2" aria-hidden="true"></canvas></div>
      <div class="stat-card"><div class="l">FC a riposo</div><div class="v">{card_rhr}</div>{delta_rhr}<canvas id="sparkRhr" aria-hidden="true"></canvas></div>
      <div class="stat-card"><div class="l">Variabilità FC <abbr title="Heart Rate Variability">HRV</abbr></div><div class="v">{card_hrv}</div>{delta_hrv}<canvas id="sparkHrv" aria-hidden="true"></canvas></div>
    </div>
  </div>

  <div class="insights">{insights_html}</div>

  <div class="week-row" aria-label="Questa settimana">{week_row_html}</div>
  <div class="legend" aria-hidden="true"><span class="easy">Facile</span><span class="workout">Allenamento</span><span class="rest">Riposo</span></div>

  <div class="tabs" role="tablist">
    <button class="tabbtn" role="tab" id="tab-fitness" aria-controls="section-fitness" aria-selected="true" onclick="showSection('fitness', this)">Forma</button>
    <button class="tabbtn" role="tab" id="tab-recovery" aria-controls="section-recovery" aria-selected="false" tabindex="-1" onclick="showSection('recovery', this)">Recupero</button>
    <button class="tabbtn" role="tab" id="tab-runs" aria-controls="section-runs" aria-selected="false" tabindex="-1" onclick="showSection('runs', this)">Allenamenti</button>
  </div>

  <div class="section active" id="section-fitness" role="tabpanel" aria-labelledby="tab-fitness">
    <div class="grid three">
      <div class="panel chart-panel" role="button" tabindex="0" aria-label="Ingrandisci il grafico Forma fisica" onclick="openEnlarge('ctl')"><h2>Forma fisica <abbr>CTL</abbr></h2><div class="desc">Quanto sei allenato: media del carico degli ultimi 42 giorni. Più è alta, più sei in forma.</div>{ctl_chart_or_missing}</div>
      <div class="panel chart-panel" role="button" tabindex="0" aria-label="Ingrandisci il grafico Fatica" onclick="openEnlarge('atl')"><h2>Fatica <abbr>ATL</abbr></h2><div class="desc">Carico degli ultimi 7 giorni: sale dopo giorni duri, scende con il riposo.</div>{atl_chart_or_missing}</div>
      <div class="panel chart-panel" role="button" tabindex="0" aria-label="Ingrandisci il grafico Freschezza" onclick="openEnlarge('tsb')"><h2>Freschezza <abbr>TSB</abbr></h2><div class="desc">Forma fisica meno fatica. Sotto −20 molto stanco, tra −10 e +5 zona di allenamento, sopra +5 fresco per gareggiare.</div>{tsb_chart_or_missing}</div>
    </div>
    <div class="grid" style="margin-top:18px;">
      <div class="panel chart-panel" role="button" tabindex="0" aria-label="Ingrandisci il grafico Chilometraggio settimanale" onclick="openEnlarge('mileage')"><h2>Chilometraggio settimanale</h2><div class="desc">Ultime {weeks_trend} settimane di corsa (la data è il lunedì), con la linea di tendenza.</div>{mileage_chart_or_missing}</div>
    </div>
  </div>

  <div class="section" id="section-recovery" role="tabpanel" aria-labelledby="tab-recovery">
    <div class="grid">
      <div class="panel chart-panel" role="button" tabindex="0" aria-label="Ingrandisci il grafico HRV" onclick="openEnlarge('hrv')"><h2>Variabilità cardiaca <abbr>HRV</abbr></h2><div class="desc">Ultime {recovery_weeks} settimane. Valori stabili o in salita indicano un buon recupero.</div>{hrv_chart_or_missing}</div>
      <div class="panel chart-panel" role="button" tabindex="0" aria-label="Ingrandisci il grafico FC a riposo" onclick="openEnlarge('rhr')"><h2>FC a riposo</h2><div class="desc">Ultime {recovery_weeks} settimane. Se sale di 5 o più battiti per qualche giorno, il corpo chiede riposo.</div>{rhr_chart_or_missing}</div>
      <div class="panel chart-panel" role="button" tabindex="0" aria-label="Ingrandisci il grafico Sonno" onclick="openEnlarge('sleep')"><h2>Sonno</h2><div class="desc">Ore di sonno per notte.</div>{sleep_chart_or_missing}</div>
      <div class="panel chart-panel" role="button" tabindex="0" aria-label="Ingrandisci il grafico VO2 max" onclick="openEnlarge('vo2')"><h2>VO2 max</h2><div class="desc">Stima del device Garmin della tua capacità aerobica.</div>{vo2_chart_or_missing}</div>
    </div>
  </div>

  <div class="section" id="section-runs" role="tabpanel" aria-labelledby="tab-runs">
    <div class="panel full">
      <h2>Allenamenti recenti</h2>
      <div class="desc">Ultime {table_weeks} settimane, tutti gli sport. Clicca una riga (o premi Invio) per il dettaglio.</div>
      {table_or_missing}
    </div>
  </div>

</div>

<!-- Modal dettaglio attività (percorso + grafici) -->
<div class="modal-overlay" id="detailModal" onclick="if(event.target===this) closeModal('detailModal')">
  <div class="modal-box large" role="dialog" aria-modal="true" aria-labelledby="detailTitle">
    <div class="modal-head">
      <h3 id="detailTitle">Dettaglio attività</h3>
      <button class="modal-close" onclick="closeModal('detailModal')">Chiudi</button>
    </div>
    <div id="detailBody"></div>
    <details class="raw-toggle">
      <summary>Mostra tutti i dati grezzi Garmin</summary>
      <table class="detail-table"><tbody id="detailRawTable"></tbody></table>
    </details>
  </div>
</div>

<!-- Modal grafico ingrandito -->
<div class="modal-overlay" id="enlargeModal" onclick="if(event.target===this) closeModal('enlargeModal')">
  <div class="modal-box large" role="dialog" aria-modal="true" aria-labelledby="enlargeTitle">
    <div class="modal-head">
      <h3 id="enlargeTitle">Grafico</h3>
      <button class="modal-close" onclick="closeModal('enlargeModal')">Chiudi</button>
    </div>
    <div class="range-btns" id="rangeBtns" role="group" aria-label="Periodo">
      <button class="range-btn" data-range="1" aria-pressed="false" onclick="setEnlargeRange(1, this)">Oggi</button>
      <button class="range-btn" data-range="7" aria-pressed="false" onclick="setEnlargeRange(7, this)">Settimana</button>
      <button class="range-btn" data-range="30" aria-pressed="false" onclick="setEnlargeRange(30, this)">Mese</button>
      <button class="range-btn" data-range="0" aria-pressed="true" onclick="setEnlargeRange(0, this)">Tutto</button>
    </div>
    <canvas id="enlargeCanvas" height="70" role="img" aria-label="Grafico ingrandito"></canvas>
  </div>
</div>

<script>
Chart.defaults.color = '#8b93a9';
Chart.defaults.borderColor = '#2c2c2e';
Chart.defaults.font.size = 11;
Chart.defaults.plugins.legend.display = false;
const GENERATED_ISO = '{generated_iso}';
const X_TICKS = {{ maxRotation: 0, autoSkip: true, maxTicksLimit: 6 }};

/* Plugin: fasce colorate di sfondo (zone della Freschezza/TSB) */
const TSB_ZONES = [
  {{ from: -999, to: -20, color: 'rgba(245,101,101,0.10)' }},
  {{ from: -10, to: 5, color: 'rgba(104,211,145,0.08)' }},
  {{ from: 5, to: 999, color: 'rgba(99,179,237,0.10)' }}
];
try {{
  Chart.register({{
    id: 'zones',
    beforeDatasetsDraw(chart) {{
      // le opzioni passate all'hook non restano un array: le leggo dal grafico
      const zones = chart.options.plugins.zones;
      const y = chart.scales.y, area = chart.chartArea;
      if (!Array.isArray(zones) || !y || !area) return;
      const ctx = chart.ctx;
      ctx.save();
      zones.forEach(z => {{
        if (z.from >= y.max || z.to <= y.min) return;
        const top = y.getPixelForValue(Math.min(z.to, y.max));
        const bottom = y.getPixelForValue(Math.max(z.from, y.min));
        ctx.fillStyle = z.color;
        ctx.fillRect(area.left, top, area.right - area.left, bottom - top);
      }});
      ctx.restore();
    }}
  }});
}} catch (e) {{ console.error('Errore registrazione plugin zones', e); }}

/* Plugin: disegna il valore sopra ogni barra (stile Garmin) */
try {{
  const barValueLabelPlugin = {{
    id: 'barValueLabel',
    afterDatasetsDraw(chart) {{
      try {{
        const meta = chart.getDatasetMeta(0);
        const opts = chart.options && chart.options.plugins && chart.options.plugins.barValueLabel;
        const formatter = opts && opts.formatter;
        if (!formatter || !meta || !meta.data) return;
        const ctx = chart.ctx;
        ctx.save();
        ctx.fillStyle = '#e8eaf2';
        ctx.font = '600 10px -apple-system, sans-serif';
        ctx.textAlign = 'center';
        meta.data.forEach((bar, i) => {{
          const raw = chart.data.datasets[0].data[i];
          if (raw === null || raw === undefined) return;
          ctx.fillText(formatter(raw), bar.x, bar.y - 6);
        }});
        ctx.restore();
      }} catch (e) {{ console.error('barValueLabel draw error', e); }}
    }}
  }};
  Chart.register(barValueLabelPlugin);
}} catch (e) {{ console.error('Errore registrazione plugin barValueLabel', e); }}

function fmtPace(sec) {{
  return Math.floor(sec / 60) + ':' + String(Math.round(sec % 60)).padStart(2, '0');
}}

/* ---------- registro dati grezzi per i grafici ingrandibili ---------- */
const DASH_DATA = {{}};
let enlargeChart = null;
let currentEnlargeId = null;
let currentTs = null;

function registerChart(id, labels, datasets, type, title, hasToday, weekly) {{
  DASH_DATA[id] = {{ labels, datasets, type, title, hasToday: !!hasToday, weekly: !!weekly }};
}}

function showSection(name, btn) {{
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.getElementById('section-' + name).classList.add('active');
  document.querySelectorAll('.tabbtn').forEach(b => {{
    const on = b === btn;
    b.setAttribute('aria-selected', on ? 'true' : 'false');
    b.tabIndex = on ? 0 : -1;
  }});
}}

/* ---------- finestre: focus, Esc, tastiera ---------- */
const focusStack = [];
function openModal(id) {{
  const m = document.getElementById(id);
  if (!m.classList.contains('active')) focusStack.push(document.activeElement);
  m.classList.add('active');
  m.querySelector('.modal-close').focus();
}}
function closeModal(id) {{
  const m = document.getElementById(id);
  if (!m.classList.contains('active')) return;
  m.classList.remove('active');
  const back = focusStack.pop();
  if (back && back.focus) back.focus();
}}
function topModal() {{
  const open = [...document.querySelectorAll('.modal-overlay.active')];
  return open[open.length - 1] || null;
}}
document.addEventListener('keydown', (e) => {{
  const m = topModal();
  if (e.key === 'Escape' && m) {{ closeModal(m.id); return; }}
  if (e.key === 'Tab' && m) {{
    const f = [...m.querySelectorAll('button, summary, [tabindex="0"]')].filter(el => el.offsetParent !== null);
    if (!f.length) return;
    if (e.shiftKey && document.activeElement === f[0]) {{ e.preventDefault(); f[f.length - 1].focus(); }}
    else if (!e.shiftKey && document.activeElement === f[f.length - 1]) {{ e.preventDefault(); f[0].focus(); }}
    return;
  }}
  if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('[role="button"], tr.clickable')) {{
    e.preventDefault();
    e.target.click();
  }}
  if ((e.key === 'ArrowRight' || e.key === 'ArrowLeft') && e.target.matches('[role="tab"]')) {{
    const tabs = [...document.querySelectorAll('[role="tab"]')];
    const next = tabs[(tabs.indexOf(e.target) + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
    next.focus(); next.click();
  }}
}});

/* ---------- oggi e dati vecchi: calcolati al momento dell'apertura, non della generazione ---------- */
try {{
  const n = new Date();
  const todayIso = n.getFullYear() + '-' + String(n.getMonth() + 1).padStart(2, '0') + '-' + String(n.getDate()).padStart(2, '0');
  document.querySelectorAll('.day-box').forEach(b => {{
    if (b.dataset.date === todayIso) {{
      b.classList.add('today');
      b.setAttribute('aria-current', 'date');
    }}
  }});
}} catch (e) {{ console.error('Errore data di oggi', e); }}

/* ---------- grafico ingrandito con selettore Oggi/Settimana/Mese/Tutto ---------- */
function openEnlarge(id) {{
  const d = DASH_DATA[id];
  if (!d) {{ console.warn('Nessun dato registrato per il grafico:', id); return; }}
  currentEnlargeId = id;
  document.getElementById('rangeBtns').style.display = 'flex';
  document.getElementById('enlargeTitle').textContent = d.title;
  document.getElementById('enlargeCanvas').setAttribute('aria-label', 'Grafico ingrandito: ' + d.title);
  // su serie settimanali "Settimana"/"Mese" vorrebbero dire 7/30 settimane: resta solo "Tutto"
  document.querySelectorAll('#rangeBtns .range-btn').forEach(b => {{
    const r = b.dataset.range;
    const show = r === '0' || (!d.weekly && (r !== '1' || d.hasToday));
    b.style.display = show ? '' : 'none';
    b.setAttribute('aria-pressed', r === '0' ? 'true' : 'false');
  }});
  openModal('enlargeModal');
  drawEnlarge(0);
}}

function openActivityChartEnlarge(kind, title, color) {{
  if (!currentTs) return;
  document.getElementById('rangeBtns').style.display = 'none';
  document.getElementById('enlargeTitle').textContent = title;
  document.getElementById('enlargeCanvas').setAttribute('aria-label', 'Grafico ingrandito: ' + title);
  const labels = (currentTs.distance_km || currentTs.time_s || []).map(v => (v !== null && v !== undefined) ? (v.toFixed ? v.toFixed(2) : v) : '');
  const data = currentTs[kind] || [];
  const isPace = kind === 'pace_sec_km';
  if (enlargeChart) enlargeChart.destroy();
  enlargeChart = new Chart(document.getElementById('enlargeCanvas'), {{
    type: 'line',
    data: {{ labels, datasets: [{{ data, borderColor: color, backgroundColor: color + '33', fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2 }}] }},
    options: {{
      responsive: true, animation: {{ duration: 400 }},
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        tooltip: {{ enabled: true, mode: 'index', intersect: false, callbacks: isPace ? {{ label: (ctx) => fmtPace(ctx.raw) + '/km' }} : undefined }}
      }},
      scales: {{
        x: {{ title: {{ display: true, text: 'km' }}, grid: {{ display: false }} }},
        y: isPace ? {{ reverse: true, grid: {{ color: '#2c2c2e' }}, ticks: {{ callback: (v) => fmtPace(v) }} }} : {{ grid: {{ color: '#2c2c2e' }} }}
      }}
    }}
  }});
  openModal('enlargeModal');
}}

function setEnlargeRange(n, btn) {{
  document.querySelectorAll('#rangeBtns .range-btn').forEach(b => b.setAttribute('aria-pressed', b === btn ? 'true' : 'false'));
  drawEnlarge(n);
}}

function drawEnlarge(n) {{
  const d = DASH_DATA[currentEnlargeId];
  // slice(-0) restituirebbe tutto per caso: n=0 vuol dire "Tutto" in modo esplicito
  const cut = arr => n ? arr.slice(-n) : arr;
  const labels = cut(d.labels);
  const datasets = d.datasets.map(ds => ({{ ...ds, data: cut(ds.data) }}));
  if (enlargeChart) enlargeChart.destroy();
  enlargeChart = new Chart(document.getElementById('enlargeCanvas'), {{
    type: d.type,
    data: {{ labels, datasets }},
    options: {{
      responsive: true,
      animation: {{ duration: 500 }},
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{ tooltip: {{ enabled: true, mode: 'index', intersect: false }}, zones: currentEnlargeId === 'tsb' ? TSB_ZONES : null }},
      scales: {{ x: {{ ticks: {{ ...X_TICKS, maxTicksLimit: 10 }} }} }}
    }}
  }});
}}

/* ---------- sparkline mini nelle card in alto ---------- */
function sparkline(elemId, values, color, labels) {{
  if (!values || values.length === 0) return;
  try {{
    new Chart(document.getElementById(elemId), {{
      type: 'line',
      data: {{ labels: labels || values.map((_, i) => i), datasets: [{{ data: values, borderColor: color, backgroundColor: 'transparent', tension: 0.35, pointRadius: 0, borderWidth: 2 }}] }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        interaction: {{ mode: 'index', intersect: false }},
        plugins: {{ legend: {{ display: false }}, tooltip: {{ enabled: true, mode: 'index', intersect: false }} }},
        scales: {{ x: {{ display: false }}, y: {{ display: false }} }}
      }}
    }});
  }} catch (e) {{ console.error('Errore sparkline ' + elemId, e); }}
}}

/* ---------- dettaglio attività: percorso animato + 4 grafici ---------- */
const ACTIVITY_DETAILS = {activity_details_json};
const ACTIVITY_TIMESERIES = {activity_timeseries_json};
const ACTIVITY_SUMMARY = {activity_summary_json};

function flattenObj(obj, prefix) {{
  prefix = prefix || '';
  let rows = [];
  if (!obj || typeof obj !== 'object') return rows;
  for (const k in obj) {{
    const v = obj[k];
    if (v === null || v === undefined || v === '') continue;
    const key = prefix ? prefix + '.' + k : k;
    if (typeof v === 'object' && !Array.isArray(v)) {{
      rows = rows.concat(flattenObj(v, key));
    }} else if (Array.isArray(v)) {{
      if (v.length > 0 && typeof v[0] === 'object') {{
        rows.push([key, v.length + ' elementi (dati grezzi disponibili)']);
      }} else {{
        rows.push([key, v.join(', ')]);
      }}
    }} else {{
      rows.push([key, String(v)]);
    }}
  }}
  return rows;
}}

function buildRouteSvg(lat, lon) {{
  const W = 600, H = 220, PAD = 14;
  const latMin = Math.min(...lat), latMax = Math.max(...lat);
  const lonMin = Math.min(...lon), lonMax = Math.max(...lon);
  // un grado di longitudine è più corto di uno di latitudine: cos(lat) evita il percorso schiacciato
  const kx = Math.cos((latMin + latMax) / 2 * Math.PI / 180);
  const spanX = ((lonMax - lonMin) * kx) || 0.0001;
  const spanY = (latMax - latMin) || 0.0001;
  const scale = Math.min((W - 2 * PAD) / spanX, (H - 2 * PAD) / spanY);
  const offX = (W - spanX * scale) / 2, offY = (H - spanY * scale) / 2;
  const pts = lat.map((la, i) => {{
    const x = offX + (lon[i] - lonMin) * kx * scale;
    const y = offY + (latMax - la) * scale;
    return [x, y];
  }});
  const d = pts.map((p, i) => (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
  return {{ svg: `<svg viewBox="0 0 ${{W}} ${{H}}" xmlns="http://www.w3.org/2000/svg">
      <path id="routePath" d="${{d}}" fill="none" stroke="#1c2650" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
      <path id="routePathAnim" d="${{d}}" fill="none" stroke="#4fd1c5" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
      <circle id="routeDot" r="6" fill="#f6ad55"/>
    </svg>`, pts }};
}}

function animateRoute() {{
  const path = document.getElementById('routePathAnim');
  const dot = document.getElementById('routeDot');
  if (!path || !dot) return;
  const len = path.getTotalLength();
  path.style.transition = 'none';
  path.style.strokeDasharray = len;
  path.style.strokeDashoffset = len;
  void path.getBoundingClientRect();
  const duration = 2500;
  const start = performance.now();
  function frame(now) {{
    const t = Math.min(1, (now - start) / duration);
    const eased = t;
    path.style.strokeDashoffset = len * (1 - eased);
    const p = path.getPointAtLength(len * eased);
    dot.setAttribute('cx', p.x);
    dot.setAttribute('cy', p.y);
    if (t < 1) requestAnimationFrame(frame);
  }}
  requestAnimationFrame(frame);
}}

function openDetail(activityId, title) {{
  const data = ACTIVITY_DETAILS[activityId];
  const ts = ACTIVITY_TIMESERIES[activityId];
  document.getElementById('detailTitle').textContent = title;

  const body = document.getElementById('detailBody');
  body.innerHTML = '';
  currentTs = ts || null;

  if (ts && ts.lat && ts.lat.length > 5) {{
    const wrap = document.createElement('div');
    wrap.className = 'route-wrap';
    const {{ svg }} = buildRouteSvg(ts.lat, ts.lon);
    wrap.innerHTML = svg;
    body.appendChild(wrap);

    const replay = document.createElement('button');
    replay.className = 'replay-btn';
    replay.textContent = 'Rivedi animazione del percorso';
    replay.onclick = animateRoute;
    body.appendChild(replay);
  }} else {{
    const msg = document.createElement('div');
    msg.className = 'missing';
    msg.textContent = 'Nessuna traccia GPS disponibile per questa attività.';
    body.appendChild(msg);
  }}

  const summary = ACTIVITY_SUMMARY[activityId];
  if (summary) {{
    const statsWrap = document.createElement('div');
    statsWrap.className = 'detail-stats';
    const labels = {{
      distanza: 'Distanza', durata: 'Durata', passo_medio: 'Passo medio',
      fc_media: 'FC media', fc_max: 'FC max', dislivello: 'Dislivello',
      cadenza_media: 'Cadenza (passi/min)', calorie: 'Calorie'
    }};
    Object.keys(labels).forEach(k => {{
      if (summary[k] && summary[k] !== '—') {{
        const box = document.createElement('div');
        box.className = 'dstat';
        box.innerHTML = `<div class="v">${{summary[k]}}</div><div class="l">${{labels[k]}}</div>`;
        statsWrap.appendChild(box);
      }}
    }});
    body.appendChild(statsWrap);
  }}

  const grid = document.createElement('div');
  grid.className = 'mini-grid';

  function addMini(title, canvasId, onClickFn, extraHtml) {{
    const p = document.createElement('div');
    p.className = 'mini-panel';
    p.setAttribute('role', 'button');
    p.tabIndex = 0;
    p.setAttribute('aria-label', 'Ingrandisci il grafico ' + title);
    p.innerHTML = `<h4>${{title}}</h4>${{extraHtml || ''}}<canvas id="${{canvasId}}" aria-hidden="true"></canvas>`;
    p.onclick = onClickFn;
    grid.appendChild(p);
  }}

  if (ts && ts.pace_sec_km && ts.pace_sec_km.some(v => v !== null)) {{
    const validPaces = ts.pace_sec_km.filter(v => v !== null);
    const media = summary && summary.passo_medio !== '—' ? summary.passo_medio : fmtPace(validPaces.reduce((a,b)=>a+b,0)/validPaces.length) + '/km';
    const migliore = fmtPace(Math.min(...validPaces)) + '/km';
    addMini('Passo', 'detailPace', () => openActivityChartEnlarge('pace_sec_km', 'Passo', '#4fd1c5'),
      `<div class="mini-stat-row"><div class="ms">Media<strong>${{media}}</strong></div><div class="ms">Migliore<strong>${{migliore}}</strong></div></div>`);
  }}
  if (ts && ts.hr && ts.hr.some(v => v !== null)) addMini('Battiti (bpm)', 'detailHr', () => openActivityChartEnlarge('hr', 'Battiti (bpm)', '#f56565'));
  if (ts && ts.cadence && ts.cadence.some(v => v !== null)) addMini('Cadenza (passi al minuto)', 'detailCadence', () => openActivityChartEnlarge('cadence', 'Cadenza (passi al minuto)', '#b794f4'));
  if (ts && ts.elevation && ts.elevation.some(v => v !== null)) addMini('Dislivello (m)', 'detailElevation', () => openActivityChartEnlarge('elevation', 'Dislivello (m)', '#f6ad55'));

  body.appendChild(grid);

  const rawTbody = document.getElementById('detailRawTable');
  rawTbody.innerHTML = '';
  if (!data) {{
    rawTbody.innerHTML = '<tr><td colspan="2">Nessun dettaglio disponibile.</td></tr>';
  }} else {{
    flattenObj(data).forEach(([k, v]) => {{
      const tr = document.createElement('tr');
      tr.innerHTML = '<td>' + k + '</td><td></td>';
      tr.children[1].textContent = v;
      rawTbody.appendChild(tr);
    }});
  }}

  document.querySelector('#detailModal details.raw-toggle').open = false;
  openModal('detailModal');

  setTimeout(() => {{
    if (!ts) return;
    if (ts.lat && ts.lat.length > 5) animateRoute();
    const xLabels = (ts.distance_km || ts.time_s || []).map(v => (v !== null ? v.toFixed ? v.toFixed(2) : v : ''));
    const hoverOpts = {{ interaction: {{ mode: 'index', intersect: false }}, plugins: {{ legend: {{ display: false }}, tooltip: {{ enabled: true, mode: 'index', intersect: false }} }} }};
    try {{
      if (ts.hr && ts.hr.some(v => v !== null)) {{
        new Chart(document.getElementById('detailHr'), {{
          type: 'line',
          data: {{ labels: xLabels, datasets: [{{ data: ts.hr, borderColor: '#f56565', backgroundColor: '#f5656522', fill: true, tension: 0.25, pointRadius: 0 }}] }},
          options: {{ responsive: true, animation: {{ duration: 900 }}, ...hoverOpts, scales: {{ x: {{ display: false }}, y: {{ grid: {{ color: '#2c2c2e' }} }} }} }}
        }});
      }}
    }} catch(e) {{ console.error('Errore grafico detailHr', e); }}
    try {{
      if (ts.pace_sec_km && ts.pace_sec_km.some(v => v !== null)) {{
        new Chart(document.getElementById('detailPace'), {{
          type: 'line',
          data: {{ labels: xLabels, datasets: [{{ data: ts.pace_sec_km, borderColor: '#4fd1c5', backgroundColor: '#4fd1c522', fill: true, tension: 0.3, pointRadius: 0, spanGaps: true }}] }},
          options: {{
            responsive: true, animation: {{ duration: 900 }},
            interaction: {{ mode: 'index', intersect: false }},
            plugins: {{ legend: {{ display: false }}, tooltip: {{ enabled: true, mode: 'index', intersect: false, callbacks: {{ label: (ctx) => fmtPace(ctx.raw) + '/km' }} }} }},
            scales: {{ x: {{ display: false }}, y: {{ reverse: true, grid: {{ color: '#2c2c2e' }}, ticks: {{ callback: (v) => fmtPace(v) }} }} }}
          }}
        }});
      }}
    }} catch(e) {{ console.error('Errore grafico detailPace', e); }}
    try {{
      if (ts.cadence && ts.cadence.some(v => v !== null)) {{
        new Chart(document.getElementById('detailCadence'), {{
          type: 'line',
          data: {{ labels: xLabels, datasets: [{{ data: ts.cadence, borderColor: '#b794f4', backgroundColor: '#b794f422', fill: true, tension: 0.25, pointRadius: 0 }}] }},
          options: {{ responsive: true, animation: {{ duration: 900 }}, ...hoverOpts, scales: {{ x: {{ display: false }}, y: {{ grid: {{ color: '#2c2c2e' }} }} }} }}
        }});
      }}
    }} catch(e) {{ console.error('Errore grafico detailCadence', e); }}
    try {{
      if (ts.elevation && ts.elevation.some(v => v !== null)) {{
        new Chart(document.getElementById('detailElevation'), {{
          type: 'line',
          data: {{ labels: xLabels, datasets: [{{ data: ts.elevation, borderColor: '#f6ad55', backgroundColor: 'rgba(246,173,85,0.15)', fill: true, tension: 0.25, pointRadius: 0 }}] }},
          options: {{ responsive: true, animation: {{ duration: 900 }}, ...hoverOpts, scales: {{ x: {{ display: false }}, y: {{ grid: {{ color: '#2c2c2e' }} }} }} }}
        }});
      }}
    }} catch(e) {{ console.error('Errore grafico detailElevation', e); }}
  }}, 30);
}}

{chart_scripts}
</script>
</body>
</html>
"""


def render_or_missing(has_data, chart_html, missing_msg):
    return chart_html if has_data else f'<div class="missing">{missing_msg}</div>'


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

def main():
    print("== Dashboard Allenamento — avvio ==")
    chartjs_source = get_chartjs_source()
    client = get_client()
    print("Login OK (token da cache).")

    activities = fetch_activities(client, LOAD_LOOKBACK_DAYS)
    running_activities = [a for a in activities if is_running(a)]
    print(f"  -> di cui {len(running_activities)} corse")

    rhr_series, sleep_series, hrv_series = fetch_daily_wellness(client, RECOVERY_LOOKBACK_DAYS)
    vo2_series = fetch_vo2max(client, WEEKS_TREND * 7, VO2_SAMPLE_EVERY_DAYS)

    load_series = compute_ctl_atl_tsb(activities, LOAD_LOOKBACK_DAYS)
    load_series_display = load_series[-WEEKS_TREND * 7:]

    mileage = compute_weekly_mileage(running_activities, WEEKS_TREND)

    pb_5k_pace_sec_km = ATHLETE["pb_5k_sec"] / 5.0
    recent_rows = build_recent_table(activities, ACTIVITY_TABLE_WEEKS, pb_5k_pace_sec_km)
    week_row = build_week_row(running_activities, pb_5k_pace_sec_km)

    today = date.today()
    days_left = (RACE_DATE - today).days
    weeks_left = round(days_left / 7, 1)
    predicted_sec = predict_10k_from_5k(ATHLETE["pb_5k_sec"])
    predicted_10k = sec_to_mmss(predicted_sec)
    insights = compute_insights(load_series, mileage, vo2_series, days_left)

    recent_activities_raw = [a for a in activities if str(a.get("activityId")) in {r["activity_id"] for r in recent_rows}]
    activity_details, activity_timeseries = fetch_activity_details(client, recent_activities_raw)
    activity_summary = {str(a.get("activityId")): build_activity_summary(activity_details.get(str(a.get("activityId")), a)) for a in recent_activities_raw}
    activity_details = {aid: flatten_for_json(d) for aid, d in activity_details.items()}

    card_ctl = f"{load_series[-1]['ctl']}" if load_series else "—"
    card_atl = f"{load_series[-1]['atl']}" if load_series else "—"
    card_vo2 = f"{vo2_series[-1]['value']}" if vo2_series else "—"
    card_rhr = f"{rhr_series[-1]['value']} bpm" if rhr_series else "—"
    card_hrv = f"{hrv_series[-1]['value']} ms" if hrv_series else "—"

    def delta_html(d, up_is_good):
        """Variazione a 7 giorni sotto il numero; up_is_good=None per una metrica neutra (fatica)."""
        if d is None:
            return '<div class="delta">&nbsp;</div>'
        cls = "" if up_is_good is None or d == 0 else ("good" if (d > 0) == up_is_good else "bad")
        return f'<div class="delta {cls}">{d:+g} in 7 giorni</div>'

    chart_scripts = []
    x_ticks = "ticks: X_TICKS"

    def line_chart(elem_id, labels, dataset_label, values, color, zones=False):
        return f"""
try {{
new Chart(document.getElementById('{elem_id}'), {{
  type: 'line',
  data: {{ labels: {json.dumps(labels)}, datasets: [{{ label: '{dataset_label}', data: {json.dumps(values)}, borderColor: '{color}', backgroundColor: '{color}22', fill: {'false' if zones else 'true'}, tension: 0.3, pointRadius: 0, borderWidth: 2 }}] }},
  options: {{
    responsive: true,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{ tooltip: {{ enabled: true, mode: 'index', intersect: false }}, zones: {'TSB_ZONES' if zones else 'null'} }},
    scales: {{ x: {{ grid: {{ display: false }}, {x_ticks} }}, y: {{ grid: {{ color: '#2c2c2e' }} }} }}
  }}
}});
}} catch(e) {{ console.error('Errore grafico {elem_id}', e); }}"""

    has_load = any(p["ctl"] or p["atl"] for p in load_series_display)
    if has_load:
        labels_full = [fmt_day(p['date']) for p in load_series]
        labels = [fmt_day(p['date']) for p in load_series_display]
        spark_labels = json.dumps(labels_full[-SPARKLINE_DAYS:])
        chart_scripts.append(line_chart('ctlChart', labels, 'Forma fisica', [p['ctl'] for p in load_series_display], '#4fd1c5'))
        chart_scripts.append(line_chart('atlChart', labels, 'Fatica', [p['atl'] for p in load_series_display], '#f56565'))
        chart_scripts.append(line_chart('tsbChart', labels, 'Freschezza', [p['tsb'] for p in load_series_display], '#f6ad55', zones=True))
        chart_scripts.append(f"sparkline('sparkCtl', {json.dumps([p['ctl'] for p in load_series[-SPARKLINE_DAYS:]])}, '#4fd1c5', {spark_labels});")
        chart_scripts.append(f"sparkline('sparkAtl', {json.dumps([p['atl'] for p in load_series[-SPARKLINE_DAYS:]])}, '#f56565', {spark_labels});")
        load_has_today = load_series[-1]["date"] == today.isoformat()
        chart_scripts.append(f"try {{ registerChart('ctl', {json.dumps(labels_full)}, [{{label:'Forma fisica', data:{json.dumps([p['ctl'] for p in load_series])}, borderColor:'#4fd1c5', backgroundColor:'transparent', tension:0.3, pointRadius:0}}], 'line', 'Forma fisica (CTL)', {json.dumps(load_has_today)}); }} catch(e) {{ console.error('registerChart ctl', e); }}")
        chart_scripts.append(f"try {{ registerChart('atl', {json.dumps(labels_full)}, [{{label:'Fatica', data:{json.dumps([p['atl'] for p in load_series])}, borderColor:'#f56565', backgroundColor:'transparent', tension:0.3, pointRadius:0}}], 'line', 'Fatica (ATL)', {json.dumps(load_has_today)}); }} catch(e) {{ console.error('registerChart atl', e); }}")
        chart_scripts.append(f"try {{ registerChart('tsb', {json.dumps(labels_full)}, [{{label:'Freschezza', data:{json.dumps([p['tsb'] for p in load_series])}, borderColor:'#f6ad55', backgroundColor:'transparent', tension:0.3, pointRadius:0}}], 'line', 'Freschezza (TSB)', {json.dumps(load_has_today)}); }} catch(e) {{ console.error('registerChart tsb', e); }}")

    if vo2_series:
        vo2_labels = [fmt_day(p['date']) for p in vo2_series]
        chart_scripts.append(line_chart('vo2Chart', vo2_labels, 'VO2max', [p['value'] for p in vo2_series], '#68d391'))
        chart_scripts.append(f"sparkline('sparkVo2', {json.dumps([p['value'] for p in vo2_series])}, '#68d391', {json.dumps(vo2_labels)});")
        vo2_has_today = vo2_series[-1]["date"] == today.isoformat()
        chart_scripts.append(f"try {{ registerChart('vo2', {json.dumps(vo2_labels)}, [{{label:'VO2max', data:{json.dumps([p['value'] for p in vo2_series])}, borderColor:'#68d391', backgroundColor:'transparent', tension:0.3, pointRadius:0}}], 'line', 'VO2 max', {json.dumps(vo2_has_today)}); }} catch(e) {{ console.error('registerChart vo2', e); }}")

    if rhr_series:
        rhr_labels = [fmt_day(p['date']) for p in rhr_series]
        chart_scripts.append(line_chart('rhrChart', rhr_labels, 'FC a riposo', [p['value'] for p in rhr_series], '#f687b3'))
        chart_scripts.append(f"sparkline('sparkRhr', {json.dumps([p['value'] for p in rhr_series[-SPARKLINE_DAYS:]])}, '#f687b3', {json.dumps(rhr_labels[-SPARKLINE_DAYS:])});")
        rhr_has_today = rhr_series[-1]["date"] == today.isoformat()
        chart_scripts.append(f"try {{ registerChart('rhr', {json.dumps(rhr_labels)}, [{{label:'FC a riposo', data:{json.dumps([p['value'] for p in rhr_series])}, borderColor:'#f687b3', backgroundColor:'transparent', tension:0.3, pointRadius:0}}], 'line', 'FC a riposo', {json.dumps(rhr_has_today)}); }} catch(e) {{ console.error('registerChart rhr', e); }}")

    if hrv_series:
        hrv_labels = [fmt_day(p['date']) for p in hrv_series]
        chart_scripts.append(line_chart('hrvChart', hrv_labels, 'HRV', [p['value'] for p in hrv_series], '#b794f4'))
        chart_scripts.append(f"sparkline('sparkHrv', {json.dumps([p['value'] for p in hrv_series[-SPARKLINE_DAYS:]])}, '#b794f4', {json.dumps(hrv_labels[-SPARKLINE_DAYS:])});")
        hrv_has_today = hrv_series[-1]["date"] == today.isoformat()
        chart_scripts.append(f"try {{ registerChart('hrv', {json.dumps(hrv_labels)}, [{{label:'HRV', data:{json.dumps([p['value'] for p in hrv_series])}, borderColor:'#b794f4', backgroundColor:'transparent', tension:0.3, pointRadius:0}}], 'line', 'Variabilità cardiaca (HRV)', {json.dumps(hrv_has_today)}); }} catch(e) {{ console.error('registerChart hrv', e); }}")

    has_mileage = len(mileage["values"]) > 0
    if has_mileage:
        chart_scripts.append(f"""
try {{
new Chart(document.getElementById('mileageChart'), {{
  type: 'bar',
  data: {{ labels: {json.dumps(mileage['labels'])}, datasets: [
    {{ label: 'km settimanali', data: {json.dumps(mileage['values'])}, backgroundColor: '#4fd1c5', borderRadius: 5 }},
    {{ label: 'tendenza', data: {json.dumps(mileage['trend'])}, type: 'line', borderColor: '#f6ad55', backgroundColor: 'transparent', pointRadius: 0 }}
  ] }},
  options: {{ responsive: true, interaction: {{ mode: 'index', intersect: false }}, plugins: {{ legend: {{ display: true }}, tooltip: {{ enabled: true, mode: 'index', intersect: false }} }}, scales: {{ x: {{ grid: {{ display: false }}, {x_ticks} }}, y: {{ grid: {{ color: '#2c2c2e' }} }} }} }}
}});
}} catch(e) {{ console.error('Errore grafico mileageChart', e); }}""")
        chart_scripts.append(f"try {{ registerChart('mileage', {json.dumps(mileage['labels'])}, [{{label:'km settimanali', data:{json.dumps(mileage['values'])}, backgroundColor:'#4fd1c5'}}], 'bar', 'Chilometraggio settimanale', false, true); }} catch(e) {{ console.error('registerChart mileage', e); }}")

    if sleep_series:
        sleep_has_today = sleep_series[-1]["date"] == today.isoformat()
        sleep_labels = [fmt_day(p['date']) for p in sleep_series]
        chart_scripts.append(f"""
try {{
new Chart(document.getElementById('sleepChart'), {{
  type: 'bar',
  data: {{ labels: {json.dumps(sleep_labels)}, datasets: [{{ label: 'Ore di sonno', data: {json.dumps([p['hours'] for p in sleep_series])}, backgroundColor: '#63b3ed', borderRadius: 5 }}] }},
  options: {{ responsive: true, interaction: {{ mode: 'index', intersect: false }}, plugins: {{ tooltip: {{ enabled: true, mode: 'index', intersect: false }} }}, scales: {{ x: {{ grid: {{ display: false }}, {x_ticks} }}, y: {{ grid: {{ color: '#2c2c2e' }} }} }} }}
}});
}} catch(e) {{ console.error('Errore grafico sleepChart', e); }}""")
        chart_scripts.append(f"try {{ registerChart('sleep', {json.dumps(sleep_labels)}, [{{label:'Ore di sonno', data:{json.dumps([p['hours'] for p in sleep_series])}, backgroundColor:'#63b3ed'}}], 'bar', 'Sonno', {json.dumps(sleep_has_today)}); }} catch(e) {{ console.error('registerChart sleep', e); }}")

    type_labels = {"easy": "Facile", "workout": "Allenamento"}
    if recent_rows:
        def _row_html(r):
            dist_cell = f"{r['distance_km']} km" if r['distance_km'] else "—"
            safe_title = js_escape(f"{r['name']} · {r['date']}")
            badge = f"<span class='badge {r['type']}'>{type_labels[r['type']]}</span>" if r['type'] in type_labels else "<span class='muted'>—</span>"
            return (
                f"<tr class='clickable' tabindex='0' onclick=\"openDetail('{r['activity_id']}', '{safe_title}')\">"
                f"<td>{r['date']}</td><td class='hide-sm'>{r['sport']}</td><td>{r['name']}</td>"
                f"<td>{dist_cell}</td><td>{r['duration'] or '—'}</td>"
                f"<td>{r['pace'] or '—'}</td><td>{r['avg_hr'] or '—'}</td><td class='hide-sm'>{r['max_hr'] or '—'}</td>"
                f"<td>{badge}</td></tr>"
            )
        rows_html = "\n".join(_row_html(r) for r in recent_rows)
        table_html = f"""<div class="table-wrap"><table>
<thead><tr><th>Data</th><th class="hide-sm">Sport</th><th>Nome</th><th>Distanza</th><th>Durata</th><th>Passo</th><th>FC media</th><th class="hide-sm">FC max</th><th>Tipo</th></tr></thead>
<tbody>{rows_html}</tbody></table></div>"""
    else:
        table_html = None

    if insights:
        insights_html = "\n".join(f'<div class="insight {i["color"]}"><h2>{i["title"]}</h2><p>{i["text"]}</p></div>' for i in insights)
    else:
        insights_html = '<div class="insight info"><h2>Dati insufficienti</h2><p>Servono più allenamenti registrati per generare consigli automatici.</p></div>'

    kind_labels = {"easy": "facile", "workout": "allenamento", "rest": "riposo"}
    week_row_html = "\n".join(
        f'<div class="day-box {d["kind"]}" data-date="{d["date"]}" aria-label="{d["label"]}: {str(d["km"]) + " km, " if d["km"] else ""}{kind_labels[d["kind"]]}">'
        f'<div class="d">{d["label"]}</div><div class="km">{d["km"] if d["km"] else "—"}</div></div>'
        for d in week_row
    )

    def canvas(elem_id, label):
        return f'<canvas id="{elem_id}" role="img" aria-label="{label}"></canvas>'

    def last(series, key="value"):
        return series[-1][key] if series else "—"

    ctx = {
        "race_name": RACE_NAME, "race_date_str": RACE_DATE.strftime("%d/%m/%Y"),
        "generated_at": datetime.now().strftime("%d/%m/%Y alle %H:%M"),
        "generated_iso": today.isoformat(),
        "days_left": days_left, "weeks_left": weeks_left, "predicted_10k": predicted_10k,
        "predicted_pace": sec_to_pace_str(predicted_sec / 10.0),
        "weeks_trend": WEEKS_TREND, "recovery_weeks": RECOVERY_LOOKBACK_DAYS // 7, "table_weeks": ACTIVITY_TABLE_WEEKS,
        "card_ctl": card_ctl, "card_atl": card_atl, "card_vo2": card_vo2, "card_rhr": card_rhr, "card_hrv": card_hrv,
        "delta_ctl": delta_html(delta_7d(load_series, "ctl"), True),
        "delta_atl": delta_html(delta_7d(load_series, "atl"), None),
        "delta_vo2": delta_html(delta_7d(vo2_series), True),
        "delta_rhr": delta_html(delta_7d(rhr_series), False),
        "delta_hrv": delta_html(delta_7d(hrv_series), True),
        "insights_html": insights_html, "week_row_html": week_row_html,
        "ctl_chart_or_missing": render_or_missing(has_load, canvas("ctlChart", f"Grafico forma fisica, ultimo valore {card_ctl}"), "Dati insufficienti."),
        "atl_chart_or_missing": render_or_missing(has_load, canvas("atlChart", f"Grafico fatica, ultimo valore {card_atl}"), "Dati insufficienti."),
        "tsb_chart_or_missing": render_or_missing(has_load, canvas("tsbChart", f"Grafico freschezza, ultimo valore {last(load_series, 'tsb')}"), "Dati insufficienti."),
        "mileage_chart_or_missing": render_or_missing(has_mileage, canvas("mileageChart", f"Chilometri per settimana, ultima settimana {mileage['values'][-1] if has_mileage else 0} km"), "Nessuna corsa trovata nel periodo."),
        "vo2_chart_or_missing": render_or_missing(len(vo2_series) > 0, canvas("vo2Chart", f"Grafico VO2 max, ultimo valore {card_vo2}"), "Il tuo account non restituisce dati VO2max."),
        "hrv_chart_or_missing": render_or_missing(len(hrv_series) > 0, canvas("hrvChart", f"Grafico HRV, ultimo valore {card_hrv}"), "Nessun dato HRV trovato."),
        "rhr_chart_or_missing": render_or_missing(len(rhr_series) > 0, canvas("rhrChart", f"Grafico FC a riposo, ultimo valore {card_rhr}"), "Nessun dato di FC a riposo trovato."),
        "sleep_chart_or_missing": render_or_missing(len(sleep_series) > 0, canvas("sleepChart", f"Ore di sonno, ultima notte {last(sleep_series, 'hours')} ore"), "Nessun dato di sonno trovato."),
        "table_or_missing": table_html or '<div class="missing">Nessuna corsa nelle ultime settimane.</div>',
        "chart_scripts": "\n".join(chart_scripts),
        "chartjs_source": chartjs_source,
        "activity_details_json": json_for_script(activity_details),
        "activity_timeseries_json": json_for_script(activity_timeseries),
        "activity_summary_json": json_for_script(activity_summary),
    }

    html = HTML_TEMPLATE.format(**ctx)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n✅ Fatto. Pagina generata in: {OUTPUT_HTML}")
    print("   Apri il file nel browser (doppio click).")


if __name__ == "__main__":
    main()
