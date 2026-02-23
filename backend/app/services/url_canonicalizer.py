"""
URL normalization and ID generation for Rakuten product URLs.

Handles:
- Stripping tracking params (rafcid, scid, etc.)
- Extracting shopCode + itemCode from item URLs
- Extracting variantId from query string
- Building stable IDs that don't depend on JAN
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlparse

# Tracking / affiliate query params to strip
_STRIP_PARAMS = frozenset({
    "rafcid", "scid", "s-id", "l-id", "ref", "iasid",
    "noel", "tag", "force-hierarchical", "pf",
    "wsc_i_is", "wsc_i_is_c",
})

# Regex to extract shop_code from various Rakuten URLs
_SHOP_URL_RE = re.compile(
    r"https?://search\.rakuten\.co\.jp/search/mall/[^?]*\?.*sid=(\d+)"
)


def canonicalize_item_url(url: str) -> tuple[str, str | None]:
    """
    Normalize a Rakuten item URL.

    Returns:
        (canonical_url, variant_id)

    Examples:
        Input:  https://item.rakuten.co.jp/rakuten24/404953/?variantId=4901301445520&rafcid=xxx
        Output: ("https://item.rakuten.co.jp/rakuten24/404953/", "4901301445520")
    """
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=False)

    # Extract variantId before stripping
    variant_id: str | None = None
    variant_list = params.pop("variantId", None)
    if variant_list:
        variant_id = variant_list[0]

    # Remove tracking params
    cleaned_params = {
        k: v for k, v in params.items()
        if k.lower() not in _STRIP_PARAMS
    }

    # Normalize path: ensure trailing slash, lowercase hostname
    hostname = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/") + "/"

    canonical = f"{parsed.scheme}://{hostname}{path}"
    if cleaned_params:
        canonical += "?" + urlencode(cleaned_params, doseq=True)

    return canonical, variant_id


def extract_shop_item_code(url: str) -> tuple[str, str]:
    """
    Extract (shop_code, item_code) from a canonical item URL.

    URL pattern: https://item.rakuten.co.jp/{shop_code}/{item_code}/

    Raises:
        ValueError: if URL doesn't match expected pattern
    """
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]

    if len(parts) >= 2:
        return parts[0], parts[1]

    raise ValueError(f"Cannot extract shop/item from URL: {url}")


def extract_shop_code_from_search_url(url: str) -> str | None:
    """
    Extract shop_code (sid) from a Rakuten search/mall URL.

    Example:
        https://search.rakuten.co.jp/search/mall/?sid=261122  -> "261122"
    """
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    sid_list = params.get("sid")
    if sid_list:
        return sid_list[0]

    m = _SHOP_URL_RE.match(url)
    if m:
        return m.group(1)

    return None


def make_item_id_str(shop_code: str, item_code: str) -> str:
    """
    Build a stable string-based item ID.

    Format: "{shop_code}:{item_code}"
    """
    return f"{shop_code}:{item_code}"


def make_variant_id_str(
    shop_code: str, item_code: str, variant_code: str | None = None
) -> str:
    """
    Build a stable string-based variant ID.

    Format: "{shop_code}:{item_code}:{variant_code}"
    Single (non-variant) products use variant_code = "default".
    """
    vc = variant_code or "default"
    return f"{shop_code}:{item_code}:{vc}"


def build_search_url(
    genre_id: str | None = None,
    keyword: str | None = None,
    shop_code: str | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    page: int = 1,
    sort: str | None = None,
) -> str:
    """
    Build a Rakuten search URL with optional price/page/sort params.

    Sort values:
      - "s=2" → price ascending (安い順)
      - "s=3" → price descending (高い順)
      - "s=4" → review count
      - None   → relevance (default)

    Examples:
        genre:   https://search.rakuten.co.jp/search/mall/-/568199/?min=0&max=110&p=1&s=2
        shop:    https://search.rakuten.co.jp/search/mall/?sid=261122&p=1
        keyword: https://search.rakuten.co.jp/search/mall/ミルク/100939/?p=1
    """
    base = "https://search.rakuten.co.jp/search/mall/"

    if keyword and genre_id:
        path = f"{base}{keyword}/{genre_id}/"
    elif genre_id:
        path = f"{base}-/{genre_id}/"
    elif keyword:
        path = f"{base}{keyword}/"
    else:
        path = base

    params: dict[str, str | int] = {}
    if shop_code:
        params["sid"] = shop_code
    if min_price is not None:
        params["min"] = min_price
    if max_price is not None:
        params["max"] = max_price
    if sort is not None:
        params["s"] = sort
    if page > 1:
        params["p"] = page

    if params:
        return path + "?" + urlencode(params)
    return path


def clean_tracking_params(url: str) -> str:
    """
    Strip tracking/affiliate params from any Rakuten URL.

    Works for both item URLs and shop URLs.
    Example:
        Input:  https://www.rakuten.co.jp/horiman/?rafcid=wsc_i_is_xxx
        Output: https://www.rakuten.co.jp/horiman/
    """
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=False)

    cleaned_params = {
        k: v for k, v in params.items()
        if k.lower() not in _STRIP_PARAMS
    }

    hostname = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/") + "/"

    result = f"{parsed.scheme}://{hostname}{path}"
    if cleaned_params:
        result += "?" + urlencode(cleaned_params, doseq=True)

    return result


def parse_api_item_code(api_item_code: str) -> tuple[str, str]:
    """
    Parse the API's itemCode format "shopCode:productId".

    Example:
        "horiman:10009058" → ("horiman", "10009058")
    """
    if ":" in api_item_code:
        parts = api_item_code.split(":", 1)
        return parts[0], parts[1]
    return "", api_item_code


def extract_store_code_from_item_page(item_url: str) -> str | None:
    """
    Given an item URL like https://item.rakuten.co.jp/rakuten24/69008/,
    extract the store code by fetching the shop page.

    For the actual numeric store code (sid), we need to look it up from the HTML.
    This function returns the URL-based shop identifier (e.g. "rakuten24").
    The numeric sid mapping requires a separate lookup or scrape.
    """
    try:
        shop_code, _ = extract_shop_item_code(item_url)
        return shop_code
    except ValueError:
        return None
