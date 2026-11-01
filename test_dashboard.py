"""Controlli veloci sulla logica di date del calendario e sulla scelta dell'obiettivo.

Non serve Garmin: usa dati finti. Lancia con:  python test_dashboard.py
"""

from datetime import date, timedelta

import dashboard as d

today = date.today()
last_sunday = today - timedelta(days=today.weekday() + 1)


def run(day, km, name="Corsa"):
    return {"activityId": hash((day, name)), "activityName": name, "startTimeLocal": f"{day} 18:00:00",
            "distance": km * 1000, "duration": km * 360, "activityType": {"typeKey": "running"}}


def test_calendar_weeks():
    gym = {"activityId": 1, "activityName": "Pesi", "startTimeLocal": f"{last_sunday} 10:00:00",
           "distance": 0, "duration": 2700, "activityType": {"typeKey": "strength_training"}}
    cal = d.build_calendar([run(last_sunday, 10), gym], [], [], 330, today + timedelta(days=10))
    for w in cal["weeks"]:
        assert len(w["days"]) == 7
        assert date.fromisoformat(w["days"][0]["date"]).weekday() == 0, "ogni settimana inizia di lunedì"
    week = next(w for w in cal["weeks"] if w["days"][6]["date"] == last_sunday.isoformat())
    assert len(week["days"][6]["acts"]) == 2, "le attività di domenica vanno nella domenica della loro settimana"
    assert week["run_km"] == 10 and week["km"] == 10, "la palestra non conta nei km di corsa"
    assert date.fromisoformat(cal["weeks"][-1]["days"][6]["date"]) >= today + timedelta(days=10), "arriva fino alla gara"


def test_goal():
    past = {"date": (today - timedelta(days=3)).isoformat(), "name": "Passata", "km": 10, "primary": True, "race": True}
    soon = {"date": (today + timedelta(days=5)).isoformat(), "name": "Vicina", "km": 5, "primary": False, "race": True}
    main = {"date": (today + timedelta(days=40)).isoformat(), "name": "Principale", "km": 21.0975, "primary": True, "race": True}
    assert d.pick_goal([past, soon, main])["name"] == "Principale", "la gara principale futura vince"
    assert d.pick_goal([past, soon])["name"] == "Vicina", "senza principale, la prima gara futura"
    assert d.pick_goal([past])["name"] == d.RACE_NAME, "nessuna gara futura: si usa la configurazione"


if __name__ == "__main__":
    test_calendar_weeks()
    test_goal()
    print("Tutti i controlli passati.")
