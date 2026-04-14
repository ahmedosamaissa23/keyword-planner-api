#!/usr/bin/env python3
"""
Keyword Planner MCP Server — ChatGPT MCP Integration.

An MCP server with SSE transport that exposes Google Ads Keyword Planner
tools for use in ChatGPT via the New App (MCP) feature.
"""

import os
import json
from typing import Optional, List, Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ConfigDict
from mcp.server.fastmcp import FastMCP

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

LANG_NAMES = {
    "en": "English", "ar": "Arabic", "es": "Spanish", "fr": "French",
    "de": "German", "pt": "Portuguese", "zh": "Chinese", "ja": "Japanese",
    "ko": "Korean", "hi": "Hindi", "tr": "Turkish", "it": "Italian",
    "ru": "Russian", "nl": "Dutch",
}

LOC_NAMES = {
    "us": "United States", "uk": "United Kingdom", "ca": "Canada",
    "au": "Australia", "de": "Germany", "fr": "France", "es": "Spain",
    "it": "Italy", "br": "Brazil", "in": "India", "jp": "Japan",
    "sa": "Saudi Arabia", "ae": "UAE", "eg": "Egypt", "mx": "Mexico",
    "tr": "Turkey", "kr": "South Korea", "nl": "Netherlands",
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
        raise RuntimeError("GOOGLE_ADS_CUSTOMER_ID not set")
    return cid


# ──────────────────────────────────────────
# Keyword Planner Logic
# ──────────────────────────────────────────

def _generate_ideas(keywords, url, language, locations, limit):
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


def _get_historical_metrics(keywords, language, locations):
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
# MCP Server
# ──────────────────────────────────────────

mcp = FastMCP("keyword_planner_mcp")


# ── Tool: Generate Keyword Ideas ──

class KeywordIdeasInput(BaseModel):
    """Input for generating keyword ideas."""
    model_config = ConfigDict(str_strip_whitespace=True)

    keywords: Optional[List[str]] = Field(
        default=None,
        description="Seed keywords, e.g. ['seo tools', 'digital marketing']. Provide keywords, a URL, or both."
    )
    url: Optional[str] = Field(
        default=None,
        description="URL to extract keyword ideas from, e.g. 'https://example.com/blog'"
    )
    language: str = Field(
        default="en",
        description="Language code: en, ar, es, fr, de, pt, zh, ja, ko, hi, tr, it, ru, nl"
    )
    locations: Optional[List[str]] = Field(
        default=None,
        description="Country codes: us, uk, ca, au, de, fr, es, it, br, in, jp, sa, ae, eg, mx, tr, kr, nl"
    )
    limit: int = Field(default=30, ge=1, le=100, description="Max number of results (1-100)")


@mcp.tool(
    name="generate_keyword_ideas",
    annotations={
        "title": "Generate Keyword Ideas",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def generate_keyword_ideas(params: KeywordIdeasInput) -> str:
    """Generate keyword suggestions from seed keywords or a webpage URL.

    Returns search volume, competition level, and CPC estimates for each keyword idea.
    Supports 14 languages and 18 countries.

    Args:
        params: Keyword ideas input with keywords, url, language, locations, and limit.

    Returns:
        JSON with total count and keyword_ideas array containing keyword, avg_monthly_searches,
        competition, competition_index, cpc_low, cpc_high for each result.
    """
    try:
        ideas = _generate_ideas(
            keywords=params.keywords,
            url=params.url,
            language=params.language,
            locations=params.locations or ["us"],
            limit=params.limit,
        )
        return json.dumps({"total": len(ideas), "keyword_ideas": ideas}, indent=2)
    except GoogleAdsException as ex:
        return f"Error: Google Ads API error: {ex.failure.errors[0].message}"
    except Exception as e:
        return f"Error: {str(e)}"


# ── Tool: Get Keyword Metrics ──

class KeywordMetricsInput(BaseModel):
    """Input for getting keyword historical metrics."""
    model_config = ConfigDict(str_strip_whitespace=True)

    keywords: List[str] = Field(
        ...,
        description="Keywords to get metrics for (max 20), e.g. ['digital marketing', 'content strategy']",
        min_length=1,
        max_length=20,
    )
    language: str = Field(default="en", description="Language code (e.g. 'en', 'ar')")
    locations: Optional[List[str]] = Field(default=None, description="Country codes (e.g. ['us', 'sa'])")


@mcp.tool(
    name="get_keyword_metrics",
    annotations={
        "title": "Get Keyword Historical Metrics",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def get_keyword_metrics(params: KeywordMetricsInput) -> str:
    """Get historical search volume, competition, CPC ranges, and monthly trends for specific keywords.

    Returns detailed metrics including monthly search volume history for each keyword.

    Args:
        params: Keywords list, language, and locations.

    Returns:
        JSON with total count and keyword_metrics array.
    """
    try:
        metrics = _get_historical_metrics(
            keywords=params.keywords,
            language=params.language,
            locations=params.locations or ["us"],
        )
        return json.dumps({"total": len(metrics), "keyword_metrics": metrics}, indent=2)
    except GoogleAdsException as ex:
        return f"Error: Google Ads API error: {ex.failure.errors[0].message}"
    except Exception as e:
        return f"Error: {str(e)}"


# ── Tool: Analyze Competition ──

class CompetitionInput(BaseModel):
    """Input for competition analysis."""
    model_config = ConfigDict(str_strip_whitespace=True)

    keywords: List[str] = Field(
        ...,
        description="Keywords to analyze competition for",
        min_length=1,
        max_length=20,
    )
    language: str = Field(default="en", description="Language code")
    locations: Optional[List[str]] = Field(default=None, description="Country codes")
    sort_by: str = Field(
        default="competition",
        description="Sort by: 'competition', 'volume', 'cpc_low', or 'cpc_high'"
    )


@mcp.tool(
    name="analyze_competition",
    annotations={
        "title": "Analyze Keyword Competition",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def analyze_competition(params: CompetitionInput) -> str:
    """Analyze keyword competition and identify opportunities.

    Returns competition data with an opportunity score (High/Medium/Low)
    based on search volume vs competition level.

    Args:
        params: Keywords, language, locations, and sort_by preference.

    Returns:
        JSON with total count and analysis array including opportunity scores.
    """
    try:
        metrics = _get_historical_metrics(
            keywords=params.keywords,
            language=params.language,
            locations=params.locations or ["us"],
        )

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

        sort_keys = {
            "competition": lambda x: x["competition_index"],
            "volume": lambda x: x["avg_monthly_searches"],
            "cpc_low": lambda x: x["cpc_low"],
            "cpc_high": lambda x: x["cpc_high"],
        }
        analysis.sort(key=sort_keys.get(params.sort_by, sort_keys["competition"]), reverse=True)

        return json.dumps({"total": len(analysis), "analysis": analysis}, indent=2)
    except GoogleAdsException as ex:
        return f"Error: Google Ads API error: {ex.failure.errors[0].message}"
    except Exception as e:
        return f"Error: {str(e)}"


# ── Tool: List Supported Targets ──

@mcp.tool(
    name="list_supported_targets",
    annotations={
        "title": "List Supported Languages & Locations",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def list_supported_targets() -> str:
    """List all supported language codes and country codes for keyword research.

    Returns:
        JSON with languages and locations arrays, each containing code, name, and google_id.
    """
    return json.dumps({
        "languages": [{"code": k, "name": LANG_NAMES.get(k, k), "google_id": v} for k, v in COMMON_LANGUAGES.items()],
        "locations": [{"code": k, "name": LOC_NAMES.get(k, k), "google_id": v} for k, v in COMMON_LOCATIONS.items()],
    }, indent=2)


# ──────────────────────────────────────────
# Entry Point
# ──────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    print(f"\n  Keyword Planner MCP Server starting on port {port}")
    print(f"  SSE endpoint: http://0.0.0.0:{port}/sse\n")
    mcp.run(transport="sse", host="0.0.0.0", port=port)
