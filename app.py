#!/usr/bin/env python3
"""
Keyword Planner API — ChatGPT Custom GPT Actions Backend.

A FastAPI server that exposes Google Ads Keyword Planner as REST endpoints,
designed to be used as Actions in a ChatGPT Custom GPT.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

load_dotenv()

# ──────────────────────────────────────────
# Constants
# ──────────────────────────────────────────

COMMON_LANGUAGES = {
    "en": "1000", "ar": "1019", "es": "1003", "fr": "1002",
    "de": "1001", "pt": "1014", "zh": "1017", "ja": "1005",
    "ko": "1012", "hi": "1023", "tr": "1037", "it": "1004",
    "ru": "1031", "nl": "1010",
}

COMMON_LOCATIONS = {
    "us": "2840", "uk": "2826", "ca": "2124", "au": "2036",
    "de": "2276", "fr": "2250", "es": "2724", "it": "2380",
    "br": "2076", "in": "2356", "jp": "2392", "sa": "2682",
    "ae": "2784", "eg": "2818", "mx": "2484", "tr": "2792",
    "kr": "2410", "nl": "2528",
}


def _resolve_language(lang: str) -> str:
    if lang.isdigit():
        return lang
    return COMMON_LANGUAGES.get(lang.lower(), "1000")


def _resolve_locations(locations: list[str] | None) -> list[str]:
    if not locations:
        return ["2840"]
    return [COMMON_LOCATIONS.get(loc.lower(), loc) if not loc.isdigit() else loc for loc in locations]


def _micros_to_dollars(micros: int | None) -> float:
    if not micros:
        return 0.0
    return round(micros / 1_000_000, 2)


# ──────────────────────────────────────────
# Google Ads Client
# ──────────────────────────────────────────

_google_client: GoogleAdsClient | None = None


def _get_google_client() -> GoogleAdsClient:
    global _google_client
    if _google_client is None:
        credentials = {
            "developer_token": os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
            "client_id": os.getenv("GOOGLE_ADS_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
            "refresh_token": os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
        }
        login_cid = os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID")
        if login_cid:
            credentials["login_customer_id"] = login_cid.replace("-", "")

        missing = [k for k, v in credentials.items() if not v]
        if missing:
            raise RuntimeError(
                f"Missing environment variables: {', '.join('GOOGLE_ADS_' + k.upper() for k in missing)}. "
                f"Check your .env file."
            )
        credentials["use_proto_plus"] = True
        _google_client = GoogleAdsClient.load_from_dict(credentials)
    return _google_client


def _get_customer_id() -> str:
    cid = os.getenv("GOOGLE_ADS_CUSTOMER_ID", "").replace("-", "")
    if not cid:
        raise RuntimeError("GOOGLE_ADS_CUSTOMER_ID not set in .env")
    return cid


# ──────────────────────────────────────────
# Keyword Planner Logic
# ──────────────────────────────────────────

def _generate_ideas(
    keywords: list[str] | None,
    url: str | None,
    language: str,
    locations: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """Generate keyword ideas from seeds or URL."""
    client = _get_google_client()
    customer_id = _get_customer_id()
    service = client.get_service("KeywordPlanIdeaService")
    ga_service = client.get_service("GoogleAdsService")

    request = client.get_type("GenerateKeywordIdeasRequest")
    request.customer_id = customer_id
    request.language = ga_service.language_constant_path(_resolve_language(language))

    for loc_id in _resolve_locations(locations):
        request.geo_target_constants.append(ga_service.geo_target_constant_path(loc_id))

    request.keyword_plan_network = client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH_AND_PARTNERS

    if keywords and url:
        request.keyword_and_url_seed.url = url
        for kw in keywords:
            request.keyword_and_url_seed.keywords.append(kw)
    elif keywords:
        for kw in keywords:
            request.keyword_seed.keywords.append(kw)
    elif url:
        request.url_seed.url = url
    else:
        raise ValueError("Provide at least one keyword or a URL.")

    response = service.generate_keyword_ideas(request=request)

    results = []
    for i, idea in enumerate(response):
        if i >= limit:
            break
        m = idea.keyword_idea_metrics
        results.append({
            "keyword": idea.text,
            "avg_monthly_searches": m.avg_monthly_searches or 0,
            "competition": m.competition.name if m.competition else "UNSPECIFIED",
            "competition_index": m.competition_index or 0,
            "cpc_low": _micros_to_dollars(m.low_top_of_page_bid_micros),
            "cpc_high": _micros_to_dollars(m.high_top_of_page_bid_micros),
        })

    return results


def _get_historical_metrics(
    keywords: list[str],
    language: str,
    locations: list[str],
) -> list[dict[str, Any]]:
    """Get historical metrics for specific keywords."""
    client = _get_google_client()
    customer_id = _get_customer_id()
    service = client.get_service("KeywordPlanIdeaService")
    ga_service = client.get_service("GoogleAdsService")

    request = client.get_type("GenerateKeywordHistoricalMetricsRequest")
    request.customer_id = customer_id
    request.language = ga_service.language_constant_path(_resolve_language(language))

    for loc_id in _resolve_locations(locations):
        request.geo_target_constants.append(ga_service.geo_target_constant_path(loc_id))

    for kw in keywords:
        request.keywords.append(kw)

    request.keyword_plan_network = client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH_AND_PARTNERS
    response = service.generate_keyword_historical_metrics(request=request)

    results = []
    for r in response.results:
        m = r.keyword_metrics
        monthly = []
        if m.monthly_search_volumes:
            for vol in m.monthly_search_volumes:
                monthly.append({
                    "year": vol.year,
                    "month": vol.month.name if vol.month else "UNKNOWN",
                    "searches": vol.monthly_searches or 0,
                })

        results.append({
            "keyword": r.text,
            "avg_monthly_searches": m.avg_monthly_searches or 0,
            "competition": m.competition.name if m.competition else "UNSPECIFIED",
            "competition_index": m.competition_index or 0,
            "cpc_low": _micros_to_dollars(m.low_top_of_page_bid_micros),
            "cpc_high": _micros_to_dollars(m.high_top_of_page_bid_micros),
            "monthly_search_volumes": monthly,
        })

    return results


# ──────────────────────────────────────────
# Pydantic Models (Request & Response)
# ──────────────────────────────────────────

class KeywordIdeasRequest(BaseModel):
    """Request body for keyword idea generation."""
    keywords: Optional[list[str]] = Field(
        default=None,
        description="Seed keywords (e.g., ['seo tools', 'keyword research']). Provide keywords, a URL, or both.",
        json_schema_extra={"example": ["digital marketing", "seo strategy"]},
    )
    url: Optional[str] = Field(
        default=None,
        description="URL to extract keyword ideas from.",
        json_schema_extra={"example": "https://example.com/blog"},
    )
    language: str = Field(
        default="en",
        description="Language code: en, ar, es, fr, de, pt, zh, ja, ko, hi, tr, it, ru, nl",
    )
    locations: Optional[list[str]] = Field(
        default=None,
        description="Country codes: us, uk, ca, au, de, fr, es, it, br, in, jp, sa, ae, eg, mx, tr, kr, nl",
        json_schema_extra={"example": ["us"]},
    )
    limit: int = Field(default=30, ge=1, le=100, description="Max results (1-100).")


class KeywordMetricsRequest(BaseModel):
    """Request body for keyword historical metrics."""
    keywords: list[str] = Field(
        ...,
        description="Keywords to get metrics for (max 20).",
        json_schema_extra={"example": ["digital marketing", "content strategy"]},
        min_length=1,
        max_length=20,
    )
    language: str = Field(default="en", description="Language code (e.g., 'en', 'ar').")
    locations: Optional[list[str]] = Field(
        default=None,
        description="Country codes (e.g., ['us', 'sa']).",
    )


class KeywordIdea(BaseModel):
    keyword: str
    avg_monthly_searches: int
    competition: str
    competition_index: int
    cpc_low: float
    cpc_high: float


class KeywordMetric(BaseModel):
    keyword: str
    avg_monthly_searches: int
    competition: str
    competition_index: int
    cpc_low: float
    cpc_high: float
    monthly_search_volumes: list[dict[str, Any]] = []


class KeywordIdeasResponse(BaseModel):
    total: int
    keyword_ideas: list[KeywordIdea]


class KeywordMetricsResponse(BaseModel):
    total: int
    keyword_metrics: list[KeywordMetric]


class CompetitionAnalysis(BaseModel):
    keyword: str
    avg_monthly_searches: int
    competition: str
    competition_index: int
    cpc_low: float
    cpc_high: float
    opportunity: str = Field(description="Opportunity level: High, Medium, or Low")


class CompetitionResponse(BaseModel):
    total: int
    analysis: list[CompetitionAnalysis]


# ──────────────────────────────────────────
# FastAPI App
# ──────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Validate config on startup."""
    api_key = os.getenv("API_KEY")
    if not api_key:
        print("WARNING: API_KEY not set. Your API is unprotected!")
    yield


