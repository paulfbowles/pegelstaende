"""
fetch_pegel.py
--------------
Ruft aktuelle Pegelstaende binnenschifffahrtsrelevanter Wasserstrassen
von der PEGELONLINE-API ab und speichert sie als CSV in data/pegel.csv.

Anschliessend wird die Datawrapper-Karte neu publiziert (optional).

Umgebungsvariablen (GitHub Secrets):
  DATAWRAPPERPEGELKARTE   – Datawrapper API-Schluessel
  DW_CHART_ID  – ID der Datawrapper-Karte (z. B. "aBcDe")
"""

import os
import sys
import requests
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

BASE_URL = "https://www.pegelonline.wsv.de/webservices/rest-api/v2"

RELEVANTE_GEWAESSER = [
    "RHEIN", "ELBE", "WESER", "ODER", "MAIN", "MOSEL", "NECKAR",
    "DONAU", "SAAR", "LAHN", "NAHE", "SAALE",
    "HAVEL", "UNTERE HAVEL-WASSERSTRASSE", "OBERE HAVEL-WASSERSTRASSE",
    "SPREE", "SPREE-ODER-WASSERSTRASSE",
    "MITTELLANDKANAL", "DORTMUND-EMS-KANAL", "RHEIN-HERNE-KANAL",
    "WESEL-DATTELN-KANAL", "NORD-OSTSEE-KANAL", "ELBE-SEITEN-KANAL",
    "ELBE-LUEBECK-KANAL", "TELTOWKANAL", "ODER-HAVEL-KANAL",
    "ODER-SPREE-KANAL", "HAVEL-ODER-WASSERSTRASSE",
    "DAHME", "MUERITZ-ELDE-WASSERSTRASSE",
    "ALLER", "EMS",
]

OUTPUT_PATH = Path("data/pegel.csv")

# ---------------------------------------------------------------------------
# 1. Stationsdaten laden
# ---------------------------------------------------------------------------

def lade_stationen() -> list:
    url = f"{BASE_URL}/stations.json?includeTimeseries=true&includeCurrentMeasurement=true"
    print(f"[{_now()}] Lade Stationsdaten von PEGELONLINE ...")
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    daten = r.json()
    print(f"[{_now()}] {len(daten)} Stationen geladen.")
    return daten

# ---------------------------------------------------------------------------
# 2. JSON → DataFrame
# ---------------------------------------------------------------------------

def parse_stationen(stations_raw: list) -> pd.DataFrame:
    records = []
    for s in stations_raw:
        wasserstand_cm = None
        ts_unit = None
        trend = None
        statuskennz = None

        for ts in s.get("timeseries", []):
            if ts.get("shortname") == "W":
                ts_unit = ts.get("unit")
                cm = ts.get("currentMeasurement") or {}
                wasserstand_cm = cm.get("value")
                trend = cm.get("trend")
                # API liefert teils String ("normal","low","high"), teils int (-1,0,1)
                statuskennz = cm.get("stateMnwMhw")
                break

        records.append({
            "uuid":           s.get("uuid"),
            "stationsname":   s.get("longname", s.get("shortname", "")),
            "gewaesser":      s.get("water", {}).get("longname", ""),
            "km":             s.get("km"),
            "lat":            s.get("latitude"),
            "lon":            s.get("longitude"),
            "wasserstand_cm": wasserstand_cm,
            "einheit":        ts_unit,
            "trend":          trend,
            "status_mnw_mhw": statuskennz,
        })

    return pd.DataFrame(records)

# ---------------------------------------------------------------------------
# 3. Filtern
# ---------------------------------------------------------------------------

def filtere(df_all: pd.DataFrame) -> pd.DataFrame:
    gewaesser_upper = {g.upper() for g in RELEVANTE_GEWAESSER}
    mask = (
        df_all["gewaesser"].str.upper().isin(gewaesser_upper)
        & df_all["lat"].notna()
        & df_all["lon"].notna()
        & df_all["wasserstand_cm"].notna()
    )
    df = df_all[mask].copy().reset_index(drop=True)
    print(f"[{_now()}] Nach Filter: {len(df)} Stationen.")
    return df

# ---------------------------------------------------------------------------
# 4. Klassifizieren
#    API-Feld stateMnwMhw kann sein:
#      int:    -1 (unter MNW), 0 (normal), 1 (ueber MHW)
#      string: "low", "normal", "high"  (neuere API-Versionen)
#      None:   unbekannt
# ---------------------------------------------------------------------------

STATUS_MAP_INT = {-1: "Niedrigwasser", 0: "Normal", 1: "Hochwasser"}
STATUS_MAP_STR = {"low": "Niedrigwasser", "normal": "Normal", "high": "Hochwasser"}

def klassifiziere(val) -> str:
    if val is None:
        return "Unbekannt"
    if isinstance(val, int):
        return STATUS_MAP_INT.get(val, "Unbekannt")
    if isinstance(val, str):
        return STATUS_MAP_STR.get(val.lower(), "Unbekannt")
    # float (z. B. -1.0)
    try:
        return STATUS_MAP_INT.get(int(val), "Unbekannt")
    except (ValueError, TypeError):
        return "Unbekannt"

TREND_MAP = {-1: "fallend", 0: "stabil", 1: "steigend"}

def trend_label(val) -> str:
    if val is None:
        return ""
    try:
        return TREND_MAP.get(int(val), "")
    except (ValueError, TypeError):
        return str(val)

def bereichere(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["status_label"] = df["status_mnw_mhw"].apply(klassifiziere)
    df["trend_label"]  = df["trend"].apply(trend_label)
    df["abgerufen_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return df

# ---------------------------------------------------------------------------
# 5. Exportieren
# ---------------------------------------------------------------------------

EXPORT_COLS = [
    "stationsname", "gewaesser", "km",
    "lat", "lon",
    "wasserstand_cm", "einheit",
    "trend_label", "status_label",
    "abgerufen_utc",
]

def exportiere(df: pd.DataFrame, pfad: Path) -> None:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    df[EXPORT_COLS].to_csv(pfad, index=False, encoding="utf-8-sig")
    print(f"[{_now()}] CSV gespeichert: {pfad}  ({len(df)} Zeilen)")

# ---------------------------------------------------------------------------
# 6. Datawrapper republizieren (optional)
# ---------------------------------------------------------------------------

def republiziere_datawrapper() -> None:
    api_key  = os.environ.get("DATAWRAPPERPEGELKARTE", "")
    chart_id = os.environ.get("DW_CHART_ID", "")

    if not api_key or not chart_id:
        print("[INFO] DATAWRAPPERPEGELKARTE oder DW_CHART_ID nicht gesetzt – kein Republish.")
        return

    url = f"https://api.datawrapper.de/v3/charts/{chart_id}/publish"
    headers = {"Authorization": f"Bearer {api_key}"}
    r = requests.post(url, headers=headers, timeout=30)

    if r.status_code in (200, 204):
        print(f"[{_now()}] Datawrapper-Karte '{chart_id}' erfolgreich republiziert.")
    else:
        print(f"[WARNUNG] Datawrapper-Republish fehlgeschlagen: {r.status_code} {r.text[:200]}")

# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")

# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        stations_raw = lade_stationen()
        df_all = parse_stationen(stations_raw)
        df     = filtere(df_all)
        df     = bereichere(df)
        exportiere(df, OUTPUT_PATH)
        republiziere_datawrapper()
        print(f"[{_now()}] Fertig.")
    except Exception as e:
        print(f"[FEHLER] {e}", file=sys.stderr)
        sys.exit(1)
