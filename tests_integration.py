"""
Prueba de integración sin PostgreSQL.

Reemplaza la capa app.db por un doble respaldado en SQLite (misma interfaz
pública) y levanta un SCADA falso en localhost. Ejercita:
  fetch -> build_snapshot -> write_samples/sync_alarms/write_snapshot
        -> read_snapshot (/api/state) -> read_trend (/api/trend)

La lógica SQL específica de Postgres (LISTEN/NOTIFY, to_timestamp) se valida
aparte; acá se valida el FLUJO y la forma de los datos.
"""
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, ".")
from app.normalize import build_snapshot, relay_datetime  # noqa: E402

# --- Datos de prueba (subconjunto real) ---
RAW_FEEDERS = [
    {"mode":1,"id":"A0","ir":59,"is":60,"it":61,"vrs":13.459,"vst":13.458,"vtr":13.545,"p":1.376,"q":0.255,"fr":50.039,"fPmed":0.983,"seca_cerrado":True,"secb_cerrado":False,"int52_cerrado":True,"int52_extraido":False,"pat_cerrado":False,"connection_State":0,"alarmList":[]},
    {"mode":1,"id":"A9","ir":208,"is":212,"it":211,"vrs":13.449,"vst":13.478,"vtr":13.488,"p":4.862,"q":0.986,"fr":50.039,"fPmed":0.98,"seca_cerrado":False,"secb_cerrado":True,"int52_cerrado":True,"int52_extraido":False,"pat_cerrado":False,"connection_State":0,"alarmList":[]},
    {"mode":1,"id":"A16","ir":1,"is":1,"it":0,"vrs":0,"vst":0,"vtr":0,"p":0,"q":0,"fr":0,"fPmed":1,"seca_cerrado":False,"secb_cerrado":False,"int52_cerrado":False,"int52_extraido":True,"pat_cerrado":True,"connection_State":0,"alarmList":[{"eventBitNumber":128,"alarm_active":True,"alarm_ack":False,"datetime":836386280430}]},
    {"mode":2,"id":"SA2","ir":4,"is":4,"it":4,"vrs":13.468,"vst":13.449,"vtr":13.536,"p":0.052,"q":0.086,"fr":50.048,"fPmed":0.511,"seca_cerrado":True,"secb_cerrado":False,"int52_cerrado":True,"int52_extraido":False,"pat_cerrado":False,"connection_State":0,"alarmList":[]},
    {"mode":1,"id":"AC","ir":0,"is":0,"it":0,"vrs":13.478,"vst":13.497,"vtr":13.536,"p":0,"q":0,"fr":50.042,"fPmed":0.455,"seca_cerrado":True,"secb_cerrado":True,"int52_cerrado":True,"int52_extraido":False,"pat_cerrado":False,"connection_State":0,"alarmList":[]},
]
RAW_GENS = [
    {"id":"TG1","irl":0,"isl":0.4,"itl":0,"irn":0.4,"isn":0.4,"itn":0.4,"vr":7788,"vs":7788,"vt":7788,"p":0,"q":0,"s":0,"fPmed":0,"fr":50.03,"ctRf":200,"ctRn":100,"seca_cerrado":False,"secb_cerrado":True,"int52_cerrado":True,"int52_extraido":False,"intPie_cerrado":False,"intCe_cerrado":False,"estProt_1":0,"estProt_2":0,"estProt_3":2048,"estProt_4":0,"connection_State":0},
    {"id":"TG3","irl":771,"isl":777,"itl":779.4,"irn":769.5,"isn":776.4,"itn":778.8,"vr":7776,"vs":7776,"vt":7776,"p":17204966,"q":5659825,"s":18039284,"fPmed":0.95,"fr":50.03,"ctRf":300,"ctRn":1,"seca_cerrado":False,"secb_cerrado":True,"int52_cerrado":True,"int52_extraido":False,"intPie_cerrado":True,"intCe_cerrado":False,"estProt_1":0,"estProt_2":0,"estProt_3":0,"estProt_4":0,"connection_State":0},
    {"id":"TG4","irl":0,"isl":0,"itl":0.8,"irn":1206.4,"isn":1217.2,"itn":1216,"vr":7788,"vs":7800,"vt":7788,"p":27802052,"q":6089973,"s":28344222,"fPmed":0.98,"fr":50.05,"ctRf":400,"ctRn":10,"seca_cerrado":True,"secb_cerrado":False,"int52_cerrado":True,"int52_extraido":False,"intPie_cerrado":True,"intCe_cerrado":False,"estProt_1":0,"estProt_2":0,"estProt_3":0,"estProt_4":0,"connection_State":0},
]


