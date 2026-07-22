from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import sqlite3, re

DB_FILE = "kcaldata.db"
app = FastAPI(title="kcaldata API", version="0.7.0")

WEIGHT_UNITS = {
    "g": 1, "gram": 1, "grams": 1, "kg": 1000, "kilogram": 1000, "kilograms": 1000,
    "oz": 28.3495, "ounce": 28.3495, "ounces": 28.3495,
    "lb": 453.592, "lbs": 453.592, "pound": 453.592, "pounds": 453.592,
}
SIZE_WORDS = {"large", "small", "medium", "extra", "jumbo", "mini", "xl"}
PORTION_UNITS = {
    "cup", "slice", "clove", "piece", "stick", "fillet", "serving", "can",
    "tbsp", "tablespoon", "tsp", "teaspoon", "bottle", "bar", "patty", "link",
}
# Processing / product words, stored SINGULAR. Penalized only if NOT requested.
PROCESSED = {
    "dried", "dehydrated", "powder", "powdered", "mix", "imitation", "substitute",
    "concentrate", "breaded", "frozen", "nugget", "microwaved", "heated", "smoked",
    "canned", "prepared", "juice", "drink", "beverage", "flavored", "flavor",
    "snack", "cake", "pie", "candy", "cereal", "sauce", "soup", "yogurt",
    "yoghurt", "chip", "pudding", "spread", "syrup", "baby", "babyfood",
    "infant", "dressing", "gravy", "filling", "strudel", "croissant", "strained",
}
CANONICAL_BOOST = {
    "bacon": ["pork", "cured"],
    "milk": ["whole"],
    "chicken": ["broiler", "meat only"],
    "salmon": ["fish"],
}

def fmt(n):
    return int(n) if float(n).is_integer() else round(n, 2)

def singular(w):
    w = w.lower()
    return w[:-1] if len(w) > 3 and w.endswith("s") else w

def norm_str(s):
    return " ".join(singular(w) for w in re.findall(r"[a-z]+", s.lower()))

def raw_score(term, desc):
    nq = norm_str(term); nd = norm_str(desc); s = 0.0
    if nd == nq: s += 100
    if nq and re.search(r"\b" + re.escape(nq) + r"\b", nd): s += 50
    idx = nd.find(nq) if nq else -1
    if idx >= 0: s += max(0, 30 - idx)
    s -= len(nd) * 0.15
    return s

def adjust(terms, desc):
    qwords = {singular(w) for w in re.findall(r"[a-z]+", terms.lower())}
    dset = {singular(w) for w in re.findall(r"[a-z]+", desc.lower())}
    b = 0.0
    if "raw" in dset: b += 14
    if "unprepared" in dset: b += 8
    b -= 16 * len({w for w in dset if w in PROCESSED} - qwords)
    if "egg" in qwords:
        if "whole" in dset: b += 8
        if "yolk" in dset and "yolk" not in qwords: b -= 14
    for key, prefer in CANONICAL_BOOST.items():
        if key in qwords and any(p in dset for p in prefer):
            b += 40
    return b

def text_score(terms, desc):
    return raw_score(terms, desc) + adjust(terms, desc)

def mod_words(mod):
    return {w.rstrip("s") for w in re.split(r"[\s,]+", (mod or "").lower().strip()) if w}

def search_rows(cur, term):
    cur.execute("SELECT f.fdc_id, f.description, c.kcal_per_100g "
                "FROM foods f JOIN calories c ON f.fdc_id=c.fdc_id "
                "WHERE f.description LIKE ?", (f"%{term}%",))
    return cur.fetchall()

def best_food(cur, terms):
    terms = terms.strip().lower()
    if not terms: return None
    seen = {}
    for v in [terms] + ([terms[:-1]] if terms.endswith("s") else []):
        for r in search_rows(cur, v):
            seen[r["fdc_id"]] = r
    if not seen:
        for w in sorted(terms.split(), key=len, reverse=True):
            wv = w[:-1] if w.endswith("s") else w
            if len(wv) < 3: continue
            for r in search_rows(cur, wv):
                seen[r["fdc_id"]] = r
            if seen: break
    if not seen: return None
    rows = list(seen.values())
    rows.sort(key=lambda r: text_score(terms, r["description"]), reverse=True)
    return rows

