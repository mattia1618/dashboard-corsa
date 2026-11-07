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
from html import escape

# ----------------------------------------------------------------------------
# CONFIGURAZIONE
# ----------------------------------------------------------------------------
# usati solo se nel calendario Garmin Connect non c'è una gara futura
RACE_DATE = date(2026, 11, 8)
RACE_NAME = "10 km"
RACE_KM = 10

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
CALENDAR_WEEKS_BACK = 11      # + la settimana in corso = 12 settimane di storico
CALENDAR_WEEKS_AHEAD_MAX = 16  # oltre non mostro settimane vuote fino a una gara lontana
RACE_LOOKAHEAD_MONTHS = 12

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


def sport_group(a):
    key = activity_type_key(a)
    if key in RUNNING_TYPE_KEYS:
        return "run"
    if "cycl" in key or "biking" in key:
        return "bike"
    if "swim" in key:
        return "swim"
    if key in {"strength_training", "fitness_equipment"}:
        return "strength"
    return "other"


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
    rhr_series, sleep_series, hrv_series, stress_series = [], [], [], []
    found = {"rhr": 0, "sleep": 0, "hrv": 0}

    for d in daterange(start, today):
        ds = d.isoformat()
        stats = safe(client.get_stats, ds)
        rhr = stats.get("restingHeartRate") if stats else None
        if rhr:
            rhr_series.append({"date": ds, "value": rhr})
            found["rhr"] += 1
        stress = stats.get("averageStressLevel") if stats else None
        if stress and stress > 0:  # Garmin usa -1/-2 per "non misurato"
            stress_series.append({"date": ds, "value": stress})

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

    print(f"     RHR: {found['rhr']} giorni | Sonno: {found['sleep']} giorni | HRV: {found['hrv']} giorni | Stress: {len(stress_series)} giorni")
    return rhr_series, sleep_series, hrv_series, stress_series


def fetch_stress_day(client):
    """Stress ogni 3 minuti di oggi (o di ieri se oggi non c'è ancora niente), come il grafico Garmin."""
    for d in (date.today(), date.today() - timedelta(days=1)):
        raw = safe(client.get_stress_data, d.isoformat()) or {}
        points = [p for p in (raw.get("stressValuesArray") or []) if p and len(p) >= 2]
        if any(p[1] is not None and p[1] >= 0 for p in points):
            return {
                "date": d.isoformat(),
                "labels": [datetime.fromtimestamp(p[0] / 1000).strftime("%H:%M") for p in points],
                # -1/-2 = attività o non misurato: buco nel grafico
                "values": [p[1] if p[1] is not None and p[1] >= 0 else None for p in points],
                "avg": raw.get("avgStressLevel"),
            }
    return None