# --- Doble de app.db respaldado en SQLite ---
class SqliteDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE current_snapshot (id INTEGER PRIMARY KEY, ts TEXT, payload TEXT);
            CREATE TABLE gen_sample (ts TEXT, tg_id TEXT, p_mw REAL, q_mvar REAL, s_mva REAL, fp REAL, i_linea REAL, i_neutro REAL, running INTEGER, PRIMARY KEY(ts,tg_id));
            CREATE TABLE feeder_sample (ts TEXT, feeder_id TEXT, p_mw REAL, q_mvar REAL, i_max REAL, v_ll REAL, fp REAL, fr REAL, closed INTEGER, bus TEXT, state TEXT, PRIMARY KEY(ts,feeder_id));
            CREATE TABLE alarm_event (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ingest TEXT DEFAULT (datetime('now')), ts_relay TEXT, cell_id TEXT, event_bit INTEGER, active INTEGER, ack INTEGER);
            CREATE TABLE alarm_state (cell_id TEXT, event_bit INTEGER, active INTEGER, ack INTEGER, ts_relay TEXT, PRIMARY KEY(cell_id,event_bit));
        """)
        self.notifies = 0

    def write_snapshot(self, snapshot):
        self.conn.execute("INSERT OR REPLACE INTO current_snapshot (id, ts, payload) VALUES (1, ?, ?)",
                          (snapshot["ts"], json.dumps(snapshot)))
        self.conn.commit()
        self.notifies += 1  # equivalente a NOTIFY

    def write_samples(self, snapshot):
        ts = snapshot["ts"]
        for g in snapshot["gens"]:
            self.conn.execute("INSERT OR IGNORE INTO gen_sample VALUES (?,?,?,?,?,?,?,?,?)",
                (ts, g["id"], g["pMW"], g["qMVAr"], g["sMVA"], g["fp"], g["iL"], g["iN"], int(g["running"])))
        for f in snapshot["feeders"]:
            self.conn.execute("INSERT OR IGNORE INTO feeder_sample VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ts, f["id"], f["p"], f["q"], f["imax"], f["vll"], f["fp"], f["fr"], int(f["closed"]), f["bus"], f["state"]))
        self.conn.commit()

    def sync_alarms(self, snapshot):
        changes = 0
        for f in snapshot["feeders"]:
            for a in f.get("alarms", []):
                bit = a.get("eventBitNumber")
                if bit is None:
                    continue
                active = int(bool(a.get("alarm_active")))
                ack = int(bool(a.get("alarm_ack")))
                ts_relay = relay_datetime(a["datetime"]).isoformat() if a.get("datetime") else None
                row = self.conn.execute("SELECT active, ack FROM alarm_state WHERE cell_id=? AND event_bit=?",
                                        (f["id"], bit)).fetchone()
                if row is None or row["active"] != active or row["ack"] != ack:
                    self.conn.execute("INSERT INTO alarm_event (ts_relay,cell_id,event_bit,active,ack) VALUES (?,?,?,?,?)",
                                      (ts_relay, f["id"], bit, active, ack))
                    self.conn.execute("INSERT OR REPLACE INTO alarm_state VALUES (?,?,?,?,?)",
                                      (f["id"], bit, active, ack, ts_relay))
                    changes += 1
        self.conn.commit()
        return changes

    def read_snapshot(self):
        row = self.conn.execute("SELECT payload FROM current_snapshot WHERE id=1").fetchone()
        return json.loads(row["payload"]) if row else None

    def read_trend(self, range_seconds, max_points):
        rows = self.conn.execute("SELECT ts, tg_id, p_mw, running FROM gen_sample ORDER BY ts ASC").fetchall()
        times, ts_index, series = [], {}, {}
        for r in rows:
            if r["ts"] not in ts_index:
                ts_index[r["ts"]] = len(times); times.append(r["ts"])
            series.setdefault(r["tg_id"], {})
        for tg in series:
            series[tg] = [None]*len(times)
        for r in rows:
            series[r["tg_id"]][ts_index[r["ts"]]] = round(r["p_mw"], 4)
        return {"ts": times, "series": series, "bucket_seconds": 1}

    def read_feeder_trend(self, feeder_id, range_seconds, max_points):
        rows = self.conn.execute(
            "SELECT ts, p_mw, q_mvar, i_max, v_ll, fp FROM feeder_sample WHERE feeder_id=? ORDER BY ts ASC",
            (feeder_id,)).fetchall()
        times, series = [], {"p": [], "q": [], "imax": [], "vll": [], "fp": []}
        rnd = lambda v, nd: (round(v, nd) if v is not None else None)
        for r in rows:
            times.append(r["ts"])
            series["p"].append(rnd(r["p_mw"], 4))
            series["q"].append(rnd(r["q_mvar"], 4))
            series["imax"].append(rnd(r["i_max"], 1))
            series["vll"].append(rnd(r["v_ll"], 4))
            series["fp"].append(rnd(r["fp"], 4))
        return {"id": feeder_id, "ts": times, "series": series, "bucket_seconds": 1}

    def read_recent_alarms(self, limit=50):
        rows = self.conn.execute("SELECT * FROM alarm_event ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"cell": r["cell_id"], "eventBit": r["event_bit"], "active": bool(r["active"]), "ack": bool(r["ack"]), "tsRelay": r["ts_relay"]} for r in rows]


def run():
    dbx = SqliteDB()
    print("=== Simulando 5 ciclos de poll ===")
    for cycle in range(5):
        ts = datetime.now(timezone.utc) + timedelta(seconds=cycle*5)
        # jitter en la potencia para simular variación
        gens = [dict(g) for g in RAW_GENS]
        gens[1]["p"] = int(17204966 * (1 + 0.01*cycle))  # TG3 sube de a poco
        snap = build_snapshot(RAW_FEEDERS, gens, ts=ts)
        dbx.write_samples(snap)
        ch = dbx.sync_alarms(snap)
        dbx.write_snapshot(snap)
        print(f"  ciclo {cycle}: gen {snap['totals']['genMW']:.2f} MW, {ch} cambios alarma, {dbx.notifies} notifies acum.")

    print("\n=== /api/state (último snapshot) ===")
    s = dbx.read_snapshot()
    assert s is not None, "read_snapshot devolvió None"
    assert len(s["gens"]) == 3 and len(s["feeders"]) == 5
    assert s["totals"]["gensRunning"] == 2
    print(f"  OK: {len(s['gens'])} gens, {len(s['feeders'])} feeders, {s['totals']['gensRunning']} en servicio")

    # Nombres de línea inyectados desde celdas.json (dato de referencia)
    by_id = {f["id"]: f for f in s["feeders"]}
    assert by_id["A0"]["name"] == "Chacra 11", by_id["A0"]["name"]
    assert by_id["A9"]["name"] == "Irigoyen", by_id["A9"]["name"]
    assert by_id["AC"]["name"] == "Acoplamiento", by_id["AC"]["name"]
    gen_by_id = {g["id"]: g for g in s["gens"]}
    assert gen_by_id["TG3"]["name"] == "Generador 3", gen_by_id["TG3"]["name"]
    print(f"  OK nombres: A0={by_id['A0']['name']!r}, A9={by_id['A9']['name']!r}, TG3={gen_by_id['TG3']['name']!r}")
    print(f"  buses A: p={s['buses']['A']['p']:.3f} MW  B: p={s['buses']['B']['p']:.3f} MW")

    print("\n=== /api/trend ===")
    tr = dbx.read_trend(3600, 600)
    assert len(tr["ts"]) == 5, f"esperaba 5 timestamps, hay {len(tr['ts'])}"
    assert set(tr["series"].keys()) == {"TG1","TG3","TG4"}
    tg3 = tr["series"]["TG3"]
    assert tg3[0] < tg3[-1], "TG3 debía crecer entre ciclos"
    print(f"  OK: {len(tr['ts'])} puntos, series {list(tr['series'].keys())}")
    print(f"  TG3 evolución: {[round(v,2) for v in tg3]}")
    print(f"  TG1 (detenido): {[round(v,2) for v in tr['series']['TG1']]}")

    print("\n=== /api/trend/feeder ===")
    ft = dbx.read_feeder_trend("A0", 3600, 600)
    assert len(ft["ts"]) == 5, f"esperaba 5 puntos para A0, hay {len(ft['ts'])}"
    assert set(ft["series"].keys()) == {"p", "q", "imax", "vll", "fp"}
    assert all(v is not None for v in ft["series"]["p"]), "A0 (en servicio) no debería tener P nula"
    assert ft["series"]["imax"][0] == 61, f"I máx de A0 debía ser 61 A, es {ft['series']['imax'][0]}"
    print(f"  OK: {len(ft['ts'])} puntos para A0 · P={[round(v,2) for v in ft['series']['p']]}")
    print(f"  I máx={ft['series']['imax']}  U={[round(v,2) for v in ft['series']['vll']]}")

    print("\n=== /api/alarms ===")
    al = dbx.read_recent_alarms()
    assert len(al) == 1, f"esperaba 1 evento (A16 bit128 sin ack), hay {len(al)}"
    assert al[0]["cell"] == "A16" and al[0]["eventBit"] == 128 and al[0]["active"] and not al[0]["ack"]
    print(f"  OK: {len(al)} evento — {al[0]['cell']} bit {al[0]['eventBit']} activa/sin-ack")
    print(f"  ts_relay decodificado: {al[0]['tsRelay']}")

    print("\n=== Alarma NO se duplica si no cambia ===")
    before = len(dbx.read_recent_alarms())
    snap = build_snapshot(RAW_FEEDERS, RAW_GENS, ts=datetime.now(timezone.utc)+timedelta(seconds=100))
    dbx.sync_alarms(snap)
    after = len(dbx.read_recent_alarms())
    assert before == after, f"la alarma se duplicó ({before} -> {after})"
    print(f"  OK: sigue habiendo {after} evento (no se duplicó)")

    print("\n=== Alarma SÍ registra un evento nuevo al reconocerse ===")
    acked = [dict(f) for f in RAW_FEEDERS]
    for f in acked:
        if f["id"] == "A16":
            f["alarmList"] = [dict(f["alarmList"][0], alarm_ack=True)]
    snap = build_snapshot(acked, RAW_GENS, ts=datetime.now(timezone.utc)+timedelta(seconds=105))
    ch = dbx.sync_alarms(snap)
    after2 = len(dbx.read_recent_alarms())
    assert ch == 1 and after2 == after + 1, f"esperaba 1 evento nuevo por ack (ch={ch})"
    print(f"  OK: {ch} evento nuevo por reconocimiento, total {after2}")

    print("\n*** TODAS LAS ASERCIONES PASARON ***")


if __name__ == "__main__":
    run()
