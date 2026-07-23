from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from fastmcp import FastMCP, Context
from fastmcp.tools.tool import ToolResult
import sqlite3, re, os, json, datetime, httpx

from x402.schemas.payments import PaymentRequired, PaymentRequirements, ResourceInfo
from x402.mcp.utils import extract_payment_from_meta

DB_FILE = "kcaldata.db"
FREE_DAILY_LIMIT = 100
USAGE = {}  # memory fallback when Redis isn't configured

# ---------- x402 payment settings (MCP tool) ----------
PAY_TO = "0xC58F8Eff2B6f46b9f5e75432FCdeBD5Dd949B09F"
NETWORK = "eip155:8453"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PRICE_USD = "0.005"
PRICE_UNITS = "5000"
PAID_REST_ENDPOINT = "https://pay.kcaldata.com/v1/pro/lookup"

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
PROCESSED = {
    "dried", "dehydrated", "powder", "powdered", "mix", "imitation", "substitute",
    "concentrate", "breaded", "frozen", "nugget", "microwaved", "heated", "smoked",
    "canned", "prepared", "juice", "drink", "beverage", "flavored", "flavor",
    "snack", "cake", "pie", "candy", "cereal", "sauce", "soup", "yogurt",
    "yoghurt", "chip", "pudding", "spread", "syrup", "baby", "babyfood",
    "infant", "dressing", "gravy", "filling", "strudel", "croissant", "strained",
}
CANONICAL_BOOST = {
    "bacon": ["pork", "cured"], "milk": ["whole"],
    "chicken": ["broiler", "meat only"], "salmon": ["fish"],
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


# ---------- usage counters: Upstash Redis with in-memory fallback ----------
def _redis_incr(key, ttl=90000):
    """Increment a counter in Upstash Redis. Returns new count, or None if unavailable."""
    url = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    if not url or not token:
        return None
    try:
        r = httpx.post(
            f"{url}/pipeline",
            json=[["INCR", key], ["EXPIRE", key, ttl, "NX"]],
            headers={"Authorization": f"Bearer {token}"},
            timeout=3.0,
        )
        return int(r.json()[0]["result"])
    except Exception:
        return None  # fail open


def bump_usage(identifier, today):
    """Count one call. Returns the running total for today."""
    n = _redis_incr(f"kcal:{identifier}:{today}")
    if n is not None:
        return n
    if len(USAGE) > 5000:
        for k in [k for k in USAGE if k[1] != today]:
            USAGE.pop(k, None)
    k = (identifier, today)
    USAGE[k] = USAGE.get(k, 0) + 1
    return USAGE[k]


def load_keys():
    raw = os.environ.get("API_KEYS", "").strip()
    if not raw:
        return {}
    try:
        return {str(k): int(v) for k, v in json.loads(raw).items()}
    except Exception:
        return {}


def client_ip(request):
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_check(request, api_key):
    today = datetime.date.today().isoformat()
    keys = load_keys()
    if api_key:
        if api_key not in keys:
            return JSONResponse(status_code=401, content={"error": "Invalid API key"})
        identifier = "key:" + api_key; limit = keys[api_key]; tier = "keyed"
    else:
        identifier = "ip:" + client_ip(request); limit = FREE_DAILY_LIMIT; tier = "free"
    used = bump_usage(identifier, today)
    if used > limit:
        return JSONResponse(status_code=429, content={
            "error": "Daily request limit reached",
            "tier": tier, "limit": limit,
            "message": f"Free tier is capped at {FREE_DAILY_LIMIT} requests/day. "
                       f"A developer key is 25 USDC/month - contact kcaldata@protonmail.com.",
        })
    return limit, max(0, limit - used)


def with_limits(payload, limit, remaining, status=200):
    resp = JSONResponse(content=payload, status_code=status)
    resp.headers["X-RateLimit-Limit"] = str(limit)
    resp.headers["X-RateLimit-Remaining"] = str(remaining)
    return resp


# ---------- x402 for the MCP tool ----------
def _payment_requirements():
    return PaymentRequirements(
        scheme="exact", network=NETWORK, asset=USDC_BASE,
        amount=PRICE_UNITS, payTo=PAY_TO, maxTimeoutSeconds=300,
        extra={"name": "USD Coin", "version": "2"},
    )


def _resource_server():
    """Sync x402 resource server. Returns None if CDP keys aren't configured."""
    key_id = os.environ.get("CDP_API_KEY_ID", "")
    key_secret = os.environ.get("CDP_API_KEY_SECRET", "")
    if not key_id or not key_secret:
        return None
    try:
        from x402.http import HTTPFacilitatorClientSync, FacilitatorConfig, CreateHeadersAuthProvider
        from x402.mcp.server_sync import x402ResourceServerSync
        from x402.mechanisms.evm.exact import register_exact_evm_server
        from cdp.auth.utils.http import get_auth_headers, GetAuthHeadersOptions

        host, base = "api.cdp.coinbase.com", "/platform/v2/x402"

        def hdrs(path, method):
            return get_auth_headers(GetAuthHeadersOptions(
                api_key_id=key_id, api_key_secret=key_secret,
                request_method=method, request_host=host, request_path=path))

        def make_headers():
            return {
                "verify": hdrs(f"{base}/verify", "POST"),
                "settle": hdrs(f"{base}/settle", "POST"),
                "supported": hdrs(f"{base}/supported", "GET"),
            }

        fac = HTTPFacilitatorClientSync(FacilitatorConfig(
            url=f"https://{host}{base}",
            auth_provider=CreateHeadersAuthProvider(make_headers),
        ))
        rs = x402ResourceServerSync(facilitator_clients=fac)
        return register_exact_evm_server(rs, networks=NETWORK)
    except Exception:
        return None


def _payment_from_context(ctx):
    """Pull an x402 payment payload out of the MCP request metadata."""
    try:
        meta = getattr(ctx.request_context, "meta", None)
        if meta is None:
            return None
        if hasattr(meta, "model_dump"):
            md = meta.model_dump(by_alias=True, exclude_none=True)
        elif isinstance(meta, dict):
            md = meta
        else:
            md = dict(vars(meta))
        return extract_payment_from_meta({"_meta": md})
    except Exception:
        return None


def _payment_required_result(reason):
    pr = PaymentRequired(
        x402Version=2,
        error=reason,
        resource=ResourceInfo(
            url="mcp://tool/lookup_calories",
            description="Calorie lookup from a natural-language food description.",
            mimeType="application/json",
        ),
        accepts=[_payment_requirements()],
    )
    d = pr.model_dump(by_alias=True, exclude_none=True)
    d["hint"] = {
        "message": f"Free tier used up ({FREE_DAILY_LIMIT}/day). "
                   f"Pay {PRICE_USD} USDC per call via x402, or use the paid REST endpoint.",
        "paid_endpoint": PAID_REST_ENDPOINT + "?query=YOUR_QUERY",
        "price_usd": PRICE_USD,
        "free_tier": f"{FREE_DAILY_LIMIT} calls/day",
        "developer_plan": "25 USDC/month - kcaldata@protonmail.com",
    }
    return ToolResult(
        content=[{"type": "text", "text": json.dumps(d)}],
        structured_content=d,
        is_error=True,
    )


def _mcp_caller_id():
    try:
        from fastmcp.server.dependencies import get_http_request
        req = get_http_request()
        xff = req.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        client = getattr(req, "client", None)
        return getattr(client, "host", None) or "unknown"
    except Exception:
        return "unknown"


# ---------- MCP server ----------
mcp = FastMCP("kcaldata")


@mcp.tool
def lookup_calories(query: str, ctx: Context):
    """Look up calorie information for a food or meal.

    Accepts natural-language descriptions including quantity and unit,
    e.g. "2 large eggs", "8 oz salmon", "1 cup rice", "3 slices bacon",
    or a plain food name like "banana". Returns the matched food, its
    calories, the total for the amount given, and how it was calculated.

    Free tier: 100 calls/day. Beyond that, pay $0.005 USDC per call (x402).
    """
    today = datetime.date.today().isoformat()
    caller = _mcp_caller_id()
    used = bump_usage("mcp:" + caller, today)

    if used > FREE_DAILY_LIMIT:
        payment = _payment_from_context(ctx)
        if payment is None:
            return _payment_required_result(
                f"Free daily allowance ({FREE_DAILY_LIMIT} calls) exhausted. Payment required.")
        rs = _resource_server()
        if rs is None:
            return _payment_required_result(
                "Payment received but settlement is unavailable. Use the paid REST endpoint.")
        try:
            reqs = _payment_requirements()
            verify = rs.verify_payment(payment, reqs)
            if not getattr(verify, "is_valid", False):
                return _payment_required_result(
                    getattr(verify, "invalid_reason", None) or "Payment verification failed.")
            rs.settle_payment(payment, reqs)
        except Exception:
            return _payment_required_result("Payment could not be settled. Please retry.")

    r = do_lookup(query)
    return r if r is not None else {"error": f"No match for '{query}'"}


mcp_app = mcp.http_app(path="/mcp", allowed_hosts=["*"], allowed_origins=["*"])

# ---------- REST API ----------
app = FastAPI(
    title="kcaldata",
    version="1.0.0",
    description=(
        "Calorie and nutrition data for AI agents and developers. Send messy food text, "
        "get sourced calorie JSON. Derived from USDA FoodData Central (public domain).\n\n"
        "Free tier: 100 requests/day. Developer plan: 25 USDC/month "
        "(contact kcaldata@protonmail.com). Autonomous agents can pay per call via x402."
    ),
    contact={"name": "kcaldata", "url": "https://kcaldata.com", "email": "kcaldata@protonmail.com"},
    license_info={"name": "Data: USDA FoodData Central (public domain)"},
    lifespan=mcp_app.lifespan,
)


@app.get("/")
def home():
    return {
        "name": "kcaldata API",
        "version": "1.0.0",
        "rest": "/v1/lookup?query=banana",
        "mcp": "/mcp-server/mcp",
        "docs": "/docs",
        "free_tier": f"{FREE_DAILY_LIMIT} requests/day",
        "developer_plan": "25 USDC/month - kcaldata@protonmail.com",
        "agent_pay_per_call": PAID_REST_ENDPOINT,
        "counters": "redis" if os.environ.get("UPSTASH_REDIS_REST_URL") else "memory",
    }


@app.get("/v1/lookup")
def lookup(query: str, request: Request, api_key: str | None = None):
    rc = rate_check(request, request.headers.get("x-api-key") or api_key)
    if isinstance(rc, JSONResponse):
        return rc
    limit, remaining = rc
    r = do_lookup(query)
    if not r:
        return with_limits({"error": f"No match for '{query}'"}, limit, remaining, status=404)
    return with_limits(r, limit, remaining)


class Query(BaseModel):
    query: str


@app.post("/v1/lookup")
def lookup_post(body: Query, request: Request, api_key: str | None = None):
    rc = rate_check(request, request.headers.get("x-api-key") or api_key)
    if isinstance(rc, JSONResponse):
        return rc
    limit, remaining = rc
    r = do_lookup(body.query)
    if not r:
        return with_limits({"error": f"No match for '{body.query}'"}, limit, remaining, status=404)
    return with_limits(r, limit, remaining)


app.mount("/mcp-server", mcp_app)
