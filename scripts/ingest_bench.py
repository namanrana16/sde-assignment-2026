"""Dev tool: replay 5 real load-test chunks into a copy of a finished load-test state DB and time the ingest.

Needs runs/load_before/{state,sim_run} (a finished scripts/load_test.py run copied there). Env: DROP_IDX=1 drops the
calls_lead index, CKPT=<pages> sets wal_autocheckpoint, any arg turns on cProfile caller output.
"""
import sys, time, cProfile, pstats, shutil, pathlib, os, sqlite3
sys.path.insert(0, '.')
from controller.store import Store
from controller.ingest import Ingestor
src = pathlib.Path('runs/load_before/state'); db = next(src.glob('*.db'))
d = pathlib.Path('runs/ib2'); shutil.rmtree(d, ignore_errors=True); d.mkdir()
for e in ('', '-wal', '-shm'):
    p = pathlib.Path(str(db) + e)
    if p.exists(): shutil.copy(p, d / (db.name + e))
st = Store(d / db.name)
E0 = 9629557
st.conn.execute("DELETE FROM calls WHERE event_id>?", (E0,))
if os.environ.get("DROP_IDX"): st.conn.execute("DROP INDEX calls_lead")
if os.environ.get("CKPT"): st.conn.execute(f"PRAGMA wal_autocheckpoint={os.environ['CKPT']}")
st.conn.execute("UPDATE ingest_cursor SET last_chunk=261, last_event_id=? WHERE id=1", (E0,))
ev = pathlib.Path('runs/load_before/sim_run/events')
class C:
    def get_chunk_text(self, n): return open(ev / f"{n:06d}.jsonl", encoding='utf-8').read()
    def get_config(self, w): raise RuntimeError
ing = Ingestor(C(), st)
tot = 0; t0 = time.perf_counter()
pr = cProfile.Profile() if len(sys.argv) > 1 else None
for n in range(262, 267):
    meta = {"chunk": n, "first_event_id": 0, "last_event_id": 10**9}
    if pr: pr.enable()
    s = ing.process_chunk(meta)
    if pr: pr.disable()
    tot += s.events
dt = time.perf_counter() - t0
print(f"{tot} events in {dt:.2f}s = {tot/dt:.0f} ev/s on a {db.stat().st_size/2**20:.0f} MB db")
if pr: pstats.Stats(pr).print_callers("execute")