def fetch_training_load(client):
    """Carico acuto e focus carico ufficiali Garmin (dallo stato di allenamento)."""
    raw = safe(client.get_training_status, date.today().isoformat()) or {}
    status = next(iter(((raw.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}).values()), {}) or {}
    acute = status.get("acuteTrainingLoadDTO") or {}
    balance = next(iter(((raw.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap") or {}).values()), {}) or {}
    if not acute.get("dailyTrainingLoadAcute") and not balance:
        return None
    focus = [
        {"label": label, "value": round(balance[f"monthlyLoad{key}"]), "min": balance.get(f"monthlyLoad{key}TargetMin"), "max": balance.get(f"monthlyLoad{key}TargetMax"), "color": color}
        for key, label, color in (("AerobicLow", "Aerobico basso", "#4fd1c5"), ("AerobicHigh", "Aerobico alto", "#f6ad55"), ("Anaerobic", "Anaerobico", "#b794f4"))
        if balance.get(f"monthlyLoad{key}") is not None
    ]
    return {
        "acute": acute.get("dailyTrainingLoadAcute"),
        "min": acute.get("minTrainingLoadChronic"),
        "max": acute.get("maxTrainingLoadChronic"),
        "ratio": acute.get("dailyAcuteChronicWorkloadRatio"),
        "status": acute.get("acwrStatus"),
        "focus": focus,
    }


def build_training_load_html(tl):
    """Card stile Garmin: carico acuto sulla fascia ottimale, sotto il focus carico delle ultime 4 settimane."""
    if not tl:
        return '<div class="missing">Garmin non ha restituito il carico di allenamento.</div>'
    parts = []
    if tl["acute"] is not None and tl["min"] and tl["max"]:
        top = max(tl["max"] * 1.3, tl["acute"] * 1.1)
        pct = lambda v: f"{min(v / top * 100, 100):.1f}%"
        label, color = {"OPTIMAL": ("Ottimale", "#68d391"), "LOW": ("Basso", "#63b3ed"), "HIGH": ("Alto", "#f6ad55"), "VERY_HIGH": ("Molto alto", "#f56565")}.get(tl["status"], ("", "#a0a0a5"))
        ratio = f" · rapporto acuto/cronico {tl['ratio']:g}" if tl["ratio"] is not None else ""
        parts.append(
            f'<div class="tl-head"><span class="tl-num">{round(tl["acute"])}</span><span style="color:{color}">{label}</span></div>'
            f'<div class="tl-bar" role="img" aria-label="Carico acuto {round(tl["acute"])}, fascia ottimale {round(tl["min"])}-{round(tl["max"])}">'
            f'<div class="tl-zone" style="left:{pct(tl["min"])};width:calc({pct(tl["max"])} - {pct(tl["min"])})"></div>'
            f'<div class="tl-dot" style="left:{pct(tl["acute"])}"></div></div>'
            f'<div class="tl-scale"><span style="left:{pct(tl["min"])}">{round(tl["min"])}</span><span style="left:{pct(tl["max"])}">{round(tl["max"])}</span></div>'
            f'<div class="tl-note">Carico acuto (ultimi 7 giorni){ratio}. La fascia verde è la zona ottimale.</div>'
        )
    if tl["focus"]:
        top = max(max(f["value"], f["max"] or 0) for f in tl["focus"]) * 1.15
        pct = lambda v: f"{v / top * 100:.1f}%"
        rows, short = [], []
        for f in tl["focus"]:
            zone = f'<div class="tl-target" style="left:{pct(f["min"])};width:calc({pct(f["max"])} - {pct(f["min"])})"></div>' if f["min"] is not None and f["max"] is not None else ""
            rows.append(f'<div class="tl-frow"><div class="tl-flabel"><span>{f["label"]}</span><span>{f["value"]}</span></div>'
                        f'<div class="tl-fbar"><div style="width:{pct(f["value"])};background:{f["color"]}"></div>{zone}</div></div>')
            if f["min"] is not None and f["value"] < f["min"]:
                short.append(f["label"].lower())
        verdict = (f'<div class="tl-note warn">Carenza: {", ".join(short)}.</div>' if short
                   else '<div class="tl-note">Carico ben distribuito.</div>')
        parts.append(f'<h3 class="tl-sub">Focus carico · ultime 4 settimane</h3>{"".join(rows)}'
                     f'<div class="tl-note">Il riquadro tratteggiato è il range consigliato da Garmin.</div>{verdict}')
    return "".join(parts)


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

# ----------------------------------------------------------------------------
# FETCH — profilo, record, previsioni Garmin e gare (scheda "Io" e calendario)
# ----------------------------------------------------------------------------

PR_LABELS = {1: "1 km", 2: "1 miglio", 3: "5 km", 4: "10 km", 5: "Mezza maratona", 6: "Maratona", 7: "Corsa più lunga"}
PREDICTION_KEYS = {"5": "time5K", "10": "time10K", "21.0975": "timeHalfMarathon", "42.195": "timeMarathon"}


def fetch_profile(client):
    data = (safe(client.get_user_profile) or {}).get("userData") or {}
    age = None
    try:
        born = date.fromisoformat(data["birthDate"])
        today = date.today()
        age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    except Exception:
        pass
    records = []
    for r in sorted(safe(client.get_personal_record) or [], key=lambda r: r.get("typeId") or 0):
        label, value = PR_LABELS.get(r.get("typeId")), r.get("value")
        if not label or not value:
            continue
        when = (r.get("actStartDateTimeInGMTFormatted") or "")[:10]
        is_distance = r["typeId"] == 7  # "corsa più lunga" è in metri, gli altri in secondi
        records.append({
            "label": label,
            "value": f"{value / 1000:.2f} km" if is_distance else sec_to_hms(value),
            "seconds": None if is_distance else value,
            "date": f"{fmt_day(when)}/{when[:4]}" if when else "",
            "activity": r.get("activityName") or "",
        })
    return {
        "name": safe(client.get_full_name) or "",
        "age": age,
        "height_cm": round(data["height"]) if data.get("height") else ATHLETE["height_cm"],
        "weight_kg": round(data["weight"] / 1000, 1) if data.get("weight") else ATHLETE["weight_kg"],
        "vo2max": data.get("vo2MaxRunning"),
        "lt_hr": data.get("lactateThresholdHeartRate"),
        "records": records,
    }


def fetch_race_predictions(client):
    raw = safe(client.get_race_predictions) or {}
    if isinstance(raw, list):
        raw = raw[-1] if raw else {}
    return {km: raw[key] for km, key in PREDICTION_KEYS.items() if raw.get(key)}


def fetch_races(client, first_day, months_ahead):
    """Eventi del calendario Garmin Connect (gare comprese) dal mese di first_day in avanti."""
    events = {}
    y, m = first_day.year, first_day.month
    last = date.today() + timedelta(days=31 * months_ahead)
    while (y, m) <= (last.year, last.month):
        cal = safe(client.connectapi, f"/calendar-service/year/{y}/month/{m - 1}") or {}
        for it in cal.get("calendarItems") or []:
            if it.get("itemType") != "event" or not it.get("date"):
                continue
            target = it.get("completionTarget") or {}
            km = target.get("value") if target.get("unitType") == "distance" else None
            if km and target.get("unit") == "meter":
                km /= 1000
            elif km and target.get("unit") == "mile":
                km *= 1.609344
            events[it.get("id") or it["date"]] = {
                "date": it["date"], "name": it.get("title") or "Gara", "km": round(km, 4) if km else None,
                "primary": bool(it.get("primaryEvent")), "race": bool(it.get("isRace")),
            }
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return sorted(events.values(), key=lambda e: e["date"])


def pick_goal(races):
    """La gara principale futura di Garmin; altrimenti la prima gara futura; altrimenti la configurazione."""
    today = date.today().isoformat()
    future = [r for r in races if r["date"] >= today and r["race"]]
    goal = next((r for r in future if r["primary"]), None) or (future[0] if future else None)
    if goal:
        return {"date": goal["date"], "name": goal["name"], "km": goal["km"]}
    return {"date": RACE_DATE.isoformat(), "name": RACE_NAME, "km": RACE_KM}


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
        return {"labels": [], "values": [], "avg": 0}
    # tutte le settimane, anche quelle a 0 km, così non spariscono barre
    mondays = []
    d = start - timedelta(days=start.weekday())
    while d <= today:
        mondays.append(d)
        d += timedelta(weeks=1)
    values = [round(weekly.get(iso_week_key(m), 0.0), 1) for m in mondays]
    # media delle sole settimane complete: quella in corso la abbasserebbe
    done = values[:-1] or values
    return {"labels": [f"{m:%d/%m}" for m in mondays], "values": values, "avg": round(sum(done) / len(done), 1)}


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


def build_calendar(activities, load_series, races, pb_5k_pace_sec_km, until):
    """Settimane lun-dom per la scheda Calendario: attività di ogni giorno e riepilogo della settimana."""
    today = date.today()
    start = today - timedelta(days=today.weekday(), weeks=CALENDAR_WEEKS_BACK)
    end = min(max(today, until), today + timedelta(weeks=CALENDAR_WEEKS_AHEAD_MAX))
    end += timedelta(days=6 - end.weekday())
    by_day = {}
    for a in activities:
        try:
            dt = datetime.strptime(a.get("startTimeLocal"), "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        dist_km = (a.get("distance") or 0) / 1000.0
        dur_s = a.get("duration") or 0
        by_day.setdefault(dt.date(), []).append({
            "id": str(a.get("activityId")), "time": dt.strftime("%H:%M"),
            "name": a.get("activityName") or sport_label(a), "sport": sport_label(a), "group": sport_group(a),
            "dur": round(dur_s), "km": round(dist_km, 2) if dist_km else None,
            "pace": sec_to_pace_str(dur_s / dist_km) if is_pace_sport(a) and dist_km else None,
            "hr": round(a["averageHR"]) if a.get("averageHR") else None,
            "load": round(estimate_load(a)),
            "kind": classify_effort(a, pb_5k_pace_sec_km) if is_pace_sport(a) else None,
            "summary": build_activity_summary(a),
            "te": round(a["aerobicTrainingEffect"], 1) if a.get("aerobicTrainingEffect") else None,
            "desc": a.get("description") or "",
            "feel": a.get("directWorkoutFeel"),  # Garmin: 0, 25, 50, 75, 100
            "rpe": a.get("directWorkoutRpe"),    # Garmin: 10-100
        })
    load_by_day = {p["date"]: p for p in load_series}
    weeks = []
    monday = start
    while monday <= end:
        days = [monday + timedelta(days=i) for i in range(7)]
        acts = [sorted(by_day.get(d, []), key=lambda x: x["time"]) for d in days]
        flat = [x for day in acts for x in day]
        form = load_by_day.get(min(days[-1], today).isoformat()) if monday <= today else None
        weeks.append({
            "days": [{"date": d.isoformat(), "acts": day, "form": load_by_day.get(d.isoformat())} for d, day in zip(days, acts)],
            "dur": sum(x["dur"] for x in flat),
            "km": round(sum(x["km"] or 0 for x in flat), 1),
            "run_km": round(sum(x["km"] or 0 for x in flat if x["group"] == "run"), 1),
            "load": sum(x["load"] for x in flat),
            "ctl": form["ctl"] if form else None,
            "atl": form["atl"] if form else None,
            "tsb": form["tsb"] if form else None,
        })
        monday += timedelta(weeks=1)
    first, last = start.isoformat(), end.isoformat()
    return {"weeks": weeks, "races": [r for r in races if first <= r["date"] <= last]}


def compute_insights(mileage, vo2_series):
    """Consigli che non dipendono dalla gara. Quello sulla freschezza lo calcola la pagina,
    perché cambia se si cambia obiettivo dalla scheda Io."""
    insights = []
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


def json_for_script(obj):
    """json.dumps + escape di '</' per evitare che testo libero (es. nomi di
    attività con caratteri strani) chiuda accidentalmente il tag <script>."""
    return json.dumps(obj).replace("</", "<\\/")


# ----------------------------------------------------------------------------
# HTML TEMPLATE
# ----------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Dashboard allenamento · {page_title}</title>
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
    --tabbar-h: 64px;
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

  .tabbtn svg {{ display: none; }}

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
  .tl-head {{ display: flex; align-items: baseline; gap: 10px; font-size: 14px; }}
  .tl-num {{ font-size: 34px; font-weight: 700; }}
  .tl-bar {{ position: relative; height: 12px; border-radius: 6px; background: #2c2c2e; margin: 12px 0 4px; }}
  .tl-zone {{ position: absolute; top: 0; bottom: 0; background: #2f855a; }}
  .tl-dot {{ position: absolute; top: 50%; width: 16px; height: 16px; margin: -8px 0 0 -8px; border-radius: 50%; background: #fff; border: 2px solid #000; }}
  .tl-scale {{ position: relative; height: 16px; font-size: 12px; color: var(--muted); }}
  .tl-scale span {{ position: absolute; transform: translateX(-50%); }}
  .tl-note {{ color: var(--muted); font-size: 12px; margin-top: 6px; }}
  .tl-note.warn {{ color: #f6ad55; }}
  .tl-sub {{ font-size: 14px; margin: 20px 0 8px; font-weight: 600; }}
  .tl-frow {{ margin-bottom: 10px; }}
  .tl-flabel {{ display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 4px; }}
  .tl-fbar {{ position: relative; height: 10px; border-radius: 5px; background: #2c2c2e; }}
  .tl-fbar > div:first-child {{ height: 100%; border-radius: 5px; }}
  .tl-target {{ position: absolute; top: -3px; bottom: -3px; border: 1px dashed #fff; border-radius: 3px; }}
  .stress-legend {{ display: flex; flex-wrap: wrap; gap: 12px; font-size: 12px; color: var(--muted); margin-top: 8px; }}
  .stress-legend i {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: -1px; }}
  .missing {{ color: var(--muted); font-size: 13px; font-style: italic; padding: 20px 0; text-align: center; }}
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--panel-border); }}
  th {{ color: var(--muted); font-weight: 500; font-size: 12px; }}
  /* finestra allenamento: intestazione, barra dei numeri, due colonne, poi percorso e grafici */
  .wk-top {{ display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 10px; }}
  .wk-date {{ color: var(--info); font-weight: 600; text-transform: capitalize; }}
  .wk-chips {{ display: flex; gap: 6px; flex-wrap: wrap; }}
  .wk-chip {{ --c: var(--muted); border: 1px solid var(--c); color: var(--c); border-radius: 6px; padding: 3px 8px; font-size: 12px; font-weight: 600; }}
  .wk-chip.ctl {{ --c: var(--accent); }}
  .wk-chip.atl {{ --c: var(--danger); }}
  .wk-chip.tsb {{ --c: var(--accent2); }}
  .wk-bar {{ background: var(--panel-hover); border: 1px solid var(--panel-border); border-left: 4px solid var(--c, var(--muted)); border-radius: 10px; padding: 12px 14px; margin-bottom: 14px; }}
  .wk-bar.run {{ --c: var(--accent); }}
  .wk-bar.bike {{ --c: var(--info); }}
  .wk-bar.swim {{ --c: var(--hrv); }}
  .wk-bar.strength {{ --c: var(--accent2); }}
  .wk-big {{ display: flex; flex-wrap: wrap; gap: 6px 26px; align-items: baseline; margin: 4px 0; }}
  .wk-big b {{ font-size: 26px; font-weight: 700; }}
  .wk-big span {{ color: var(--muted); font-size: 13px; }}
  .wk-sport {{ color: var(--muted); font-size: 13px; }}
  .wk-cols {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 18px; margin-bottom: 18px; }}
  .wk-cols h4, .wk-more {{ margin: 0 0 6px; font-size: 13px; color: var(--muted); font-weight: 600; }}
  .wk-table td:first-child {{ color: var(--muted); }}
  .wk-table td:last-child {{ font-weight: 600; text-align: right; }}
  .wk-side p {{ margin: 0 0 14px; }}
  .wk-desc {{ background: var(--panel-hover); border-radius: 8px; padding: 10px; white-space: pre-wrap; overflow-wrap: anywhere; }}
  .wk-scale {{ display: flex; gap: 4px; margin-bottom: 14px; }}
  .wk-scale span {{ flex: 1; text-align: center; font-size: 11px; color: var(--muted); padding: 6px 2px; border-radius: 6px; background: var(--panel-hover); }}
  .wk-scale span.on {{ background: color-mix(in srgb, var(--accent) 22%, var(--panel)); color: var(--text); font-weight: 600; }}
  .wk-more {{ border-top: 1px solid var(--panel-border); padding-top: 14px; }}
  @media (max-width: 640px) {{
    .wk-cols {{ grid-template-columns: 1fr; }}
    .wk-big b {{ font-size: 22px; }}
  }}
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
  .route-wrap svg {{ width: 100%; height: auto; aspect-ratio: 600 / 260; display: block; border-radius: 8px; background: var(--panel); }}
  .route-credit {{ font-size: 11px; color: var(--muted); text-align: right; margin-top: 4px; }}
  .route-legend {{ display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; font-size: 12px; color: var(--muted); margin-top: 6px; }}
  .route-legend i {{ width: 90px; height: 6px; border-radius: 3px; background: linear-gradient(90deg, var(--info), var(--accent2), var(--danger)); }}
  .route-legend em {{ font-style: normal; flex-basis: 100%; }}
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

  /* ---------- scheda Io ---------- */
  .me-stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 10px; }}
  .me-stats .dstat {{ background: var(--panel-hover); border-radius: 10px; padding: 10px; }}
  .me-stats .dstat .v {{ font-size: 18px; font-weight: 700; }}
  .me-stats .dstat .l {{ font-size: 12px; color: var(--muted); }}
  .goal-form {{ display: grid; gap: 12px; }}
  .goal-form label {{ display: grid; gap: 4px; font-size: 12px; color: var(--muted); }}
  .goal-form label[hidden] {{ display: none; }}
  .goal-form input, .goal-form select {{ background: var(--panel-hover); border: 1px solid var(--panel-border); color: var(--text); border-radius: 8px; padding: 10px; font: inherit; font-size: 15px; min-height: 44px; width: 100%; }}
  .goal-form input:focus, .goal-form select:focus {{ border-color: var(--accent); outline: none; }}
  .form-btns {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .btn-primary, .btn-ghost {{ border-radius: 8px; padding: 10px 16px; font: inherit; font-weight: 600; cursor: pointer; min-height: 44px; }}
  .btn-primary {{ background: var(--accent); color: var(--bg); border: 1px solid var(--accent); }}
  .btn-ghost {{ background: transparent; color: var(--text); border: 1px solid var(--panel-border); }}
  .form-note {{ margin: 0; font-size: 12px; color: var(--muted); min-height: 1.4em; }}

  /* ---------- scheda Calendario ---------- */
  .cal-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }}
  .cal-head h2 {{ margin: 0; font-size: 18px; }}
  .cal-weekdays, .cal-week {{ display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)) minmax(140px, 1.1fr); gap: 1px; }}
  .cal-weekdays {{ position: sticky; top: 0; z-index: 4; background: var(--bg); font-size: 12px; color: var(--muted); padding: 6px 0; }}
  .cal-weekdays span {{ padding-left: 8px; }}
  .calendar {{ display: grid; gap: 1px; background: var(--panel-border); border: 1px solid var(--panel-border); border-radius: 12px; overflow: hidden; }}
  .cal-month {{ background: var(--bg); padding: 14px 8px 6px; font-weight: 700; font-size: 15px; text-transform: capitalize; }}
  .cal-day, .cal-sum {{ background: var(--panel); min-height: 116px; padding: 6px; display: flex; flex-direction: column; gap: 5px; min-width: 0; }}
  .cal-day.today {{ box-shadow: inset 0 0 0 2px var(--accent); }}
  .cal-date {{ font-size: 12px; color: var(--muted); }}
  .cal-date .wd {{ display: none; }}
  .cal-day.today .cal-date {{ color: var(--accent); font-weight: 700; }}
  .cal-items {{ display: flex; flex-direction: column; gap: 5px; min-width: 0; }}
  .cal-card {{ --c: var(--muted); display: block; width: 100%; text-align: left; background: var(--panel-hover); border: 1px solid var(--panel-border); border-left: 3px solid var(--c); border-radius: 8px; padding: 6px 7px; color: var(--text); font: inherit; font-size: 12px; line-height: 1.3; }}
  button.cal-card {{ cursor: pointer; }}
  button.cal-card:hover {{ border-color: var(--c); }}
  .cal-card.run {{ --c: var(--accent); }}
  .cal-card.bike {{ --c: var(--info); }}
  .cal-card.swim {{ --c: var(--hrv); }}
  .cal-card.strength {{ --c: var(--accent2); }}
  .cal-card.race {{ --c: var(--danger); background: color-mix(in srgb, var(--danger) 12%, var(--panel)); }}
  .cal-card .t {{ font-weight: 600; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }}
  .cal-card .s {{ color: var(--muted); font-size: 11px; margin-top: 2px; }}
  .cal-card .s b {{ color: var(--text); font-weight: 600; }}
  .cal-sum {{ background: var(--panel-hover); font-size: 12px; gap: 3px; }}
  .cal-sum h4 {{ margin: 0 0 2px; font-size: 12px; color: var(--muted); font-weight: 600; }}
  .cal-sum .row {{ display: flex; justify-content: space-between; gap: 6px; color: var(--muted); }}
  .cal-sum .row b {{ color: var(--text); font-weight: 600; }}

  @media (max-width: 899px) {{
    .cal-weekdays {{ display: none; }}
    .cal-week {{ grid-template-columns: 1fr; }}
    .cal-sum {{ order: -1; min-height: 0; flex-direction: row; flex-wrap: wrap; gap: 2px 14px; }}
    .cal-sum h4 {{ width: 100%; }}
    .cal-sum .row {{ gap: 4px; }}
    .cal-day {{ min-height: 0; flex-direction: row; align-items: flex-start; gap: 10px; padding: 8px; }}
    .cal-date {{ flex: 0 0 48px; padding-top: 2px; }}
    .cal-date .wd {{ display: block; }}
    .cal-items {{ flex: 1; }}
    .cal-day.rest .cal-items::after {{ content: 'Riposo'; color: var(--muted); font-size: 12px; padding-top: 2px; }}
  }}

  /* ---------- telefono: schede in basso come un'app ---------- */
  @media (max-width: 767px) {{
    body {{ padding-bottom: calc(var(--tabbar-h) + 24px + env(safe-area-inset-bottom)); }}
    .tabs {{ position: fixed; left: 0; right: 0; bottom: 0; z-index: 40; margin: 0; gap: 0; border-bottom: none; border-top: 1px solid var(--panel-border); background: color-mix(in srgb, var(--bg) 88%, transparent); -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px); padding: 4px 4px env(safe-area-inset-bottom); }}
    .tabbtn {{ flex: 1; min-width: 0; min-height: var(--tabbar-h); display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 3px; padding: 4px 2px; font-size: 11px; border-bottom: none; margin: 0; -webkit-tap-highlight-color: transparent; }}
    .tabbtn svg {{ display: block; width: 23px; height: 23px; }}
    .tabbtn[aria-selected="true"] {{ font-weight: 600; }}
  }}

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
    .hide-sm {{ display: none; }}
    th, td {{ padding: 8px 6px; }}
    .modal-box {{ width: calc(100% - 20px); padding: 16px; }}
    .modal-box.large {{ max-height: 92vh; }}
  }}
</style>
</head>
<body>
<div class="container">

  <div id="summary">
  <div class="top-grid">
    <div class="hero">
      <h1 id="goalTitle"></h1>
      <div class="num" id="goalDays"></div>
      <div class="sub" id="goalSub"></div>
      <div class="predict" id="goalPredict"></div>
    </div>
    <div class="stat-cards">
      <div class="stat-card"><div class="l">Forma fisica <abbr title="Chronic Training Load">CTL</abbr></div><div class="v">{card_ctl}</div>{delta_ctl}<canvas id="sparkCtl" aria-hidden="true"></canvas></div>
      <div class="stat-card"><div class="l">Fatica <abbr title="Acute Training Load">ATL</abbr></div><div class="v">{card_atl}</div>{delta_atl}<canvas id="sparkAtl" aria-hidden="true"></canvas></div>
      <div class="stat-card"><div class="l">VO2 max</div><div class="v">{card_vo2}</div>{delta_vo2}<canvas id="sparkVo2" aria-hidden="true"></canvas></div>
      <div class="stat-card"><div class="l">FC a riposo</div><div class="v">{card_rhr}</div>{delta_rhr}<canvas id="sparkRhr" aria-hidden="true"></canvas></div>
      <div class="stat-card"><div class="l">Variabilità FC <abbr title="Heart Rate Variability">HRV</abbr></div><div class="v">{card_hrv}</div>{delta_hrv}<canvas id="sparkHrv" aria-hidden="true"></canvas></div>
    </div>
  </div>

  <div class="insights" id="insights">{insights_html}</div>

  <div class="week-row" aria-label="Questa settimana">{week_row_html}</div>
  <div class="legend" aria-hidden="true"><span class="easy">Facile</span><span class="workout">Allenamento</span><span class="rest">Riposo</span></div>
  </div>

  <nav class="tabs" role="tablist" aria-label="Sezioni">
    <button class="tabbtn" role="tab" id="tab-fitness" aria-controls="section-fitness" aria-selected="true" onclick="showSection('fitness', this)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 12 7 12 10 5 14 19 17 12 21 12"/></svg>Forma</button>
    <button class="tabbtn" role="tab" id="tab-recovery" aria-controls="section-recovery" aria-selected="false" tabindex="-1" onclick="showSection('recovery', this)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z"/></svg>Recupero</button>
    <button class="tabbtn" role="tab" id="tab-calendar" aria-controls="section-calendar" aria-selected="false" tabindex="-1" onclick="showSection('calendar', this)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/></svg>Calendario</button>
    <button class="tabbtn" role="tab" id="tab-me" aria-controls="section-me" aria-selected="false" tabindex="-1" onclick="showSection('me', this)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-6 8-6s8 2 8 6"/></svg>Io</button>
  </nav>

  <div class="section active" id="section-fitness" role="tabpanel" aria-labelledby="tab-fitness">
    <div class="grid three">
      <div class="panel chart-panel" role="button" tabindex="0" aria-label="Ingrandisci il grafico Forma fisica" onclick="openEnlarge('ctl')"><h2>Forma fisica <abbr>CTL</abbr></h2><div class="desc">Quanto sei allenato: media del carico degli ultimi 42 giorni. Più è alta, più sei in forma.</div>{ctl_chart_or_missing}</div>
      <div class="panel chart-panel" role="button" tabindex="0" aria-label="Ingrandisci il grafico Fatica" onclick="openEnlarge('atl')"><h2>Fatica <abbr>ATL</abbr></h2><div class="desc">Carico degli ultimi 7 giorni: sale dopo giorni duri, scende con il riposo.</div>{atl_chart_or_missing}</div>
      <div class="panel chart-panel" role="button" tabindex="0" aria-label="Ingrandisci il grafico Freschezza" onclick="openEnlarge('tsb')"><h2>Freschezza <abbr>TSB</abbr></h2><div class="desc">Forma fisica meno fatica. Sotto −20 molto stanco, tra −10 e +5 zona di allenamento, sopra +5 fresco per gareggiare.</div>{tsb_chart_or_missing}</div>
    </div>
    <div class="grid" style="margin-top:18px;">
      <div class="panel chart-panel" role="button" tabindex="0" aria-label="Ingrandisci il grafico Chilometraggio settimanale" onclick="openEnlarge('mileage')"><h2>Chilometraggio settimanale</h2><div class="desc">Ultime {weeks_trend} settimane di corsa (la data è il lunedì). Azzurro = sopra la tua media (linea tratteggiata), grigio = sotto; la settimana in corso è tratteggiata.</div>{mileage_chart_or_missing}</div>
      <div class="panel"><h2>Carico di allenamento</h2><div class="desc">Dati ufficiali Garmin, come sull'orologio.</div>{training_load_html}</div>
    </div>
  </div>

  <div class="section" id="section-recovery" role="tabpanel" aria-labelledby="tab-recovery">
    <div class="grid">
      <div class="panel full"><h2>Stress</h2>
        <div class="range-btns" role="group" aria-label="Periodo stress"><button class="range-btn" aria-pressed="true" onclick="showStress('day', this)">{stress_day_title}</button><button class="range-btn" aria-pressed="false" onclick="showStress('week', this)">Settimana</button></div>
        <div id="stress-day"><div class="desc">Un valore ogni 3 minuti, come nell'app Garmin. Buchi = attività o orologio non indossato.</div>{stress_day_chart_or_missing}</div>
        <div id="stress-week" hidden><div class="desc">Stress medio di ogni giorno negli ultimi 7 giorni.</div>{stress_week_chart_or_missing}</div>
        <div class="stress-legend"><span><i style="background:#3b82f6"></i>Riposo 0-25</span><span><i style="background:#f6ad55"></i>Basso 26-50</span><span><i style="background:#ed8936"></i>Medio 51-75</span><span><i style="background:#c05621"></i>Alto 76-100</span></div>
      </div>
    </div>
    <div class="grid" style="margin-top:18px;">
      <div class="panel chart-panel" role="button" tabindex="0" aria-label="Ingrandisci il grafico HRV" onclick="openEnlarge('hrv')"><h2>Variabilità cardiaca <abbr>HRV</abbr></h2><div class="desc">Ultime {recovery_weeks} settimane. Valori stabili o in salita indicano un buon recupero.</div>{hrv_chart_or_missing}</div>
      <div class="panel chart-panel" role="button" tabindex="0" aria-label="Ingrandisci il grafico FC a riposo" onclick="openEnlarge('rhr')"><h2>FC a riposo</h2><div class="desc">Ultime {recovery_weeks} settimane. Se sale di 5 o più battiti per qualche giorno, il corpo chiede riposo.</div>{rhr_chart_or_missing}</div>
      <div class="panel chart-panel" role="button" tabindex="0" aria-label="Ingrandisci il grafico Sonno" onclick="openEnlarge('sleep')"><h2>Sonno</h2><div class="desc">Ore di sonno per notte.</div>{sleep_chart_or_missing}</div>
      <div class="panel chart-panel" role="button" tabindex="0" aria-label="Ingrandisci il grafico VO2 max" onclick="openEnlarge('vo2')"><h2>VO2 max</h2><div class="desc">Stima del device Garmin della tua capacità aerobica.</div>{vo2_chart_or_missing}</div>
    </div>
  </div>

  <div class="section" id="section-calendar" role="tabpanel" aria-labelledby="tab-calendar">
    <div class="cal-head">
      <h2>Calendario</h2>
      <button type="button" class="range-btn" onclick="scrollCalendarToToday(true)">Vai a oggi</button>
    </div>
    <div class="cal-weekdays" aria-hidden="true"><span>Lun</span><span>Mar</span><span>Mer</span><span>Gio</span><span>Ven</span><span>Sab</span><span>Dom</span><span>Settimana</span></div>
    <div class="calendar" id="calendar"></div>
  </div>

  <div class="section" id="section-me" role="tabpanel" aria-labelledby="tab-me">
    <div class="grid">
      <div class="panel">
        <h2 id="meName">Io</h2>
        <div class="desc" id="meUpdated"></div>
        <div class="me-stats" id="meStats"></div>
      </div>
      <div class="panel">
        <h2>Obiettivo</h2>
        <form id="goalForm" class="goal-form" onsubmit="saveGoal(event)" novalidate>
          <label>Nome della gara<input id="goalName" type="text" maxlength="60" required autocomplete="off"></label>
          <label>Data<input id="goalDate" type="date" required></label>
          <label>Distanza
            <select id="goalDist" onchange="toggleCustomDist()">
              <option value="5">5 km</option>
              <option value="10">10 km</option>
              <option value="21.0975">Mezza maratona</option>
              <option value="42.195">Maratona</option>
              <option value="custom">Altra distanza</option>
            </select>
          </label>
          <label id="goalKmWrap" hidden>Chilometri<input id="goalKm" type="number" min="1" max="250" step="0.1" inputmode="decimal"></label>
          <label>Tempo obiettivo (facoltativo)<input id="goalTarget" type="text" placeholder="es. 54:30 oppure 1:55:00" inputmode="numeric" autocomplete="off"></label>
          <div class="form-btns">
            <button type="submit" class="btn-primary">Salva obiettivo</button>
            <button type="button" class="btn-ghost" id="goalReset" onclick="resetGoal()">Usa la gara di Garmin</button>
          </div>
          <p class="form-note" id="goalNote" role="status"></p>
        </form>
      </div>
      <div class="panel">
        <h2>Previsioni Garmin</h2>
        <div class="desc">I tempi che il tuo Garmin stima oggi per ogni distanza.</div>
        <div id="mePredictions"></div>
      </div>
      <div class="panel">
        <h2>Record personali</h2>
        <div id="meRecords"></div>
      </div>
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
        // un flag, non una funzione: Chart.js chiamerebbe da solo le funzioni nelle opzioni
        if (!opts || !opts.show || !meta || !meta.data) return;
        const formatter = (v) => v ? String(Math.round(v)) : '';
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

function showStress(which, btn) {{
  document.getElementById('stress-day').hidden = which !== 'day';
  document.getElementById('stress-week').hidden = which !== 'week';
  btn.parentNode.querySelectorAll('.range-btn').forEach(b => b.setAttribute('aria-pressed', b === btn ? 'true' : 'false'));
}}

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
  // calendario e profilo hanno tutto lo schermo: il riepilogo in alto resta nelle altre schede
  document.getElementById('summary').hidden = name === 'calendar' || name === 'me';
  if (window.matchMedia('(max-width: 767px)').matches) window.scrollTo(0, 0);
  if (name === 'calendar') scrollCalendarToToday(false);
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
  if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('[role="button"]')) {{
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

/* ---------- obiettivo, scheda Io e calendario ---------- */
const PROFILE = {profile_json};
const PREDICTIONS = {predictions_json};
const GARMIN_GOAL = {goal_json};
const CALENDAR = {calendar_json};
const TSB_NOW = {tsb_now};
const GENERATED_STAMP = '{generated_stamp}';
const GENERATED_LABEL = '{generated_label}';
const GOAL_KEY = 'corsa-obiettivo';
const WEEKDAYS = ['Lun', 'Mar', 'Mer', 'Gio', 'Ven', 'Sab', 'Dom'];
const MONTHS = ['gennaio', 'febbraio', 'marzo', 'aprile', 'maggio', 'giugno', 'luglio', 'agosto', 'settembre', 'ottobre', 'novembre', 'dicembre'];

function esc(v) {{
  return String(v === null || v === undefined ? '' : v).replace(/[&<>"']/g, c => ({{ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }})[c]);
}}
function localIso(d) {{
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}}
function fmtDate(iso) {{ return iso.slice(8, 10) + '/' + iso.slice(5, 7) + '/' + iso.slice(0, 4); }}
function fmtDur(sec) {{
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s).padStart(2, '0');
}}
function parseTime(txt) {{
  const parts = txt.trim().split(':').map(Number);
  if (parts.length < 2 || parts.length > 3 || parts.some(n => !Number.isFinite(n) || n < 0)) return null;
  const sec = parts.reduce((acc, n) => acc * 60 + n, 0);
  return sec > 0 ? sec : null;
}}
function fmtKm(km) {{
  if (Math.abs(km - 21.0975) < 0.01) return 'mezza maratona';
  if (Math.abs(km - 42.195) < 0.01) return 'maratona';
  return Math.round(km * 10) / 10 + ' km';
}}
function daysUntil(iso) {{
  const [y, m, d] = iso.split('-').map(Number);
  const t = new Date();
  return Math.round((new Date(y, m - 1, d) - new Date(t.getFullYear(), t.getMonth(), t.getDate())) / 86400000);
}}

function loadGoal() {{
  try {{
    const g = JSON.parse(localStorage.getItem(GOAL_KEY));
    if (g && g.name && g.date && g.km > 0) return Object.assign({{}}, g, {{ custom: true }});
  }} catch (e) {{}}
  return Object.assign({{}}, GARMIN_GOAL, {{ custom: false }});
}}
let GOAL = loadGoal();

function predictFor(km) {{
  // Garmin stima 5 km, 10 km, mezza e maratona: per le altre distanze scalo dalla stima più vicina (Riegel)
  const dists = Object.keys(PREDICTIONS);
  if (!dists.length || !(km > 0)) return null;
  const near = dists.reduce((a, b) => Math.abs(Math.log(Number(b) / km)) < Math.abs(Math.log(Number(a) / km)) ? b : a);
  const exact = Math.abs(Number(near) - km) < 0.01;
  return {{ sec: exact ? PREDICTIONS[near] : PREDICTIONS[near] * Math.pow(km / Number(near), 1.06), exact }};
}}

function renderGoal() {{
  const g = GOAL, days = daysUntil(g.date);
  document.title = 'Dashboard allenamento · ' + g.name;
  document.getElementById('goalTitle').textContent = g.name + ' · ' + fmtDate(g.date);
  document.getElementById('goalDays').textContent = days >= 0 ? days : '—';
  document.getElementById('goalSub').textContent = days > 0 ? 'giorni alla gara · ' + (days / 7).toFixed(1) + ' settimane'
    : days === 0 ? 'La gara è oggi: in bocca al lupo!' : 'Gara passata: scegli il prossimo obiettivo nella scheda Io.';
  const p = predictFor(g.km);
  const label = fmtKm(g.km);
  let txt = p ? 'Stima Garmin ' + (label.endsWith('maratona') ? 'sulla ' : 'sui ') + label + ': <strong>' + fmtDur(p.sec) + '</strong> (' + fmtPace(p.sec / g.km) + '/km)'
    + (p.exact ? '' : ', ricavata dalla previsione Garmin più vicina') : 'Garmin non ha ancora una stima per questa distanza.';
  if (g.target) txt += '<br>Il tuo obiettivo: <strong>' + fmtDur(g.target) + '</strong> (' + fmtPace(g.target / g.km) + '/km)';
  document.getElementById('goalPredict').innerHTML = txt;
}}

function renderInsights() {{
  const box = document.getElementById('insights');
  box.querySelectorAll('.insight.tsb').forEach(e => e.remove());
  if (TSB_NOW !== null) {{
    const tsb = TSB_NOW, days = daysUntil(GOAL.date), taper = days > 0 && days <= 21;
    let c, t, p;
    if (tsb < -15 && taper) {{
      c = 'danger'; t = 'Stanco a ' + days + ' giorni dalla gara';
      p = 'Freschezza a ' + tsb + ': è il momento di scaricare. Riduci il volume del 20-30% a settimana e tieni una sola seduta breve a ritmo gara: la freschezza risalirà in tempo per il ' + fmtDate(GOAL.date).slice(0, 5) + '.';
    }} else if (tsb < -15) {{
      c = 'danger'; t = 'Fatica alta';
      p = 'Freschezza a ' + tsb + ': la fatica è parecchio sopra la forma fisica. Meglio 2-3 giorni facili prima della prossima sessione di qualità.';
    }} else if (taper) {{
      c = tsb > -5 ? 'good' : 'info'; t = 'Scarico pre-gara: ' + days + ' giorni';
      p = "Freschezza a " + tsb + ". Nelle ultime settimane riduci il volume e mantieni un po' di ritmo gara: l'obiettivo è arrivare al via con la freschezza sopra 0.";
    }} else if (tsb > 5) {{
      c = 'good'; t = 'Sei fresco';
      p = 'Freschezza a +' + tsb + ': buon momento per una sessione impegnativa o per testarti in gara.';
    }} else {{
      c = 'info'; t = 'Equilibrio ok';
      p = 'Freschezza a ' + tsb + ': forma fisica e fatica sono bilanciate, puoi proseguire il piano come previsto.';
    }}
    const el = document.createElement('div');
    el.className = 'insight tsb ' + c;
    el.innerHTML = '<h2></h2><p></p>';
    el.firstChild.textContent = t;
    el.lastChild.textContent = p;
    box.appendChild(el);
  }}
  // il consiglio più urgente per primo: è quello che la pagina mette in evidenza
  const order = ['danger', 'warn', 'info', 'good'];
  const rank = el => order.findIndex(c => el.classList.contains(c));
  [...box.children].sort((a, b) => rank(a) - rank(b)).forEach(el => box.appendChild(el));
}}

function renderMe() {{
  const P = PROFILE;
  document.getElementById('meName').textContent = P.name || 'Io';
  document.getElementById('meUpdated').textContent = 'Dati Garmin aggiornati il ' + GENERATED_LABEL + '.';
  const stats = [
    ['Età', P.age ? P.age + ' anni' : null], ['Altezza', P.height_cm ? P.height_cm + ' cm' : null],
    ['Peso', P.weight_kg ? P.weight_kg + ' kg' : null], ['VO2 max', P.vo2max],
    ['FC a riposo', P.rhr ? P.rhr + ' bpm' : null], ['FC di soglia', P.lt_hr ? P.lt_hr + ' bpm' : null],
  ].filter(s => s[1]);
  document.getElementById('meStats').innerHTML = stats.map(s => '<div class="dstat"><div class="v">' + esc(s[1]) + '</div><div class="l">' + s[0] + '</div></div>').join('');

  const names = {{ '5': '5 km', '10': '10 km', '21.0975': 'Mezza maratona', '42.195': 'Maratona' }};
  const preds = Object.keys(PREDICTIONS);
  document.getElementById('mePredictions').innerHTML = preds.length
    ? '<table><thead><tr><th>Distanza</th><th>Tempo</th><th>Passo</th></tr></thead><tbody>' + preds.map(k =>
        '<tr><td>' + names[k] + '</td><td>' + fmtDur(PREDICTIONS[k]) + '</td><td>' + fmtPace(PREDICTIONS[k] / Number(k)) + '/km</td></tr>').join('') + '</tbody></table>'
    : '<div class="missing">Garmin non ha ancora previsioni per te.</div>';

  document.getElementById('meRecords').innerHTML = P.records.length
    ? '<table><thead><tr><th>Distanza</th><th>Record</th><th>Data</th><th class="hide-sm">Attività</th></tr></thead><tbody>' + P.records.map(r =>
        '<tr><td>' + esc(r.label) + '</td><td><strong>' + esc(r.value) + '</strong></td><td>' + esc(r.date) + '</td><td class="hide-sm">' + esc(r.activity) + '</td></tr>').join('') + '</tbody></table>'
    : '<div class="missing">Nessun record personale su Garmin.</div>';
}}

function toggleCustomDist() {{
  const custom = document.getElementById('goalDist').value === 'custom';
  document.getElementById('goalKmWrap').hidden = !custom;
}}
function fillGoalForm() {{
  document.getElementById('goalName').value = GOAL.name;
  document.getElementById('goalDate').value = GOAL.date;
  const preset = ['5', '10', '21.0975', '42.195'].find(k => GOAL.km && Math.abs(Number(k) - GOAL.km) < 0.01);
  document.getElementById('goalDist').value = preset || 'custom';
  document.getElementById('goalKm').value = preset ? '' : (GOAL.km || '');
  toggleCustomDist();
  document.getElementById('goalTarget').value = GOAL.target ? fmtDur(GOAL.target) : '';
  document.getElementById('goalReset').hidden = !GOAL.custom;
  document.getElementById('goalNote').textContent = GOAL.custom
    ? 'Obiettivo scelto da te, salvato solo su questo dispositivo.'
    : 'Obiettivo preso dalla gara principale del tuo calendario Garmin Connect. Se lo cambi qui, vale solo su questo dispositivo.';
}}
function saveGoal(e) {{
  e.preventDefault();
  const note = document.getElementById('goalNote');
  const name = document.getElementById('goalName').value.trim();
  const dateIso = document.getElementById('goalDate').value;
  const dist = document.getElementById('goalDist').value;
  const km = dist === 'custom' ? parseFloat(String(document.getElementById('goalKm').value).replace(',', '.')) : Number(dist);
  const targetTxt = document.getElementById('goalTarget').value.trim();
  const target = targetTxt ? parseTime(targetTxt) : null;
  if (!name) {{ note.textContent = 'Scrivi il nome della gara.'; document.getElementById('goalName').focus(); return; }}
  if (!dateIso) {{ note.textContent = 'Scegli la data della gara.'; document.getElementById('goalDate').focus(); return; }}
  if (!(km > 0)) {{ note.textContent = 'Scrivi quanti chilometri è lunga la gara.'; document.getElementById('goalKm').focus(); return; }}
  if (targetTxt && !target) {{ note.textContent = 'Scrivi il tempo come 54:30 oppure 1:55:00.'; document.getElementById('goalTarget').focus(); return; }}
  GOAL = {{ name, date: dateIso, km, target, custom: true }};
  let saved = true;
  try {{ localStorage.setItem(GOAL_KEY, JSON.stringify(GOAL)); }} catch (err) {{ saved = false; }}
  refreshGoal();
  note.textContent = saved ? 'Obiettivo salvato.' : 'Obiettivo applicato, ma questo browser non permette di salvarlo: alla prossima apertura tornerà quello di Garmin.';
}}
function resetGoal() {{
  try {{ localStorage.removeItem(GOAL_KEY); }} catch (e) {{}}
  GOAL = loadGoal();
  refreshGoal();
}}
function refreshGoal() {{
  renderGoal();
  renderInsights();
  renderCalendar();
  fillGoalForm();
}}

/* calendario: settimane lun-dom con il riepilogo della settimana nell'ultima colonna */
function actCard(a, iso) {{
  const main = [a.dur ? '<b>' + fmtDur(a.dur) + '</b>' : '', a.km ? a.km + ' km' : '', a.pace || ''].filter(Boolean).join(' · ');
  const kind = a.kind === 'workout' ? 'qualità' : a.kind === 'easy' ? 'facile' : '';
  const extra = [esc(a.sport) + (kind ? ' ' + kind : ''), a.load ? 'carico ' + a.load : '', a.hr ? a.hr + ' bpm' : ''].filter(Boolean).join(' · ');
  const inner = '<div class="t">' + esc(a.name) + '</div>' + (main ? '<div class="s">' + main + '</div>' : '') + '<div class="s">' + extra + '</div>';
  return '<button type="button" class="cal-card ' + a.group + '" data-id="' + esc(a.id) + '" aria-label="' + esc(a.name + ', ' + fmtDate(iso) + ': apri il dettaglio') + '">' + inner + '</button>';
}}
function raceCard(r) {{
  const label = r.date === GOAL.date ? 'Il tuo obiettivo' : 'Gara';
  return '<div class="cal-card race"><div class="t">' + esc(r.name) + '</div><div class="s">' + label + (r.km ? ' · ' + fmtKm(r.km) : '') + '</div></div>';
}}
function weekSummary(w) {{
  const rows = [];
  if (w.dur) rows.push(['Tempo', fmtDur(w.dur)]);
  if (w.run_km) rows.push(['Corsa', w.run_km + ' km']);
  if (w.km && w.km !== w.run_km) rows.push(['Distanza totale', w.km + ' km']);
  if (w.load) rows.push(['Carico', w.load]);
  if (w.ctl !== null) rows.push(['Forma', w.ctl], ['Fatica', w.atl], ['Freschezza', (w.tsb > 0 ? '+' : '') + w.tsb]);
  const body = rows.length ? rows.map(r => '<div class="row"><span>' + r[0] + '</span><b>' + r[1] + '</b></div>').join('')
    : '<div class="row"><span>Ancora niente</span></div>';
  return '<div class="cal-sum"><h4>Settimana</h4>' + body + '</div>';
}}
function renderCalendar() {{
  const box = document.getElementById('calendar');
  if (!CALENDAR.weeks.length) {{ box.innerHTML = '<div class="missing">Nessuna settimana da mostrare.</div>'; return; }}
  const today = localIso(new Date());
  const races = CALENDAR.races.slice();
  if (!races.some(r => r.date === GOAL.date)) races.push({{ date: GOAL.date, name: GOAL.name, km: GOAL.km }});
  let html = '';
  CALENDAR.weeks.forEach((w, wi) => {{
    const first = w.days.find(d => d.date.endsWith('-01')) || (wi === 0 ? w.days[0] : null);
    if (first) html += '<div class="cal-month">' + MONTHS[Number(first.date.slice(5, 7)) - 1] + ' ' + first.date.slice(0, 4) + '</div>';
    html += '<div class="cal-week">';
    w.days.forEach((d, i) => {{
      const dayRaces = races.filter(r => r.date === d.date);
      const cls = ['cal-day'];
      if (d.date === today) cls.push('today');
      else if (d.date < today && !d.acts.length && !dayRaces.length) cls.push('rest');
      html += '<div class="' + cls.join(' ') + '"' + (d.date === today ? ' aria-current="date"' : '') + '>'
        + '<div class="cal-date"><span class="wd">' + WEEKDAYS[i] + '</span>' + Number(d.date.slice(8, 10)) + '</div>'
        + '<div class="cal-items">' + dayRaces.map(raceCard).join('') + d.acts.map(a => actCard(a, d.date)).join('') + '</div></div>';
    }});
    html += weekSummary(w) + '</div>';
  }});
  box.innerHTML = html;
}}
function scrollCalendarToToday(smooth) {{
  const t = document.querySelector('.cal-day.today');
  if (t) t.scrollIntoView({{ block: 'center', behavior: smooth ? 'smooth' : 'auto' }});
}}
document.getElementById('calendar').addEventListener('click', e => {{
  const b = e.target.closest('button.cal-card');
  if (b) openDetail(b.dataset.id);
}});

try {{ renderGoal(); }} catch (e) {{ console.error('Errore obiettivo', e); }}
try {{ renderInsights(); }} catch (e) {{ console.error('Errore consigli', e); }}
try {{ renderMe(); fillGoalForm(); }} catch (e) {{ console.error('Errore scheda Io', e); }}
try {{ renderCalendar(); }} catch (e) {{ console.error('Errore calendario', e); }}

/* ---------- pagina sempre aggiornata: se online c'è una versione più nuova, si ricarica ---------- */
async function checkNewVersion() {{
  if (location.protocol === 'file:') return;
  try {{
    const r = await fetch('version.txt?t=' + Date.now(), {{ cache: 'no-store' }});
    if (!r.ok) return;
    const v = (await r.text()).trim();
    if (!/^[0-9]{{12}}$/.test(v) || v <= GENERATED_STAMP) return;
    // la cache di GitHub ignora il ?v= e può ridare la pagina vecchia per qualche minuto:
    // una sola ricarica per versione, altrimenti la pagina si ricaricherebbe all'infinito
    if (sessionStorage.getItem('ricaricata') === v) return;
    sessionStorage.setItem('ricaricata', v);
    location.replace(location.pathname + '?v=' + v);
  }} catch (e) {{}}
}}
checkNewVersion();
document.addEventListener('visibilitychange', () => {{ if (document.visibilityState === 'visible') checkNewVersion(); }});

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
const ACTIVITY_TIMESERIES = {activity_timeseries_json};
const ACTIVITY_SUMMARY = {activity_summary_json};

/* percorso su foto satellitare: tile di Esri World Imagery (gratuite, senza chiave) disegnate
   nello stesso SVG della linea, con la stessa proiezione (Web Mercator), così combaciano */
const ROUTE_W = 600, ROUTE_H = 260, ROUTE_PAD = 24;
let routePts = [];

function mercator(la, lo, z) {{
  const n = 256 * Math.pow(2, z), s = Math.sin(la * Math.PI / 180);
  return [(lo + 180) / 360 * n, (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * n];
}}

function cssColor(name) {{
  const h = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
}}

// lento = blu, medio = arancione, veloce = rosso; ratio = passo / passo di riferimento
function paceColor(ratio) {{
  const t = Math.max(0, Math.min(1, (1.25 - ratio) / 0.25));
  const [a, b, k] = t < 0.5 ? [cssColor('--info'), cssColor('--accent2'), t * 2] : [cssColor('--accent2'), cssColor('--danger'), (t - 0.5) * 2];
  return 'rgb(' + a.map((v, i) => Math.round(v + (b[i] - v) * k)).join(',') + ')';
}}

function routeRefPace(act, paces) {{
  // corsa: ritmo gara Garmin sui 10 km; altri sport: il 10% più veloce di quella attività
  if (act && act.group === 'run' && PREDICTIONS['10']) return PREDICTIONS['10'] / 10;
  const sorted = paces.filter(v => v).sort((a, b) => a - b);
  return sorted.length ? sorted[Math.floor(sorted.length * 0.1)] : null;
}}

function buildRouteSvg(ts, act) {{
  const idx = ts.lat.map((v, i) => i).filter(i => ts.lat[i] !== null && ts.lon[i] !== null);
  const lat = idx.map(i => ts.lat[i]), lon = idx.map(i => ts.lon[i]);
  const paces = idx.map(i => (ts.pace_sec_km || [])[i] || null);
  const latMin = Math.min(...lat), latMax = Math.max(...lat), lonMin = Math.min(...lon), lonMax = Math.max(...lon);
  let z = 18;
  for (; z > 2; z--) {{
    const [x0, y0] = mercator(latMax, lonMin, z), [x1, y1] = mercator(latMin, lonMax, z);
    if (x1 - x0 <= ROUTE_W - 2 * ROUTE_PAD && y1 - y0 <= ROUTE_H - 2 * ROUTE_PAD) break;
  }}
  const [cx, cy] = mercator((latMin + latMax) / 2, (lonMin + lonMax) / 2, z);
  const ox = cx - ROUTE_W / 2, oy = cy - ROUTE_H / 2;
  routePts = lat.map((la, i) => {{ const [x, y] = mercator(la, lon[i], z); return [x - ox, y - oy]; }});

  let tiles = '';
  for (let tx = Math.floor(ox / 256); tx <= Math.floor((ox + ROUTE_W) / 256); tx++) {{
    for (let ty = Math.floor(oy / 256); ty <= Math.floor((oy + ROUTE_H) / 256); ty++) {{
      tiles += `<image href="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/${{z}}/${{ty}}/${{tx}}" x="${{(tx * 256 - ox).toFixed(1)}}" y="${{(ty * 256 - oy).toFixed(1)}}" width="256" height="256"/>`;
    }}
  }}
  const ref = routeRefPace(act, paces);
  let segs = '';
  for (let i = 1; i < routePts.length; i++) {{
    const pace = paces[i] || paces[i - 1];
    const color = ref && pace ? paceColor(pace / ref) : 'var(--accent)';
    segs += `<line x1="${{routePts[i - 1][0].toFixed(1)}}" y1="${{routePts[i - 1][1].toFixed(1)}}" x2="${{routePts[i][0].toFixed(1)}}" y2="${{routePts[i][1].toFixed(1)}}" style="stroke:${{color}}"/>`;
  }}
  const raceRef = act && act.group === 'run' && PREDICTIONS['10'];
  const legend = ref ? `<div class="route-legend"><span>Lento</span><i aria-hidden="true"></i><span>Veloce</span><em>${{raceRef ? 'Rosso = il tuo ritmo gara Garmin sui 10 km (' + fmtPace(ref) + '/km)' : 'Colori rispetto al passo di questa attività'}}</em></div>` : '';
  return `<svg viewBox="0 0 ${{ROUTE_W}} ${{ROUTE_H}}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Percorso su mappa satellitare, colorato in base alla velocità">
      ${{tiles}}
      <g id="routeSegs" stroke-width="2.5" stroke-linecap="round">${{segs}}</g>
      <circle id="routeDot" r="5" style="fill:var(--text);stroke:var(--bg);stroke-width:1.5"/>
    </svg><div class="route-credit">Immagini © Esri, Maxar, Earthstar Geographics</div>${{legend}}`;
}}

function animateRoute() {{
  const segs = document.querySelectorAll('#routeSegs line');
  const dot = document.getElementById('routeDot');
  if (!dot || !routePts.length) return;
  const duration = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 2500;
  const start = performance.now();
  function frame(now) {{
    const t = duration ? Math.min(1, (now - start) / duration) : 1;
    const k = Math.round(t * segs.length);
    segs.forEach((l, i) => {{ l.style.visibility = i < k ? 'visible' : 'hidden'; }});
    const pt = routePts[Math.min(routePts.length - 1, k)];
    dot.setAttribute('cx', pt[0]);
    dot.setAttribute('cy', pt[1]);
    if (t < 1) requestAnimationFrame(frame);
  }}
  requestAnimationFrame(frame);
}}

// attività del calendario per id, con il giorno e la forma di quel giorno
const ACTS = {{}};
CALENDAR.weeks.forEach(w => w.days.forEach(d => d.acts.forEach(a => {{ ACTS[a.id] = Object.assign({{ date: d.date, form: d.form }}, a); }})));
const FEELS = ['Molto male', 'Male', 'Normale', 'Bene', 'Benissimo'];

function workoutHead(a, summary) {{
  const [y, m, d] = a.date.split('-').map(Number);
  const when = new Date(y, m - 1, d).toLocaleDateString('it-IT', {{ weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }});
  const chips = a.form ? '<div class="wk-chips"><span class="wk-chip ctl">Forma ' + a.form.ctl + '</span><span class="wk-chip atl">Fatica ' + a.form.atl + '</span><span class="wk-chip tsb">Freschezza ' + (a.form.tsb > 0 ? '+' : '') + a.form.tsb + '</span></div>' : '';
  const big = [a.dur ? '<b>' + fmtDur(a.dur) + '</b>' : '', a.km ? '<b>' + a.km + '</b> <span>km</span>' : '', a.load ? '<b>' + a.load + '</b> <span>carico</span>' : ''].filter(Boolean).map(x => '<div>' + x + '</div>').join('');
  const kind = a.kind === 'workout' ? ' · qualità' : a.kind === 'easy' ? ' · facile' : '';

  const rows = [
    ['Durata', summary.durata], ['Distanza', summary.distanza], ['Passo medio', summary.passo_medio],
    ['FC media', summary.fc_media], ['FC max', summary.fc_max], ['Cadenza (passi/min)', summary.cadenza_media],
    ['Dislivello', summary.dislivello], ['Calorie', summary.calorie],
    ['Carico', a.load || null], ['Effetto allenante aerobico', a.te],
  ].filter(r => r[1] && r[1] !== '—');
  const feel = a.feel === null || a.feel === undefined ? -1 : Math.round(a.feel / 25);
  const scale = feel >= 0 ? '<div class="wk-scale" role="img" aria-label="Sensazioni: ' + FEELS[feel] + '">' + FEELS.map((f, i) => '<span class="' + (i === feel ? 'on' : '') + '">' + f + '</span>').join('') + '</div>'
    : '<p class="muted">Non inserite sull’orologio.</p>';
  const rpe = a.rpe ? '<p><strong>' + Math.round(a.rpe / 10) + '</strong> su 10</p>' : '<p class="muted">Non inserito sull’orologio.</p>';

  return '<div class="wk-top"><div class="wk-date">' + when + ' · ' + a.time + '</div>' + chips + '</div>'
    + '<div class="wk-bar ' + a.group + '"><div class="wk-big">' + big + '</div><div class="wk-sport">' + esc(a.sport) + kind + '</div></div>'
    + '<div class="wk-cols"><div><h4>Dati dell’allenamento</h4><table class="wk-table"><tbody>'
    + rows.map(r => '<tr><td>' + r[0] + '</td><td>' + esc(r[1]) + '</td></tr>').join('') + '</tbody></table></div>'
    + '<div class="wk-side"><h4>Note</h4><p class="wk-desc">' + (a.desc ? esc(a.desc) : '<span class="muted">Nessuna nota su Garmin.</span>') + '</p>'
    + '<h4>Come ti sei sentito</h4>' + scale + '<h4>Sforzo percepito</h4>' + rpe + '</div></div>';
}}

function openDetail(activityId) {{
  const ts = ACTIVITY_TIMESERIES[activityId];
  const act = ACTS[activityId];
  if (!act) return;
  const summary = ACTIVITY_SUMMARY[activityId] || act.summary;
  document.getElementById('detailTitle').textContent = act.name;

  const body = document.getElementById('detailBody');
  body.innerHTML = workoutHead(act, summary) + '<h4 class="wk-more">Percorso e grafici</h4>';
  currentTs = ts || null;

  if (ts && ts.lat && ts.lat.length > 5) {{
    const wrap = document.createElement('div');
    wrap.className = 'route-wrap';
    wrap.innerHTML = buildRouteSvg(ts, act);
    body.appendChild(wrap);

    const replay = document.createElement('button');
    replay.className = 'replay-btn';
    replay.textContent = 'Rivedi animazione del percorso';
    replay.onclick = animateRoute;
    body.appendChild(replay);
  }} else {{
    const msg = document.createElement('div');
    msg.className = 'missing';
    msg.textContent = ts ? 'Nessuna traccia GPS per questa attività.' : 'Percorso e grafici ci sono solo per le ultime 4 settimane.';
    body.appendChild(msg);
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

    rhr_series, sleep_series, hrv_series, stress_series = fetch_daily_wellness(client, RECOVERY_LOOKBACK_DAYS)
    stress_day = fetch_stress_day(client)
    training_load = fetch_training_load(client)
    vo2_series = fetch_vo2max(client, WEEKS_TREND * 7, VO2_SAMPLE_EVERY_DAYS)

    print("  -> Scarico profilo, record, previsioni e gare in calendario...")
    profile = fetch_profile(client)
    profile["rhr"] = rhr_series[-1]["value"] if rhr_series else None
    if vo2_series:
        profile["vo2max"] = vo2_series[-1]["value"]
    predictions = fetch_race_predictions(client)
    races = fetch_races(client, date.today() - timedelta(weeks=CALENDAR_WEEKS_BACK + 1), RACE_LOOKAHEAD_MONTHS)
    goal = pick_goal(races)

    load_series = compute_ctl_atl_tsb(activities, LOAD_LOOKBACK_DAYS)
    load_series_display = load_series[-WEEKS_TREND * 7:]

    mileage = compute_weekly_mileage(running_activities, WEEKS_TREND)

    pb_5k_sec = next((r["seconds"] for r in profile["records"] if r["label"] == "5 km"), ATHLETE["pb_5k_sec"])
    pb_5k_pace_sec_km = pb_5k_sec / 5.0
    week_row = build_week_row(running_activities, pb_5k_pace_sec_km)

    today = date.today()
    insights = compute_insights(mileage, vo2_series)

    # percorso e grafici solo per le ultime settimane: ogni attività è una chiamata in più a Garmin
    recent_from = (date.today() - timedelta(weeks=ACTIVITY_TABLE_WEEKS)).isoformat()
    recent_activities_raw = [a for a in activities if (a.get("startTimeLocal") or "") >= recent_from]
    activity_details, activity_timeseries = fetch_activity_details(client, recent_activities_raw)
    activity_summary = {str(a.get("activityId")): build_activity_summary(activity_details.get(str(a.get("activityId")), a)) for a in recent_activities_raw}
    calendar = build_calendar(activities, load_series, races, pb_5k_pace_sec_km, date.fromisoformat(goal["date"]))

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
        n_weeks, avg = len(mileage["values"]), mileage["avg"]
        sky = "#7dd3fc"
        # azzurro sopra la tua media, grigio sotto; la settimana in corso è a metà: solo contorno tratteggiato
        fills = json.dumps([sky if v >= avg else "#3a3a3c" for v in mileage["values"][:-1]] + ["rgba(125,211,252,0.15)"])
        chart_scripts.append(f"""
try {{
new Chart(document.getElementById('mileageChart'), {{
  type: 'bar',
  data: {{ labels: {json.dumps(mileage['labels'])}, datasets: [
    {{ label: 'km', data: {json.dumps(mileage['values'])}, backgroundColor: {fills}, borderColor: '{sky}', borderWidth: {json.dumps([0] * (n_weeks - 1) + [1.5])}, borderDash: [4, 3], borderRadius: 5, order: 2 }},
    {{ label: 'media {avg:g} km', data: {json.dumps([avg] * n_weeks)}, type: 'line', borderColor: '#8e8e93', borderDash: [5, 4], borderWidth: 1.5, backgroundColor: 'transparent', pointRadius: 0, order: 1 }}
  ] }},
  options: {{ responsive: true, layout: {{ padding: {{ top: 18 }} }}, interaction: {{ mode: 'index', intersect: false }},
    plugins: {{ legend: {{ display: false }}, zones: [{{ from: {avg * 0.9:.1f}, to: {avg * 1.1:.1f}, color: 'rgba(255,255,255,0.06)' }}], barValueLabel: {{ show: true }},
      tooltip: {{ enabled: true, mode: 'index', intersect: false, callbacks: {{ label: (c) => c.datasetIndex ? 'media: ' + c.raw + ' km' : 'km: ' + c.raw }} }} }},
    scales: {{ x: {{ grid: {{ display: false }}, ticks: {{ maxRotation: 0, autoSkip: true }} }}, y: {{ grid: {{ color: '#2c2c2e' }}, beginAtZero: true }} }} }}
}});
}} catch(e) {{ console.error('Errore grafico mileageChart', e); }}""")
        chart_scripts.append(f"try {{ registerChart('mileage', {json.dumps(mileage['labels'])}, [{{label:'km settimanali', data:{json.dumps(mileage['values'])}, backgroundColor:'#7dd3fc'}}], 'bar', 'Chilometraggio settimanale', false, true); }} catch(e) {{ console.error('registerChart mileage', e); }}")

    stress_color = "(v) => v === null ? '#3a3a3c' : v <= 25 ? '#3b82f6' : v <= 50 ? '#f6ad55' : v <= 75 ? '#ed8936' : '#c05621'"
    if stress_day:
        chart_scripts.append(f"""
try {{
const sv = {json.dumps(stress_day['values'])}, sc = {stress_color};
new Chart(document.getElementById('stressDayChart'), {{
  type: 'bar',
  data: {{ labels: {json.dumps(stress_day['labels'])}, datasets: [{{ label: 'Stress', data: sv, backgroundColor: sv.map(sc), barPercentage: 1, categoryPercentage: 1 }}] }},
  options: {{ responsive: true, plugins: {{ legend: {{ display: false }}, tooltip: {{ enabled: true, mode: 'index', intersect: false }} }},
    scales: {{ x: {{ grid: {{ display: false }}, ticks: {{ maxRotation: 0, autoSkip: true, maxTicksLimit: 7 }} }}, y: {{ min: 0, max: 100, grid: {{ color: '#2c2c2e' }}, ticks: {{ stepSize: 25 }} }} }} }}
}});
}} catch(e) {{ console.error('Errore grafico stressDayChart', e); }}""")
    if stress_series:
        week = stress_series[-7:]
        chart_scripts.append(f"""
try {{
const wv = {json.dumps([p['value'] for p in week])}, sc = {stress_color};
new Chart(document.getElementById('stressWeekChart'), {{
  type: 'bar',
  data: {{ labels: {json.dumps([fmt_day(p['date']) for p in week])}, datasets: [{{ label: 'Stress medio', data: wv, backgroundColor: wv.map(sc), borderRadius: 5 }}] }},
  options: {{ responsive: true, layout: {{ padding: {{ top: 18 }} }}, plugins: {{ legend: {{ display: false }}, barValueLabel: {{ show: true }}, tooltip: {{ enabled: true, mode: 'index', intersect: false }} }},
    scales: {{ x: {{ grid: {{ display: false }} }}, y: {{ min: 0, max: 100, grid: {{ color: '#2c2c2e' }}, ticks: {{ stepSize: 25 }} }} }} }}
}});
}} catch(e) {{ console.error('Errore grafico stressWeekChart', e); }}""")

    if sleep_series:
        sleep_has_today =sleep_series[-1]["date"] == today.isoformat()
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

    if insights or load_series:
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

    now = datetime.now()
    generated_stamp = now.strftime("%Y%m%d%H%M")
    ctx = {
        "page_title": escape(goal["name"]),
        "generated_stamp": generated_stamp,
        "generated_label": now.strftime("%d/%m alle %H:%M"),
        "tsb_now": json.dumps(load_series[-1]["tsb"] if load_series else None),
        "profile_json": json_for_script(profile),
        "predictions_json": json_for_script(predictions),
        "goal_json": json_for_script(goal),
        "calendar_json": json_for_script(calendar),
        "weeks_trend": WEEKS_TREND, "recovery_weeks": RECOVERY_LOOKBACK_DAYS // 7,
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
        "stress_day_title": ("Oggi" if stress_day["date"] == today.isoformat() else "Ieri") if stress_day else "Oggi",
        "stress_day_chart_or_missing": render_or_missing(bool(stress_day), canvas("stressDayChart", f"Stress durante il giorno, media {stress_day['avg'] if stress_day else '—'}"), "Nessun dato di stress trovato."),
        "stress_week_chart_or_missing": render_or_missing(bool(stress_series), canvas("stressWeekChart", f"Stress medio degli ultimi 7 giorni, ultimo valore {last(stress_series)}"), "Nessun dato di stress trovato."),
        "training_load_html": build_training_load_html(training_load),
        "chart_scripts": "\n".join(chart_scripts),
        "chartjs_source": chartjs_source,
        "activity_timeseries_json": json_for_script(activity_timeseries),
        "activity_summary_json": json_for_script(activity_summary),
    }

    html = HTML_TEMPLATE.format(**ctx)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    # la pagina online lo legge all'apertura: se è più nuovo di lei, si ricarica da sola
    with open(os.path.join(os.path.dirname(OUTPUT_HTML), "version.txt"), "w", encoding="utf-8") as f:
        f.write(generated_stamp)

    print(f"\n✅ Fatto. Pagina generata in: {OUTPUT_HTML}")
    print("   Apri il file nel browser (doppio click).")


if __name__ == "__main__":
    main()
