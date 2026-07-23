import os
import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from x402.http import FacilitatorConfig, HTTPFacilitatorClient, CreateHeadersAuthProvider
from x402.http.middleware.fastapi import payment_middleware_from_config
from x402.mechanisms.evm.exact import ExactEvmServerScheme

from cdp.auth.utils.http import get_auth_headers, GetAuthHeadersOptions

# ---------------- settings ----------------
PAY_TO = "0xC58F8Eff2B6f46b9f5e75432FCdeBD5Dd949B09F"   # your Base wallet
NETWORK = "eip155:8453"                                  # Base MAINNET (real USDC)
PRICE = "$0.001"
KCALDATA_API = "https://kcaldata.onrender.com/v1/lookup"

CDP_HOST = "api.cdp.coinbase.com"
CDP_BASE_PATH = "/platform/v2/x402"
FACILITATOR_URL = f"https://{CDP_HOST}{CDP_BASE_PATH}"

CDP_API_KEY_ID = os.environ.get("CDP_API_KEY_ID", "")
CDP_API_KEY_SECRET = os.environ.get("CDP_API_KEY_SECRET", "")


def _headers_for(path: str, method: str = "POST") -> dict:
    """Generate a CDP JWT auth header scoped to one facilitator path."""
    return get_auth_headers(
        GetAuthHeadersOptions(
            api_key_id=CDP_API_KEY_ID,
            api_key_secret=CDP_API_KEY_SECRET,
            request_method=method,
            request_host=CDP_HOST,
            request_path=path,
        )
    )


def create_cdp_headers() -> dict:
    """x402 asks for auth headers per facilitator operation."""
    return {
        "verify": _headers_for(f"{CDP_BASE_PATH}/verify", "POST"),
        "settle": _headers_for(f"{CDP_BASE_PATH}/settle", "POST"),
        "supported": _headers_for(f"{CDP_BASE_PATH}/supported", "GET"),
    }


facilitator = HTTPFacilitatorClient(
    FacilitatorConfig(
        url=FACILITATOR_URL,
        auth_provider=CreateHeadersAuthProvider(create_cdp_headers),
    )
)

mw = payment_middleware_from_config(
    routes={
        "GET /v1/pro/lookup": {
            "accepts": [
                {
                    "scheme": "exact",
                    "network": NETWORK,
                    "payTo": PAY_TO,
                    "price": PRICE,
                }
            ],
            "description": "Calorie and nutrition lookup from a natural-language food description, sourced from USDA FoodData Central.",
            "mimeType": "application/json",
        }
    },
    facilitator_client=facilitator,
    schemes=[{"network": NETWORK, "server": ExactEvmServerScheme()}],
    sync_facilitator_on_start=True,
)

app = FastAPI(title="kcaldata paid (x402)", version="1.0.0")


@app.middleware("http")
async def x402_mw(request, call_next):
    return await mw(request, call_next)


@app.get("/")
def home():
    return {
        "name": "kcaldata paid endpoint (x402)",
        "paid_endpoint": "/v1/pro/lookup?query=banana",
        "network": NETWORK,
        "pay_to": PAY_TO,
        "price": PRICE,
    }


@app.get("/v1/pro/lookup")
def pro_lookup(query: str):
    # Reaching here means the x402 middleware already verified payment.
    try:
        r = httpx.get(KCALDATA_API, params={"query": query}, timeout=90)
        return JSONResponse(status_code=r.status_code, content=r.json())
    except Exception:
        return JSONResponse(status_code=502, content={"error": "upstream lookup failed"})
