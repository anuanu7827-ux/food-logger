"""
Food Logger - barcode food logging with daily/weekly nutrition summaries.

Run:  pip install flask  &&  python app.py   ->  http://127.0.0.1:5000
"""
import json
import os
import sqlite3
import urllib.request
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, Response, g, jsonify, request

DB_PATH = os.environ.get("FOODLOG_DB", "foodlog.db")
MAX_PER_FOOD_PER_DAY = 3
USER_ID = 1  # demo: single user. Swap for real auth (session/JWT) in production.

app = Flask(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS foods (
  id INTEGER PRIMARY KEY,
  barcode TEXT UNIQUE,
  name TEXT NOT NULL,
  serving_g REAL NOT NULL DEFAULT 100,
  kcal REAL NOT NULL, carbs_g REAL NOT NULL,
  protein_g REAL NOT NULL, fat_g REAL NOT NULL,
  source TEXT NOT NULL DEFAULT 'manual'
);
CREATE TABLE IF NOT EXISTS food_logs (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  food_id INTEGER NOT NULL REFERENCES foods(id),
  food_name TEXT NOT NULL,
  servings REAL NOT NULL,
  kcal REAL NOT NULL, carbs_g REAL NOT NULL,
  protein_g REAL NOT NULL, fat_g REAL NOT NULL,
  logged_at TEXT NOT NULL,          -- ISO timestamp with offset
  local_date TEXT NOT NULL,         -- user's local day (YYYY-MM-DD)
  idempotency_key TEXT NOT NULL,
  UNIQUE (user_id, food_id, logged_at),        -- no duplicate timestamps
  UNIQUE (user_id, idempotency_key)            -- safe client retries
);
CREATE INDEX IF NOT EXISTS idx_logs_user_time ON food_logs(user_id, logged_at DESC);
CREATE INDEX IF NOT EXISTS idx_logs_user_date ON food_logs(user_id, local_date);
CREATE TABLE IF NOT EXISTS food_daily_counts (
  user_id INTEGER NOT NULL, food_id INTEGER NOT NULL, local_date TEXT NOT NULL,
  count INTEGER NOT NULL CHECK (count <= 3),
  PRIMARY KEY (user_id, food_id, local_date)
);
CREATE TABLE IF NOT EXISTS goals (
  user_id INTEGER PRIMARY KEY,
  daily_kcal REAL NOT NULL,
  alert_threshold_pct REAL NOT NULL DEFAULT 80
);
CREATE TABLE IF NOT EXISTS alerts_sent (
  user_id INTEGER NOT NULL, local_date TEXT NOT NULL, level TEXT NOT NULL,
  PRIMARY KEY (user_id, local_date, level)
);
"""

SEED_FOODS = [
    ("1234567890123", "Demo Oat Bar", 40, 150, 24, 3, 5),
    ("1111111111116", "Demo Banana", 118, 105, 27, 1.3, 0.4),
    ("2222222222222", "Demo Greek Yogurt", 170, 100, 6, 17, 0.7),
]


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    for b, n, s, k, c, p, f in SEED_FOODS:
        db.execute(
            "INSERT OR IGNORE INTO foods(barcode,name,serving_g,kcal,carbs_g,protein_g,fat_g,source) "
            "VALUES (?,?,?,?,?,?,?, 'seed')", (b, n, s, k, c, p, f))
    db.execute("INSERT OR IGNORE INTO goals(user_id, daily_kcal) VALUES (?, 2000)", (USER_ID,))
    db.commit()
    db.close()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, isolation_level=None)  # manual transactions
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db:
        db.close()


def user_tz():
    try:
        return ZoneInfo(request.headers.get("X-TZ", "UTC"))
    except Exception:
        return ZoneInfo("UTC")


def err(msg, code=400):
    return jsonify(error=msg), code


def num(v, name, lo=0, hi=100000):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    if not lo <= x <= hi:
        raise ValueError(f"{name} out of range")
    return x


# ---------------------------------------------------------------- foods
def fetch_open_food_facts(code):
    url = f"https://world.openfoodfacts.org/api/v2/product/{code}.json"
    req = urllib.request.Request(url, headers={"User-Agent": "FoodLogger/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.load(r)
    except Exception:
        return None
    if data.get("status") != 1:
        return None
    p = data["product"]
    n = p.get("nutriments", {})
    per100 = lambda key: float(n.get(f"{key}_100g") or 0)
    name = p.get("product_name") or "Unknown product"
    if p.get("brands"):
        name = f"{name} ({p['brands'].split(',')[0]})"
    # Normalise to a 100 g serving; user picks how many servings.
    return dict(name=name, serving_g=100, kcal=per100("energy-kcal"),
                carbs_g=per100("carbohydrates"), protein_g=per100("proteins"),
                fat_g=per100("fat"), source="openfoodfacts")


@app.get("/api/foods/barcode/<code>")
def food_by_barcode(code):
    code = code.strip()
    if not code.isdigit() or not 6 <= len(code) <= 14:
        return err("Invalid barcode")
    db = get_db()
    row = db.execute("SELECT * FROM foods WHERE barcode=?", (code,)).fetchone()
    if row:
        return jsonify(food=dict(row))
    f = fetch_open_food_facts(code)
    if not f:
        return err("Product not found. Please add it manually.", 404)
    cur = db.execute(
        "INSERT INTO foods(barcode,name,serving_g,kcal,carbs_g,protein_g,fat_g,source) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (code, f["name"], f["serving_g"], f["kcal"], f["carbs_g"], f["protein_g"], f["fat_g"], f["source"]))
    f.update(id=cur.lastrowid, barcode=code)
    return jsonify(food=f)


@app.get("/api/foods")
def search_foods():
    q = f"%{request.args.get('q', '').strip()}%"
    rows = get_db().execute(
        "SELECT * FROM foods WHERE name LIKE ? ORDER BY name LIMIT 20", (q,)).fetchall()
    return jsonify(foods=[dict(r) for r in rows])


@app.post("/api/foods")
def create_food():
    d = request.get_json(force=True)
    try:
        name = (d.get("name") or "").strip()
        if not name:
            raise ValueError("name is required")
        vals = (name, num(d.get("serving_g", 100), "serving_g", 1, 5000),
                num(d.get("kcal"), "kcal", 0, 5000), num(d.get("carbs_g", 0), "carbs_g"),
                num(d.get("protein_g", 0), "protein_g"), num(d.get("fat_g", 0), "fat_g"))
    except ValueError as e:
        return err(str(e))
    barcode = (d.get("barcode") or "").strip() or None
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO foods(name,serving_g,kcal,carbs_g,protein_g,fat_g,barcode) VALUES (?,?,?,?,?,?,?)",
            vals + (barcode,))
    except sqlite3.IntegrityError:
        return err("A food with that barcode already exists", 409)
    row = db.execute("SELECT * FROM foods WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(food=dict(row)), 201


# ---------------------------------------------------------------- goal + alerts
def get_goal(db):
    return db.execute("SELECT * FROM goals WHERE user_id=?", (USER_ID,)).fetchone()


@app.get("/api/goal")
def read_goal():
    return jsonify(goal=dict(get_goal(get_db())))


@app.put("/api/goal")
def set_goal():
    d = request.get_json(force=True)
    try:
        kcal = num(d.get("daily_kcal"), "daily_kcal", 500, 10000)
        pct = num(d.get("alert_threshold_pct", 80), "alert_threshold_pct", 10, 100)
    except ValueError as e:
        return err(str(e))
    get_db().execute(
        "INSERT INTO goals(user_id,daily_kcal,alert_threshold_pct) VALUES (?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET daily_kcal=excluded.daily_kcal, "
        "alert_threshold_pct=excluded.alert_threshold_pct", (USER_ID, kcal, pct))
    return jsonify(goal=dict(get_goal(get_db())))


def day_total(db, local_date):
    return db.execute(
        "SELECT COALESCE(SUM(kcal),0) FROM food_logs WHERE user_id=? AND local_date=?",
        (USER_ID, local_date)).fetchone()[0]


def status_for(total, goal):
    pct = 100 * total / goal["daily_kcal"]
    if pct > 100:
        return "over", pct
    if pct >= goal["alert_threshold_pct"]:
        return "warn", pct
    return "ok", pct


def fire_alert_once(db, local_date, total):
    """Return an alert dict only the first time a level is crossed that day."""
    goal = get_goal(db)
    level, pct = status_for(total, goal)
    if level == "ok":
        return None
    cur = db.execute("INSERT OR IGNORE INTO alerts_sent VALUES (?,?,?)", (USER_ID, local_date, level))
    if cur.rowcount == 0:
        return None
    msg = (f"You have gone over your {goal['daily_kcal']:.0f} kcal goal."
           if level == "over" else f"You have used {pct:.0f}% of your daily calories.")
    return {"level": level, "message": msg}


# ---------------------------------------------------------------- logs
def log_json(row):
    return dict(row)


@app.post("/api/logs")
def add_log():
    d = request.get_json(force=True)
    db = get_db()
    try:
        servings = num(d.get("servings", 1), "servings", 0.1, 20)
    except ValueError as e:
        return err(str(e))
    key = d.get("idempotency_key") or str(uuid.uuid4())

    prior = db.execute("SELECT * FROM food_logs WHERE user_id=? AND idempotency_key=?",
                       (USER_ID, key)).fetchone()
    if prior:  # client retried after a network failure: return the original result
        return jsonify(log=log_json(prior), duplicate=True)

    food = db.execute("SELECT * FROM foods WHERE id=?", (d.get("food_id"),)).fetchone()
    if not food:
        return err("Food not found", 404)

    now = datetime.now(user_tz())
    logged_at = now.isoformat(timespec="seconds")
    local_date = now.date().isoformat()

    db.execute("BEGIN IMMEDIATE")
    try:
        # Race-safe "max 3 per food per day": the upsert only updates while count < 3.
        cur = db.execute(
            "INSERT INTO food_daily_counts(user_id,food_id,local_date,count) VALUES (?,?,?,1) "
            "ON CONFLICT(user_id,food_id,local_date) DO UPDATE SET count = count + 1 WHERE count < ?",
            (USER_ID, food["id"], local_date, MAX_PER_FOOD_PER_DAY))
        if cur.rowcount == 0:
            db.execute("ROLLBACK")
            return err(f"You can log the same food at most {MAX_PER_FOOD_PER_DAY} times per day.", 409)
        cur = db.execute(
            "INSERT INTO food_logs(user_id,food_id,food_name,servings,kcal,carbs_g,protein_g,fat_g,"
            "logged_at,local_date,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (USER_ID, food["id"], food["name"], servings, food["kcal"] * servings,
             food["carbs_g"] * servings, food["protein_g"] * servings, food["fat_g"] * servings,
             logged_at, local_date, key))
        log_id = cur.lastrowid
        total = day_total(db, local_date)
        alert = fire_alert_once(db, local_date, total)
        db.execute("COMMIT")
    except sqlite3.IntegrityError:
        db.execute("ROLLBACK")
        return err("This food was already logged at this exact time.", 409)
    except Exception:
        db.execute("ROLLBACK")
        raise
    row = db.execute("SELECT * FROM food_logs WHERE id=?", (log_id,)).fetchone()
    return jsonify(log=log_json(row), alert=alert), 201


@app.delete("/api/logs/<int:log_id>")
def delete_log(log_id):
    db = get_db()
    row = db.execute("SELECT * FROM food_logs WHERE id=? AND user_id=?", (log_id, USER_ID)).fetchone()
    if not row:
        return err("Log not found", 404)
    db.execute("BEGIN IMMEDIATE")
    db.execute("DELETE FROM food_logs WHERE id=?", (log_id,))
    db.execute(
        "UPDATE food_daily_counts SET count = count - 1 WHERE user_id=? AND food_id=? AND local_date=?",
        (USER_ID, row["food_id"], row["local_date"]))
    db.execute("DELETE FROM food_daily_counts WHERE count <= 0")
    db.execute("COMMIT")
    return jsonify(deleted=log_id)


@app.get("/api/logs")
def list_logs():
    """Retrieve logs by date range (up to 1 year), paginated."""
    today = datetime.now(user_tz()).date()
    try:
        start = datetime.strptime(request.args.get("from", today.isoformat()), "%Y-%m-%d").date()
        end = datetime.strptime(request.args.get("to", today.isoformat()), "%Y-%m-%d").date()
        limit = min(int(request.args.get("limit", 100)), 500)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        return err("Bad date or paging parameter")
    if (end - start).days > 366 or end < start:
        return err("Date range must be 1 year or less")
    rows = get_db().execute(
        "SELECT * FROM food_logs WHERE user_id=? AND local_date BETWEEN ? AND ? "
        "ORDER BY logged_at DESC LIMIT ? OFFSET ?",
        (USER_ID, start.isoformat(), end.isoformat(), limit, offset)).fetchall()
    return jsonify(logs=[dict(r) for r in rows])


# ---------------------------------------------------------------- summaries
@app.get("/api/summary")
def summary():
    rng = request.args.get("range", "day")
    if rng not in ("day", "week"):
        return err("range must be day or week")
    db = get_db()
    today = datetime.now(user_tz()).date()
    n_days = 1 if rng == "day" else 7
    days = [(today - timedelta(days=i)).isoformat() for i in range(n_days - 1, -1, -1)]
    rows = db.execute(
        "SELECT local_date, SUM(kcal) kcal, SUM(carbs_g) carbs, SUM(protein_g) protein, SUM(fat_g) fat "
        "FROM food_logs WHERE user_id=? AND local_date BETWEEN ? AND ? GROUP BY local_date",
        (USER_ID, days[0], days[-1])).fetchall()
    by_date = {r["local_date"]: r for r in rows}
    out = []
    for d in days:
        r = by_date.get(d)
        out.append(dict(date=d, kcal=round(r["kcal"], 1) if r else 0,
                        carbs=round(r["carbs"], 1) if r else 0,
                        protein=round(r["protein"], 1) if r else 0,
                        fat=round(r["fat"], 1) if r else 0))
    goal = get_goal(db)
    totals = {k: round(sum(x[k] for x in out), 1) for k in ("kcal", "carbs", "protein", "fat")}
    today_kcal = out[-1]["kcal"]
    status, pct = status_for(today_kcal, goal)
    return jsonify(range=rng, days=out, totals=totals, goal=dict(goal),
                   today_status=status, today_pct=round(pct, 1))


# ---------------------------------------------------------------- UI
@app.get("/")
def index():
    return Response(HTML, mimetype="text/html")


HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Food Logger</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#fff;--fg:#111;--card:#f4f6f8;--line:#c8ced6;--brand:#0b5cad;--warn:#7a4b00;--warnbg:#ffe8b3;--bad:#8a1010;--badbg:#ffd0d0;--ok:#0d5f2b}
@media (prefers-color-scheme:dark){:root{--bg:#10151b;--fg:#f3f5f7;--card:#1b232c;--line:#3a4653;--brand:#6db3ff;--warn:#ffd98a;--warnbg:#4a3600;--bad:#ffb3b3;--badbg:#5a1515;--ok:#7be0a0}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:20px/1.5 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:16px}
h1{font-size:1.6rem;margin:.2em 0}h2{font-size:1.25rem;margin:.2em 0 .6em}
.card{background:var(--card);border:2px solid var(--line);border-radius:16px;padding:16px;margin:16px 0}
button,input{font:inherit}
button{min-height:60px;padding:12px 20px;border-radius:14px;border:3px solid var(--brand);background:var(--brand);color:#fff;font-weight:700;cursor:pointer;width:100%}
@media (prefers-color-scheme:dark){button{color:#000}}
button.secondary{background:transparent;color:var(--brand)}
button.small{min-height:48px;width:auto;padding:8px 16px}
button:focus-visible,input:focus-visible{outline:4px solid #ffbf00;outline-offset:2px}
.row{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
label{display:block;font-weight:600;margin:10px 0 4px}
input{width:100%;min-height:54px;padding:8px 12px;border:2px solid var(--line);border-radius:10px;background:var(--bg);color:var(--fg)}
#banner{display:none;padding:16px;border-radius:14px;font-weight:700;border:3px solid;margin:12px 0}
#banner.warn{display:block;background:var(--warnbg);color:var(--warn);border-color:var(--warn)}
#banner.over{display:block;background:var(--badbg);color:var(--bad);border-color:var(--bad)}
.bar{height:30px;background:var(--bg);border:2px solid var(--line);border-radius:15px;overflow:hidden}
.bar>div{height:100%;background:var(--ok)}.bar.warn>div{background:#c77f00}.bar.over>div{background:#c42020}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line)}
.charts{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
canvas{max-height:300px}
dialog{border:3px solid var(--brand);border-radius:16px;max-width:520px;width:92%;background:var(--bg);color:var(--fg)}
video{width:100%;border-radius:12px;background:#000}
.sr{position:absolute;left:-9999px}
#msg{min-height:1.5em;font-weight:600}
</style></head>
<body><main>
<h1>Food Logger</h1>
<div id="banner" role="alert" aria-live="assertive"></div>

<section class="card" aria-labelledby="today-h">
  <h2 id="today-h">Today</h2>
  <p id="today-text"></p>
  <div class="bar" id="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100"><div></div></div>
  <p id="macro-text"></p>
</section>

<div class="row">
  <button id="scan">📷 Scan barcode</button>
  <button id="manual" class="secondary">✏️ Add food by hand</button>
  <button id="goalbtn" class="secondary">🎯 Set calorie goal</button>
</div>
<p id="msg" role="status" aria-live="polite"></p>

<section class="card" id="foodcard" hidden aria-labelledby="fc-h">
  <h2 id="fc-h">Log this food</h2>
  <p id="fc-info"></p>
  <label for="servings">How many servings?</label>
  <input id="servings" type="number" min="0.1" max="20" step="0.1" value="1">
  <p></p><button id="logit">✅ Log it</button>
</section>

<section class="card"><h2>Charts</h2>
  <div class="charts">
    <div><h3>Today: calories by nutrient</h3><canvas id="donut" role="img" aria-label="Donut chart of calories from carbs, protein and fat today"></canvas></div>
    <div><h3>This week: calories vs goal</h3><canvas id="week" role="img" aria-label="Bar chart of daily calories for the past 7 days with goal line"></canvas></div>
  </div>
  <details><summary>Show data as a table</summary>
    <table id="weektbl"><thead><tr><th>Date</th><th>Calories</th><th>Carbs g</th><th>Protein g</th><th>Fat g</th></tr></thead><tbody></tbody></table>
  </details>
</section>

<section class="card"><h2>Today's log</h2>
  <table id="logtbl"><thead><tr><th>Time</th><th>Food</th><th>Servings</th><th>Calories</th><th></th></tr></thead><tbody></tbody></table>
</section>

<dialog id="scandlg"><h2>Scan barcode</h2>
  <video id="vid" playsinline muted></video>
  <p id="scanhint">Point the camera at a barcode.</p>
  <label for="codeinput">Or type the barcode number</label>
  <input id="codeinput" inputmode="numeric" placeholder="e.g. 1234567890123">
  <p></p><button id="lookup">Look up</button><p></p>
  <button class="secondary" id="closescan">Cancel</button>
</dialog>

<dialog id="manualdlg"><h2>Add food</h2>
  <label for="m-name">Name</label><input id="m-name">
  <label for="m-serv">Serving size (g)</label><input id="m-serv" type="number" value="100">
  <label for="m-kcal">Calories per serving</label><input id="m-kcal" type="number">
  <label for="m-carbs">Carbs (g)</label><input id="m-carbs" type="number" value="0">
  <label for="m-protein">Protein (g)</label><input id="m-protein" type="number" value="0">
  <label for="m-fat">Fat (g)</label><input id="m-fat" type="number" value="0">
  <label for="m-barcode">Barcode (optional)</label><input id="m-barcode" inputmode="numeric">
  <p></p><button id="m-save">Save</button><p></p>
  <button class="secondary" id="m-cancel">Cancel</button>
</dialog>

<dialog id="goaldlg"><h2>Daily calorie goal</h2>
  <label for="g-kcal">Calories per day</label><input id="g-kcal" type="number">
  <label for="g-pct">Warn me at (% of goal)</label><input id="g-pct" type="number" value="80">
  <p></p><button id="g-save">Save</button><p></p>
  <button class="secondary" id="g-cancel">Cancel</button>
</dialog>
</main>
<script>
const $=s=>document.querySelector(s);
const tz=Intl.DateTimeFormat().resolvedOptions().timeZone;
async function api(path,opts={}){
  const r=await fetch(path,{...opts,headers:{'Content-Type':'application/json','X-TZ':tz}});
  const j=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(j.error||'Request failed');
  return j;
}
const say=(t)=>{$('#msg').textContent=t};
let currentFood=null, stream=null, scanning=false, charts={};

function showFood(f){
  currentFood=f;
  $('#fc-info').textContent=`${f.name}: ${Math.round(f.kcal)} kcal per ${f.serving_g} g (carbs ${f.carbs_g} g, protein ${f.protein_g} g, fat ${f.fat_g} g)`;
  $('#foodcard').hidden=false; $('#servings').focus();
  $('#foodcard').scrollIntoView({behavior:'smooth'});
}

async function refresh(){
  const [day,week,logs]=await Promise.all([api('/api/summary?range=day'),api('/api/summary?range=week'),api('/api/logs')]);
  const t=day.totals, g=day.goal.daily_kcal, pct=day.today_pct;
  $('#today-text').textContent=`${Math.round(t.kcal)} of ${Math.round(g)} kcal (${Math.round(pct)}%)`;
  $('#macro-text').textContent=`Carbs ${t.carbs} g · Protein ${t.protein} g · Fat ${t.fat} g`;
  const bar=$('#bar'); bar.className='bar '+day.today_status; bar.setAttribute('aria-valuenow',Math.min(100,Math.round(pct)));
  bar.firstElementChild.style.width=Math.min(100,pct)+'%';
  const b=$('#banner'); b.className=day.today_status==='ok'?'':day.today_status;
  b.textContent=day.today_status==='over'?'🚫 Over limit: you have gone over your daily calorie goal.'
    :day.today_status==='warn'?`⚠️ Warning: you have used ${Math.round(pct)}% of your daily calories.`:'';
  drawDonut(t); drawWeek(week); drawTables(week,logs.logs);
}
function drawDonut(t){
  const data=[t.carbs*4,t.protein*4,t.fat*9];
  charts.donut&&charts.donut.destroy();
  charts.donut=new Chart($('#donut'),{type:'doughnut',data:{
    labels:[`Carbs ${t.carbs} g`,`Protein ${t.protein} g`,`Fat ${t.fat} g`],
    datasets:[{data:data.some(x=>x>0)?data:[1,0,0],backgroundColor:['#0b5cad','#1e8a4c','#c77f00']}]},
    options:{plugins:{legend:{position:'bottom',labels:{font:{size:16}}}}}});
}
function drawWeek(w){
  charts.week&&charts.week.destroy();
  charts.week=new Chart($('#week'),{data:{labels:w.days.map(d=>d.date.slice(5)),datasets:[
    {type:'bar',label:'Calories',data:w.days.map(d=>d.kcal),backgroundColor:'#0b5cad'},
    {type:'line',label:`Goal (${w.goal.daily_kcal})`,data:w.days.map(()=>w.goal.daily_kcal),borderColor:'#c42020',borderDash:[6,4],pointRadius:0}]},
    options:{plugins:{legend:{labels:{font:{size:16}}}},scales:{y:{beginAtZero:true}}}});
}
function drawTables(w,logs){
  $('#weektbl tbody').innerHTML=w.days.map(d=>`<tr><td>${d.date}</td><td>${Math.round(d.kcal)}</td><td>${d.carbs}</td><td>${d.protein}</td><td>${d.fat}</td></tr>`).join('');
  $('#logtbl tbody').innerHTML=logs.length?logs.map(l=>`<tr><td>${l.logged_at.slice(11,16)}</td><td>${esc(l.food_name)}</td><td>${l.servings}</td><td>${Math.round(l.kcal)}</td><td><button class="small secondary" data-undo="${l.id}" aria-label="Undo ${esc(l.food_name)}">Undo</button></td></tr>`).join('')
    :'<tr><td colspan="5">Nothing logged yet today.</td></tr>';
}
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

$('#logtbl').addEventListener('click',async e=>{
  const id=e.target.dataset.undo; if(!id) return;
  try{await api('/api/logs/'+id,{method:'DELETE'}); say('Entry removed.'); refresh();}catch(x){say(x.message)}
});

$('#logit').onclick=async()=>{
  if(!currentFood) return;
  try{
    const r=await api('/api/logs',{method:'POST',body:JSON.stringify({
      food_id:currentFood.id,servings:+$('#servings').value,idempotency_key:crypto.randomUUID()})});
    say(`Logged ${currentFood.name}.`+(r.alert?' '+r.alert.message:''));
    if(r.alert&&'Notification' in window&&Notification.permission==='granted') new Notification('Food Logger',{body:r.alert.message});
    $('#foodcard').hidden=true; currentFood=null; refresh();
  }catch(x){say('⚠️ '+x.message)}
};

async function lookup(code){
  try{const r=await api('/api/foods/barcode/'+encodeURIComponent(code)); closeScan(); showFood(r.food); say('');}
  catch(x){say(x.message)}
}
async function openScan(){
  if('Notification' in window&&Notification.permission==='default') Notification.requestPermission();
  $('#scandlg').showModal(); $('#codeinput').value='';
  if(!('BarcodeDetector' in window)){$('#scanhint').textContent='Camera scanning is not supported in this browser. Type the number below.';return;}
  try{
    stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'environment'}});
    $('#vid').srcObject=stream; await $('#vid').play(); scanning=true;
    const det=new BarcodeDetector({formats:['ean_13','ean_8','upc_a','upc_e']});
    (async function loop(){
      while(scanning){
        try{const c=await det.detect($('#vid')); if(c.length){scanning=false; await lookup(c[0].rawValue); return;}}catch(e){}
        await new Promise(r=>setTimeout(r,300));
      }
    })();
  }catch(e){$('#scanhint').textContent='Camera unavailable. Type the barcode number below.';}
}
function closeScan(){scanning=false;stream&&stream.getTracks().forEach(t=>t.stop());stream=null;$('#scandlg').close();}
$('#scan').onclick=openScan; $('#closescan').onclick=closeScan;
$('#lookup').onclick=()=>{const c=$('#codeinput').value.trim(); if(c) lookup(c);};

$('#manual').onclick=()=>$('#manualdlg').showModal();
$('#m-cancel').onclick=()=>$('#manualdlg').close();
$('#m-save').onclick=async()=>{
  try{
    const r=await api('/api/foods',{method:'POST',body:JSON.stringify({name:$('#m-name').value,serving_g:$('#m-serv').value,
      kcal:$('#m-kcal').value,carbs_g:$('#m-carbs').value,protein_g:$('#m-protein').value,fat_g:$('#m-fat').value,barcode:$('#m-barcode').value})});
    $('#manualdlg').close(); showFood(r.food);
  }catch(x){say(x.message)}
};

$('#goalbtn').onclick=async()=>{const g=(await api('/api/goal')).goal;$('#g-kcal').value=g.daily_kcal;$('#g-pct').value=g.alert_threshold_pct;$('#goaldlg').showModal();};
$('#g-cancel').onclick=()=>$('#goaldlg').close();
$('#g-save').onclick=async()=>{
  try{await api('/api/goal',{method:'PUT',body:JSON.stringify({daily_kcal:$('#g-kcal').value,alert_threshold_pct:$('#g-pct').value})});
    $('#goaldlg').close(); say('Goal saved.'); refresh();}catch(x){say(x.message)}
};
refresh();
</script></body></html>
"""

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
