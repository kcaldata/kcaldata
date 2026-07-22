from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import sqlite3, re

DB_FILE = "kcaldata.db"
app = FastAPI(title="kcaldata API", version="0.1.0")

def score(query, desc):
    q = query.lower().strip()
    d = desc.lower()
    s = 0.0
    if d == q:
        s += 100
    if re.search(r"\b" + re.escape(q) + r"\b", d):   # whole-word match
        s += 50
    idx = d.find(q)                                   # earlier match = better
    if idx >= 0:
        s += max(0, 30 - idx)
    s -= len(d) * 0.15                                # prefer concise/generic names
    return s

def do_lookup(query):
    con = sqlite3.connect(DB_FILE); con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("""
        SELECT f.description, c.kcal_per_100g
        FROM foods f JOIN calories c ON f.fdc_id = c.fdc_id
        WHERE f.description LIKE ?
    """, (f"%{query}%",))
    rows = cur.fetchall(); con.close()
    if not rows:
        return None
    ranked = sorted(rows, key=lambda r: score(query, r["description"]), reverse=True)
    best = ranked[0]
    conf = max(0.3, min(0.99, score(query, best["description"]) / 100))
    return {
        "query": query,
        "matched_food": best["description"],
        "calories_kcal_per_100g": round(best["kcal_per_100g"]),
        "confidence": round(conf, 2),
        "source": "USDA FoodData Central (SR Legacy)",
        "alternatives": [
            {"food": r["description"], "calories_kcal_per_100g": round(r["kcal_per_100g"])}
            for r in ranked[1:4]
        ],
    }

@app.get("/")
def home():
    return {"name": "kcaldata API", "try": "/v1/lookup?query=cheddar", "docs": "/docs"}

@app.get("/v1/lookup")
def lookup(query: str):
    result = do_lookup(query)
    if result is None:
        return JSONResponse(status_code=404, content={"error": f"No match for '{query}'"})
    return result

class Query(BaseModel):
    query: str

@app.post("/v1/lookup")
def lookup_post(body: Query):
    result = do_lookup(body.query)
    if result is None:
        return JSONResponse(status_code=404, content={"error": f"No match for '{body.query}'"})
    return result

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)