def has_portion(cur, fdc_id, req_words):
    if not req_words: return False
    cur.execute("SELECT modifier FROM portions WHERE fdc_id=? AND gram_weight>0", (fdc_id,))
    for (mod,) in cur.fetchall():
        low = (mod or "").lower()
        if req_words & mod_words(mod) or any(w in low for w in req_words):
            return True
    return False

def find_portion(cur, fdc_id, candidates):
    cur.execute("SELECT amount, modifier, gram_weight FROM portions "
                "WHERE fdc_id=? AND gram_weight>0", (fdc_id,))
    portions = cur.fetchall()
    cand = {c.lower().rstrip("s") for c in candidates if c}
    def out(amount, modifier, gw, assumed=False):
        per = gw / amount if amount else gw
        m = (modifier or "").strip()
        label = f"1 {m} \u2248 {round(per)} g" if m else f"1 unit \u2248 {round(per)} g"
        return per, label, assumed
    for amount, modifier, gw in portions:
        if cand & mod_words(modifier):
            return out(amount, modifier, gw)
    if cand:
        for amount, modifier, gw in portions:
            if any(c in (modifier or "").lower() for c in cand):
                return out(amount, modifier, gw)
        return None
    for pref in ("medium", "large", "small"):
        for amount, modifier, gw in portions:
            if pref in mod_words(modifier):
                return out(amount, modifier, gw, assumed=True)
    return None

def parse(query):
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(.*)$", query.strip())
    if m and m.group(2).strip():
        return float(m.group(1)), m.group(2).strip()
    return None, query.strip()

def do_lookup(query):
    con = sqlite3.connect(DB_FILE); con.row_factory = sqlite3.Row
    cur = con.cursor()
    qty, rest = parse(query)
    grams = unit = basis = None; assumed = False; rows = None; food_terms = rest

    if qty is not None and rest:
        tokens = rest.split()
        fw = tokens[0].lower().rstrip(".")
        if fw in WEIGHT_UNITS and len(tokens) > 1:
            grams = qty * WEIGHT_UNITS[fw]; unit = tokens[0]
            food_terms = " ".join(tokens[1:])
            basis = f"{fmt(qty)} {unit} = {round(grams)} g"
            rows = best_food(cur, food_terms)
        else:
            descriptors = [t for t in tokens if t.lower() in SIZE_WORDS]
            units = [t for t in tokens if t.lower().rstrip("s") in PORTION_UNITS]
            req_words = {t.lower().rstrip("s") for t in descriptors + units}
            stripped = " ".join(t for t in tokens
                                if t.lower() not in SIZE_WORDS
                                and t.lower().rstrip("s") not in PORTION_UNITS)
            food_terms = stripped.strip() or rest
            rows = best_food(cur, food_terms)
            if rows and req_words:
                head = rows[:25]
                head.sort(key=lambda r: text_score(food_terms, r["description"])
                          + (60 if has_portion(cur, r["fdc_id"], req_words) else 0),
                          reverse=True)
                rows = head + rows[25:]
            if rows:
                p = find_portion(cur, rows[0]["fdc_id"], units + descriptors)
                if p:
                    per, label, assumed = p
                    grams = qty * per
                    unit = (descriptors + units + ["unit"])[0]
                    basis = f"{fmt(qty)} \u00d7 ({label})"

    if rows is None:
        rows = best_food(cur, food_terms)
    if not rows:
        con.close(); return None

    best = rows[0]; kcal100 = best["kcal_per_100g"]
    result = {
        "query": query,
        "matched_food": best["description"],
        "calories_kcal_per_100g": round(kcal100),
        "confidence": round(max(0.3, min(0.99, text_score(food_terms, best["description"]) / 100)), 2),
        "source": "USDA FoodData Central (SR Legacy)",
    }
    if grams is not None:
        result["quantity"] = fmt(qty); result["unit"] = unit
        result["grams_used"] = round(grams, 1)
        result["calories_kcal"] = round(kcal100 * grams / 100)
        result["basis"] = basis
        if assumed:
            result["note"] = "No size given; assumed a medium/standard portion."
    elif qty is not None:
        result["note"] = "Couldn't resolve the unit; showing calories per 100 g."
    result["alternatives"] = [
        {"food": r["description"], "calories_kcal_per_100g": round(r["kcal_per_100g"])}
        for r in rows[1:4]
    ]
    con.close(); return result

@app.get("/")
def home():
    return {"name": "kcaldata API", "try": "/v1/lookup?query=banana", "docs": "/docs"}

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
