#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AKO TV -> M3U converter
=======================

What this does
--------------
The AKO TV app (com.akomov.tv v2.4.7) pulls its channel list from a private
HTTP API at `https://ako-tv.com/api_v4/channels/channels.php`. The response is
NOT plain JSON and NOT M3U — it is **reversed-base64**: the server returns a
base64 string whose characters are reversed end-to-end, and reversing +
base64-decoding yields the real JSON (confirmed by the method
`decodeReversedBase64` inside classes.dex).

Most IPTV player apps (and the user's category list schema) expect a plain
M3U playlist URL. This script bridges that gap: it fetches the AKO TV channels
endpoint, decodes the response, parses the JSON, and writes a standard
`#EXTM3U` playlist the user can upload to their GitHub repo and reference as a
single category URL (matching the pattern of their other playlists:
ISLAMI.M3U, TRFEH.M3U, Aelatna_TV_Channels.m3u, etc.).

How to run
----------
Run this on a machine / network where ako-tv.com is reachable (Saudi Arabia,
Egypt, etc.). It cannot run from the analysis sandbox because the server is
geo-blocked there.

    pip install requests
    python3 scripts/ako_to_m3u.py

Outputs
-------
  - AKO_TV_channels.m3u                (the playlist, ready to upload)
  - AKO_TV_channels_decoded.json       (raw decoded JSON, for debugging)
  - AKO_TV_channels_summary.txt        (counts per category)

If the response format differs slightly from what we expect (field names,
nesting), the script still produces an M3U from whatever it can find and
prints what it saw so the user can adjust.
"""
import base64
import json
import os
import sys
import time
from collections import Counter, defaultdict
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    sys.exit("This script needs `requests`. Install it:  pip install requests")

# ---------------------------------------------------------------
# Constants extracted from the AKO TV APK (com.akomov.tv v2.4.7)
# ---------------------------------------------------------------
AKO_API_BASE = "https://ako-tv.com/api_v4"
AKO_PASS     = "12111998"          # hardcoded auth param from the APK
AKO_USER_AGENT = "okhttp/4.10.0"  # the app uses okhttp

# Channel-related endpoints (only these — user wants channels, not movies)
EP_CHANNELS_LIST   = "/channels/channels.php"
EP_CATEGORIES      = "/channels/categories.php"
EP_SUBCATEGORIES   = "/channels/subcategories.php"
EP_CHANNEL_SEARCH  = "/channels/search.php"

OUT_DIR = "/home/z/my-project/download"
os.makedirs(OUT_DIR, exist_ok=True)

# ---------------------------------------------------------------
# Core decoder: reversed-base64 -> JSON
# Mirrors com.akomov.tv's decodeReversedBase64()
# ---------------------------------------------------------------
def decode_reversed_base64(payload: str):
    """Reverse the string, then base64-decode it. Returns bytes.

    The AKO TV server returns responses where the base64 alphabet is reversed
    character-by-character (last char becomes first). Reversing restores a
    valid base64 string, which then decodes to UTF-8 JSON.
    """
    if not payload:
        raise ValueError("empty payload")
    payload = payload.strip()
    # Some endpoints pad with whitespace/newlines; strip them.
    reversed_payload = payload[::-1]
    # base64 must be a multiple of 4 chars; pad if needed.
    padding = (-len(reversed_payload)) % 4
    reversed_payload += "=" * padding
    try:
        decoded_bytes = base64.b64decode(reversed_payload, validate=False)
    except Exception as e:
        # Fallback: maybe the response is already plain JSON or already base64.
        raise ValueError(f"base64 decode failed after reversal: {e}")
    return decoded_bytes


def try_parse_json(decoded_bytes: bytes):
    """Try to parse the decoded bytes as JSON. Be tolerant of BOM/whitespace."""
    text = decoded_bytes.decode("utf-8", errors="replace").strip()
    # Strip a BOM if present
    if text.startswith("\ufeff"):
        text = text[1:]
    # Some responses wrap JSON in parentheses or quotes — try to peel them.
    for attempt in (text, text.lstrip("(").rstrip(")"),
                    text.strip('"').replace('\\"', '"')):
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            continue
    # Last resort: return the raw text so the caller can inspect it.
    return {"_raw_decoded_text": text}


def ako_get(path: str, extra_params: dict | None = None, timeout: int = 30):
    """Fetch an AKO TV endpoint and return (raw_text, decoded_json_or_None)."""
    params = {"v4": "1", "pass": AKO_PASS}
    if extra_params:
        params.update(extra_params)
    url = f"{AKO_API_BASE}{path}?{urlencode(params)}"
    print(f"[fetch] {url}")
    r = requests.get(url, headers={
        "User-Agent": AKO_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
    }, timeout=timeout)
    print(f"        HTTP {r.status_code}  {len(r.content)} bytes  "
          f"content-type={r.headers.get('Content-Type','?')}")
    r.raise_for_status()
    raw = r.text.strip()
    # Try decode; if it fails, return raw so we can inspect.
    try:
        decoded = try_parse_json(decode_reversed_base64(raw))
        return raw, decoded
    except Exception as e:
        print(f"        decode failed: {e}; returning raw text")
        return raw, None


# ---------------------------------------------------------------
# Channel field extraction (tolerant of schema variations)
# ---------------------------------------------------------------
# Field names we may see on a channel object. The DEX reads these generic
# keys across the media + channels APIs: name, url, media_url, embedURL,
# category, quality, season_name, season_url. For channels we also expect
# id/logo/image-like keys. Try every plausible variant.
NAME_KEYS   = ("name", "title", "channel_name", "channelName", "n")
URL_KEYS    = ("url", "stream_url", "streamUrl", "media_url", "mediaUrl",
               "playable_url", "playableUrl", "src", "link", "direct_url")
LOGO_KEYS   = ("logo", "image", "icon", "img", "thumb", "thumbnail",
               "poster", "tvg_logo", "logo_url", "logoUrl")
CAT_KEYS    = ("category", "category_name", "categoryName", "group",
               "group_title", "groupName", "cat", "type")
ID_KEYS     = ("id", "channel_id", "channelId", "_id", "pk")
EPG_KEYS    = ("epg_id", "epgId", "tvg_id", "tvg-id", "epg_channel_id")


def first_key(obj: dict, keys):
    for k in keys:
        if k in obj and obj[k] not in (None, ""):
            return obj[k]
    return None


def coerce_channels(decoded) -> list:
    """The decoded payload may be a list, a dict with .channels/.data/.items,
    or a dict mapping category->list. Normalize to a flat list of dicts."""
    if decoded is None:
        return []
    if isinstance(decoded, list):
        # Could be a list of channels, or a list of categories each with .items
        flat = []
        for item in decoded:
            if isinstance(item, dict):
                # If item has a list-bearing field, expand it
                for k in ("items", "channels", "list", "data", "streams"):
                    if isinstance(item.get(k), list):
                        for sub in item[k]:
                            if isinstance(sub, dict):
                                # Inherit category from parent if missing
                                if not first_key(sub, CAT_KEYS):
                                    cat = first_key(item, ("name", "title", "category"))
                                    if cat:
                                        sub.setdefault("_inherited_category", cat)
                                flat.append(sub)
                        break
                else:
                    flat.append(item)
        return flat
    if isinstance(decoded, dict):
        # Common wrappers
        for k in ("channels", "data", "items", "list", "result", "results",
                  "streams", "live"):
            if isinstance(decoded.get(k), list):
                return coerce_channels(decoded[k])
        # Maybe it's a dict of category_name -> [channels]
        flat = []
        for k, v in decoded.items():
            if isinstance(v, list):
                for sub in v:
                    if isinstance(sub, dict):
                        sub.setdefault("_inherited_category", k)
                        flat.append(sub)
        return flat
    return []


def build_m3u(channels: list) -> str:
    """Build a standard #EXTM3U playlist from the channel list."""
    lines = ["#EXTM3U"]
    n = 0
    skipped = 0
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        name = first_key(ch, NAME_KEYS)
        url  = first_key(ch, URL_KEYS)
        if not url:
            skipped += 1
            continue
        if not name:
            name = "Channel " + str(first_key(ch, ID_KEYS) or n + 1)
        logo = first_key(ch, LOGO_KEYS) or ""
        cat  = first_key(ch, CAT_KEYS) or ch.get("_inherited_category") or "AKO TV"
        epg  = first_key(ch, EPG_KEYS) or ""
        # Sanitize: m3u attributes are space-separated key="value"
        def q(v):
            return str(v).replace('"', "'").strip()
        attrs = []
        if epg:
            attrs.append(f'tvg-id="{q(epg)}"')
        if logo:
            attrs.append(f'tvg-logo="{q(logo)}"')
        attrs.append(f'group-title="{q(cat)}"')
        lines.append(f"#EXTINF:-1 {attrs} {q(name)}")
        lines.append(str(url))
        n += 1
    out = "\n".join(lines) + "\n"
    print(f"[m3u] wrote {n} channels (skipped {skipped} without a URL)")
    return out


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------
def main():
    print("=" * 70)
    print("AKO TV -> M3U converter")
    print("=" * 70)

    # Step 1: fetch the channel list
    print("\n[1/4] Fetching channel list from AKO TV...")
    try:
        raw_channels, decoded_channels = ako_get(EP_CHANNELS_LIST)
    except Exception as e:
        print(f"\nERROR: could not fetch {EP_CHANNELS_LIST}")
        print(f"  Reason: {e}")
        print("\nIf you see a connection timeout, you are on a network where")
        print("ako-tv.com is blocked. Run this script from Saudi Arabia /")
        print("Egypt / UAE, or via a VPN to one of those regions.")
        # Save raw error for inspection
        with open(os.path.join(OUT_DIR, "AKO_TV_fetch_error.txt"), "w",
                  encoding="utf-8") as f:
            f.write(str(e))
        sys.exit(2)

    # Save decoded JSON regardless so the user can inspect schema
    with open(os.path.join(OUT_DIR, "AKO_TV_channels_decoded.json"), "w",
              encoding="utf-8") as f:
        if decoded_channels is not None:
            json.dump(decoded_channels, f, ensure_ascii=False, indent=2)
        else:
            f.write(raw_channels)
    print(f"[2/4] Saved decoded JSON -> "
          f"{os.path.join(OUT_DIR, 'AKO_TV_channels_decoded.json')}")

    if decoded_channels is None:
        print("\nCould not decode the response as reversed-base64 JSON.")
        print("Inspect AKO_TV_channels_decoded.json (it contains the raw text)")
        print("and adjust decode_reversed_base64() if needed.")
        sys.exit(3)

    # Step 3: normalize to a flat channel list
    channels = coerce_channels(decoded_channels)
    print(f"[3/4] Normalized to {len(channels)} channel entries")

    if not channels:
        print("\nNo channels found in the decoded response. Inspect")
        print("AKO_TV_channels_decoded.json to see the actual structure,")
        print("then tweak coerce_channels() / build_m3u() field lookups.")
        sys.exit(4)

    # Step 4: build M3U
    m3u_text = build_m3u(channels)
    m3u_path = os.path.join(OUT_DIR, "AKO_TV_channels.m3u")
    with open(m3u_path, "w", encoding="utf-8") as f:
        f.write(m3u_text)
    print(f"[4/4] Saved M3U playlist -> {m3u_path}")

    # Summary by category
    summary = Counter()
    for ch in channels:
        cat = first_key(ch, CAT_KEYS) or ch.get("_inherited_category") or "(بدون تصنيف)"
        summary[cat] += 1
    summary_path = os.path.join(OUT_DIR, "AKO_TV_channels_summary.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"AKO TV channels summary — generated {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Total channels: {len(channels)}\n\n")
        f.write("Channels per category:\n")
        for cat, n in summary.most_common():
            f.write(f"  {n:4d}  {cat}\n")
    print(f"      Saved summary -> {summary_path}")

    print("\n" + "=" * 70)
    print("DONE. Next steps:")
    print("  1. Upload AKO_TV_channels.m3u to your GitHub repo")
    print("     (e.g. zezo81795-cell/IO/raw/refs/heads/main/AKO_TV_channels.m3u)")
    print("  2. Reference that raw URL in your IPTV categories JSON.")
    print("=" * 70)


if __name__ == "__main__":
    main()