app = FastAPI(
    title="Keyword Planner API",
    description=(
        "Google Ads Keyword Planner API for ChatGPT. "
        "Provides keyword research, search volume data, competition analysis, "
        "and CPC estimates for SEO and PPC campaigns."
    ),
    version="1.0.0",
    lifespan=lifespan,
    servers=[
        {"url": "https://your-domain.com", "description": "Production server"},
        {"url": "http://localhost:8000", "description": "Local development"},
    ],
)

# CORS — allow ChatGPT to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://chat.openai.com", "https://chatgpt.com", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Key security
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: str = Security(api_key_header)):
    """Verify the API key if one is configured."""
    expected = os.getenv("API_KEY")
    if expected and api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")
    return api_key


# ──────────────────────────────────────────
# API Endpoints
# ──────────────────────────────────────────

@app.post(
    "/keyword-ideas",
    response_model=KeywordIdeasResponse,
    summary="Generate keyword ideas",
    description="Generate keyword suggestions from seed keywords or a webpage URL. Returns search volume, competition, and CPC data.",
    tags=["Keyword Research"],
)
async def generate_keyword_ideas(
    body: KeywordIdeasRequest,
    _key: str = Security(verify_api_key),
):
    try:
        ideas = _generate_ideas(
            keywords=body.keywords,
            url=body.url,
            language=body.language,
            locations=body.locations or ["us"],
            limit=body.limit,
        )
        return KeywordIdeasResponse(total=len(ideas), keyword_ideas=ideas)
    except GoogleAdsException as ex:
        raise HTTPException(status_code=502, detail=f"Google Ads API error: {ex.failure.errors[0].message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    "/keyword-metrics",
    response_model=KeywordMetricsResponse,
    summary="Get keyword historical metrics",
    description="Get historical search volume, competition level, CPC ranges, and monthly trends for specific keywords.",
    tags=["Keyword Research"],
)
async def get_keyword_metrics(
    body: KeywordMetricsRequest,
    _key: str = Security(verify_api_key),
):
    try:
        metrics = _get_historical_metrics(
            keywords=body.keywords,
            language=body.language,
            locations=body.locations or ["us"],
        )
        return KeywordMetricsResponse(total=len(metrics), keyword_metrics=metrics)
    except GoogleAdsException as ex:
        raise HTTPException(status_code=502, detail=f"Google Ads API error: {ex.failure.errors[0].message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    "/competition-analysis",
    response_model=CompetitionResponse,
    summary="Analyze keyword competition",
    description="Analyze keyword competition and identify opportunities. Returns competition data with an opportunity score (High/Medium/Low).",
    tags=["Keyword Research"],
)
async def analyze_competition(
    body: KeywordMetricsRequest,
    sort_by: str = Query(
        default="competition",
        description="Sort by: competition, volume, cpc_low, cpc_high",
        enum=["competition", "volume", "cpc_low", "cpc_high"],
    ),
    _key: str = Security(verify_api_key),
):
    try:
        metrics = _get_historical_metrics(
            keywords=body.keywords,
            language=body.language,
            locations=body.locations or ["us"],
        )

        # Add opportunity scores
        analysis = []
        for m in metrics:
            volume = m["avg_monthly_searches"]
            comp_idx = m["competition_index"]

            if volume > 1000 and comp_idx < 40:
                opportunity = "High"
            elif volume > 500 and comp_idx < 60:
                opportunity = "Medium"
            else:
                opportunity = "Low"

            analysis.append({**m, "opportunity": opportunity})

        # Sort
        sort_keys = {
            "competition": lambda x: x["competition_index"],
            "volume": lambda x: x["avg_monthly_searches"],
            "cpc_low": lambda x: x["cpc_low"],
            "cpc_high": lambda x: x["cpc_high"],
        }
        analysis.sort(key=sort_keys.get(sort_by, sort_keys["competition"]), reverse=True)

        return CompetitionResponse(total=len(analysis), analysis=analysis)
    except GoogleAdsException as ex:
        raise HTTPException(status_code=502, detail=f"Google Ads API error: {ex.failure.errors[0].message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/supported-targets",
    summary="List supported languages and locations",
    description="Returns all supported language codes and country codes you can use in keyword research requests.",
    tags=["Reference"],
)
async def list_supported_targets():
    lang_names = {
        "en": "English", "ar": "Arabic", "es": "Spanish", "fr": "French",
        "de": "German", "pt": "Portuguese", "zh": "Chinese", "ja": "Japanese",
        "ko": "Korean", "hi": "Hindi", "tr": "Turkish", "it": "Italian",
        "ru": "Russian", "nl": "Dutch",
    }
    loc_names = {
        "us": "United States", "uk": "United Kingdom", "ca": "Canada",
        "au": "Australia", "de": "Germany", "fr": "France", "es": "Spain",
        "it": "Italy", "br": "Brazil", "in": "India", "jp": "Japan",
        "sa": "Saudi Arabia", "ae": "UAE", "eg": "Egypt", "mx": "Mexico",
        "tr": "Turkey", "kr": "South Korea", "nl": "Netherlands",
    }

    return {
        "languages": [{"code": k, "name": lang_names.get(k, k), "google_id": v} for k, v in COMMON_LANGUAGES.items()],
        "locations": [{"code": k, "name": loc_names.get(k, k), "google_id": v} for k, v in COMMON_LOCATIONS.items()],
    }


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


# ──────────────────────────────────────────
# Entry Point
# ──────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    print(f"\n  Keyword Planner API starting on http://{host}:{port}")
    print(f"  OpenAPI docs: http://{host}:{port}/docs")
    print(f"  OpenAPI JSON: http://{host}:{port}/openapi.json\n")
    uvicorn.run(app, host=host, port=port)
