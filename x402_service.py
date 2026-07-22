from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx
from x402.http import HTTPFacilitatorClient, FacilitatorConfig
from x402.http.middleware.fastapi import payment_middleware_from_config
from x402.mechanisms.evm.exact import ExactEvmServerScheme

# ---------- payment settings ----------
PAY_TO = "0xC58F8Eff2B6f46b9f5e75432FCdeBD5Dd949B09F"   # your Base wallet
NETWORK = "eip155:84532"                       # Base Sepolia TESTNET (fake money)
FACILITATOR = "https://x402.org/facilitator"   # free public testnet facilitator
PRICE = "$0.001"
PRICE_BASE_UNITS = "1000"                       # 1000 = 0.001 USDC (6 decimals)
KCALDATA_API = "https://kcaldata.onrender.com/v1/lookup"

fac = HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR))
mw = payment_middleware_from_config(
    routes={"GET /v1/pro/lookup": {"accepts": [{
        "scheme": "exact", "network": NETWORK, "payTo": PAY_TO,
        "price": PRICE, "maxAmountRequired": PRICE_BASE_UNITS}]}},
    facilitator_client=fac,
    schemes=[{"network": NETWORK, "server": ExactEvmServerScheme()}],
    sync_facilitator_on_start=True,
)

app = FastAPI(title="kcaldata paid (x402)", version="0.1.0")

@app.middleware("http")
async def x402_mw(request, call_next):
    return await mw(request, call_next)

@app.get("/")
def home():
    return {"name": "kcaldata paid endpoint (x402)",
            "paid_endpoint": "/v1/pro/lookup?query=banana",
            "network": NETWORK, "pay_to": PAY_TO, "price": PRICE}

@app.get("/v1/pro/lookup")
def pro_lookup(query: str):
    # If we reach here, the x402 middleware already verified payment.
    try:
        r = httpx.get(KCALDATA_API, params={"query": query}, timeout=90)
        return JSONResponse(status_code=r.status_code, content=r.json())
    except Exception:
        return JSONResponse(status_code=502, content={"error": "upstream lookup failed"})
