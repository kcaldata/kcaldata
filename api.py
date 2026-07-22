from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import sqlite3, re

DB_FILE = "kcaldata.db"
app = FastAPI(title="kcaldata API", version="0.2.0")

WEIGHT_UNITS = {
    "g": 1, "gram": 1, "grams": 1,
    "kg": 1000, "kilogram": 1000, "kilograms": 1000,
    "oz": 28.3495, "ounce": 28.3495, "ounces": 28.3495,
    "lb": 453.592, "lbs": 453.592, "pound": 453.592, "pounds": 453.592,
}

def fmt(n):
    return int(n) if float(n).is_integer() else round(n, 2)

def score(query, desc):
    q = query.lower().strip(); d = desc.lower(); s = 0.0
    if d == q: s += 100
    if re.search(r"\b" + re.escape(q) + r"\b", d): s += 50
    idx = d.find(q)
    if idx >= 0: s += max(0, 30 - idx)
    s -= len(d) * 0.15
    return s

def best_food(cur, terms):
    terms = terms.strip()
    if not terms: return None
    variants = [terms]
    if terms.endswith("s"): variants.append(terms[:-1])
    rows = []
    for v in variants:
        cur.execute("SELECT f.fdc_id, f.description, c.kcal_per_100g "
                    "FROM foods f JOIN calories c ON f.fdc_id=c.fdc_id "
                    "WHERE f.description LIKE ?", (f"%{v}%",))
        rows = cur.fetchall()
        if rows: break
    if not rows:
        for w in sorted(terms.split(), key=len, reverse=True):
            w2 = w[:-1] if w.endswith("s") else w
            cur.execute("SELECT f.fdc_id, f.description, c.kcal_per_100g "
                        "FROM foods f JOIN calories c ON f.fdc_id=c.fdc_id "
                        "WHERE f.description LIKE ?", (f"%{w2}%",))
            rows = cur.fetchall()
            if rows: break
    if not rows: return None
    rows.sort(key=lambda r: score(terms, r["description"]), reverse=True)
    return rows

def find_portion(cur, fdc_id, word):
    cur.execute("SELECT amount, modifier, gram_weight FROM portions "
                "WHERE fdc_id=? AND gram_weight>0", (fdc_id,))
    w = word.lower().rstrip("s")
    partial = None
    for amount, modifier, gram_weight in cur.fetchall():
        mod = (modifier or "").lower().strip()
        per = gram_weight / amount if amount else gram_weight
        label = f"1 {modifier} \u2248 {round(per)} g"
        if mod == w:
            return per, label
        if partial is None and w and w in mod.split():
            partial = (per, label)
    return partial

def parse(query):
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(.*)$", query.strip())
    if m and m.group(2).strip():
        return float(m.group(1)), m.group(2).strip()
    return None, query.strip()

def do_lookup(query):
    con = sqlite3.connect(DB_FILE); con.row_factory = sqlite3.Row
    cur = con.cursor()
    qty, rest = parse(query)
    grams = None; unit = None; basis = None; food_terms = rest; rows = None

    if qty is not None and rest:
        first, _, remainder = rest.partition(" ")
        fw = first.lower().rstrip(".")
        if fw in WEIGHT_UNITS and remainder:
            grams = qty * WEIGHT_UNITS[fw]; unit = first; food_terms = remainder
            basis = f"{fmt(qty)} {unit} = {round(grams)} g"
        elif remainder:
            cand = best_food(cur, remainder)
            if cand:
                p = find_portion(cur, cand[0]["fdc_id"], first)
                if p:
                    per, label = p
                    grams = qty * per; unit = first; food_terms = remainder
                    rows = cand
                    basis = f"{fmt(qty)} \u00d7 ({label})"

    rows = rows or best_food(cur, food_terms)
    if not rows:
        con.close(); return None
    best = rows[0]; kcal100 = best["kcal_per_100g"]
    result = {
        "query": query,
        "matched_food": best["description"],
        "calories_kcal_per_100g": round(kcal100),
        "confidence": round(max(0.3, min(0.99, score(food_terms, best["description"]) / 100)), 2),
        "source": "USDA FoodData Central (SR Legacy)",
    }
    if grams is not None:
        result["quantity"] = fmt(qty)
        result["unit"] = unit
        result["grams_used"] = round(grams, 1)
        result["calories_kcal"] = round(kcal100 * grams / 100)
        result["basis"] = basis
    elif qty is not None:
        result["note"] = "Couldn't resolve the unit; showing calories per 100 g."
    result["alternatives"] = [
        {"food": r["description"], "calories_kcal_per_100g": round(r["kcal_per_100g"])}
        for r in rows[1:4]
    ]
    con.close(); return result

@app.get("/")
def home():
    return {"name": "kcaldata API", "try": "/v1/lookup?query=2 large eggs", "docs": "/docs"}

@app.get("/v1/lookup")
def lookup(query: str):
    r = do_lookup(query)
    return r if r else JSONResponse(status_code=404, content={"error": f"No match for '{query}'"})

class Query(BaseModel):
    query: str

@app.post("/v1/lookup")
def lookup_post(body: Query):
    r = do_lookup(body.query)
    return r if r else JSONResponse(status_code=404, content={"error": f"No match for '{body.query}'"})
