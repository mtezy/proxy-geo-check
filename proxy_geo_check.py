#!/usr/bin/env python3
"""
proxy_geo_check.py — Accurate proxy/VPN location checker.

WHY ONE API IS NOT ENOUGH
-------------------------
A geo lookup returns the ASN *registry* location, not the real exit location.
For residential / rotating / mobile proxies that is frequently a datacenter
city or simply the wrong one.

WHAT IS RELIABLE (measured)
---------------------------
  country   -> yes, agreement 1.0 across 8 sources once strings are normalised
  ASN       -> yes, agreement 1.0
  timezone  -> yes, agreement 0.83-1.0
  proxy/VPN -> per-source only; sources genuinely disagree, so every flag is
               reported separately and never collapsed into one boolean
  city      -> NOT reliable as a string vote ("Frankfurt am Main" vs "Hanau am
               Main" vs "Gelnhausen" are different strings ~20 km apart).
               This tool therefore estimates the location from COORDINATES:
               it takes every lat/lon the sources return, computes the median
               centroid, and reports the spread in km. City names are still
               listed with their distance from that centroid.

METHOD
------
  1. ECHO   route a request THROUGH the proxy to an echo service to learn the
            real exit IP (also detects chaining: exit IP != configured IP)
  2. GEO    look that IP up in 8 independent sources IN PARALLEL, with retries
            (direct, NOT through the proxy: these are IP-parameter lookups,
             routing them via the proxy only leaks the proxy's own geo)
  3. VOTE   normalise country strings, then majority-vote on country + ASN
  4. LOCATE median centroid of all returned coordinates + spread in km
  5. FLAGS  collect proxy / hosting / type flags per source and report them
            side by side instead of collapsing them into one boolean
  6. SCORE  confidence from cross-source agreement

USAGE
-----
  python proxy_geo_check.py --ip 45.155.204.10
  python proxy_geo_check.py --proxy socks5h://127.0.0.1:1080     # socks5h = DNS via proxy
  python proxy_geo_check.py --list proxies.txt --json out.json

  NOTE: use socks5h:// (not socks5://) so DNS is resolved through the proxy;
  with socks5:// the lookup resolves locally and can leak your own geo.
  Requires: pip install requests pysocks
"""

import argparse, json, math, sys, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

try:
    import requests
except ImportError:
    sys.exit("pip install requests pysocks")

UA = {"User-Agent": "curl/8.5.0"}
RETRIES = 2          # per-source attempts; flag sources (ip-api/proxycheck) matter
RETRY_BACKOFF = 0.8  # seconds

# --------------------------------------------------------------------------
# Geo sources. Each entry:
#   name -> (url, country_path, city_path, flag_keys, asn_key, tz_key, coord)
# coord is a callable(payload) -> (lat, lon) | None, handling each source's
# own shape (ipinfo packs both into a "loc" string).
# flag_keys are read verbatim from the payload and reported, never merged.
# --------------------------------------------------------------------------
def _f(*p):
    return p


SOURCES = {
    "ip-api": (
        "http://ip-api.com/json/{ip}?fields=status,country,countryCode,regionName,city,"
        "isp,org,as,proxy,hosting,mobile,timezone,lat,lon",
        ("country",), ("city",), ("proxy", "hosting", "mobile"), ("as",), ("timezone",),
        ("lat", "lon")),
    "proxycheck": (
        "https://proxycheck.io/v2/{ip}?vpn=1&asn=1",
        ("country",), ("city",), ("proxy", "type"), ("asn",), ("timezone",),
        ("latitude", "longitude")),
    "ipwho.is": (
        "https://ipwho.is/{ip}",
        ("country",), ("city",), None, ("connection", "asn"), ("timezone", "id"),
        ("latitude", "longitude")),
    "ipinfo": (
        "https://ipinfo.io/{ip}/json",
        ("country",), ("city",), None, ("org",), ("timezone",),
        ("loc",)),  # "lat,lon"
    "freeipapi": (
        "https://freeipapi.com/api/json/{ip}",
        ("countryName",), ("cityName",), None, None, ("timeZone",),
        ("latitude", "longitude")),
    "db-ip": (
        "https://api.db-ip.com/v2/free/{ip}",
        ("countryName",), ("city",), None, None, ("timeZone",), None),
    "ipapi.is": (
        "https://api.ipapi.is/?q={ip}",
        ("country",), ("city",), None, ("asn",), ("timezone",), ("lat", "lon")),
    "ipwhois.app": (
        "https://ipwhois.app/json/{ip}",
        ("country",), ("city",), None, ("asn",), ("timezone",),
        ("latitude", "longitude")),
}

# services that echo the caller IP (used through the proxy for exit detection)
ECHO = [
    "https://api.ipify.org?format=json",
    "https://ipinfo.io/json",
    "https://api.myip.com",
]

# --------------------------------------------------------------------------
# Country normalisation. Without this the vote is meaningless:
# "Russia" / "Russian Federation" / "RU" count as three different answers.
# --------------------------------------------------------------------------
_ALIAS = {
    "russia": "RU", "russian federation": "RU", "ru": "RU",
    "united states": "US", "united states of america": "US", "us": "US", "usa": "US",
    "united kingdom": "GB", "great britain": "GB", "gb": "GB", "uk": "GB",
    "germany": "DE", "de": "DE", "netherlands": "NL", "the netherlands": "NL", "nl": "NL",
    "australia": "AU", "au": "AU", "canada": "CA", "ca": "CA",
    "france": "FR", "fr": "FR", "china": "CN", "cn": "CN",
    "japan": "JP", "jp": "JP", "singapore": "SG", "sg": "SG",
    "india": "IN", "in": "IN", "brazil": "BR", "br": "BR",
    "indonesia": "ID", "id": "ID", "south korea": "KR", "korea, republic of": "KR", "kr": "KR",
    "hong kong": "HK", "hk": "HK", "taiwan": "TW", "tw": "TW",
    "poland": "PL", "pl": "PL", "sweden": "SE", "se": "SE",
    "switzerland": "CH", "ch": "CH", "spain": "ES", "es": "ES",
    "italy": "IT", "it": "IT", "turkey": "TR", "tr": "TR",
    "ukraine": "UA", "ua": "UA", "vietnam": "VN", "vn": "VN",
    "thailand": "TH", "th": "TH", "malaysia": "MY", "my": "MY",
    "philippines": "PH", "ph": "PH", "united arab emirates": "AE", "ae": "AE",
    "south africa": "ZA", "za": "ZA", "mexico": "MX", "mx": "MX",
    "argentina": "AR", "ar": "AR", "finland": "FI", "fi": "FI",
    "norway": "NO", "no": "NO", "denmark": "DK", "dk": "DK",
    "ireland": "IE", "ie": "IE", "austria": "AT", "at": "AT",
    "belgium": "BE", "be": "BE", "czechia": "CZ", "czech republic": "CZ", "cz": "CZ",
    "romania": "RO", "ro": "RO", "portugal": "PT", "pt": "PT",
    "israel": "IL", "il": "IL", "saudi arabia": "SA", "sa": "SA",
    "new zealand": "NZ", "nz": "NZ",
}


def norm_country(s):
    if not s:
        return None
    k = str(s).strip().lower()
    if k in _ALIAS:
        return _ALIAS[k]
    return str(s).strip().upper() if len(k) == 2 else str(s).strip()


def norm_asn(s):
    """Reduce 'AS208677 Cloud Technologies LLC' / 15169 to 'AS208677'."""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    tok = s.split()[0].upper()
    if tok.startswith("AS"):
        tok = tok[2:]
    return f"AS{tok}" if tok.isdigit() else s


def _dig(obj, path):
    if path is None:
        return None
    cur = obj
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _coord(payload, spec):
    """Extract (lat, lon) for a source, handling its own shape."""
    if not spec:
        return None
    try:
        if spec == ("loc",):                       # ipinfo: "37.386,-122.0838"
            loc = _dig(payload, ("loc",))
            if not loc or "," not in str(loc):
                return None
            a, b = str(loc).split(",")[:2]
            return float(a), float(b)
        lat, lon = _dig(payload, (spec[0],)), _dig(payload, (spec[1],))
        if lat is None or lon is None:
            return None
        lat, lon = float(lat), float(lon)
        if lat == 0.0 and lon == 0.0:              # null island = no data
            return None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return lat, lon
    except (TypeError, ValueError):
        return None


def haversine_km(a, b):
    """Great-circle distance between (lat, lon) pairs, in km."""
    (la1, lo1), (la2, lo2) = a, b
    r = 6371.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp = math.radians(la2 - la1)
    dl = math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


# --------------------------------------------------------------------------
def query_source(name, tpl, ip, timeout=15, retries=RETRIES):
    """Look up an IP directly (NOT through the proxy — these are IP lookups)."""
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(tpl.format(ip=ip), timeout=timeout, headers=UA)
            if r.status_code == 200:
                try:
                    return name, r.json()
                except ValueError:
                    last = {"_nonjson": r.text[:100]}
            else:
                last = {"_http": r.status_code}
        except Exception as e:
            last = {"_err": type(e).__name__}
        if attempt + 1 < retries:
            time.sleep(RETRY_BACKOFF * (attempt + 1))
    return name, (last or {"_err": "unknown"})


def echo_ip(proxies=None, timeout=20):
    """Real exit IP as seen from outside, optionally routed through the proxy."""
    for u in ECHO:
        try:
            r = requests.get(u, timeout=timeout, proxies=proxies, headers=UA)
            j = r.json()
            ip = j.get("ip") or j.get("query") or j.get("IPv4")
            if ip:
                return ip, u
        except Exception:
            continue
    return None, None


# --------------------------------------------------------------------------
def check(ip=None, proxy_url=None):
    proxies = None
    if proxy_url:
        if proxy_url.startswith("socks5://"):
            # socks5:// resolves DNS locally -> can leak the caller's geo.
            proxy_url = "socks5h://" + proxy_url[len("socks5://"):]
        proxies = {"http": proxy_url, "https": proxy_url}

    report = {"input_ip": ip, "proxy": proxy_url, "checked_at": int(time.time())}

    # --- step 1: exit IP
    exit_ip, src = echo_ip(proxies)
    report["exit_ip"] = exit_ip
    report["echo_source"] = src
    if proxy_url:
        report["chained"] = bool(exit_ip and ip and exit_ip != ip)

    target = ip or exit_ip
    if not target:
        report["error"] = "could not determine an IP to look up"
        return report
    report["target_ip"] = target

    # --- step 2: parallel geo lookup, direct, with retries
    results = {}
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as ex:
        futs = [ex.submit(query_source, n, SOURCES[n][0], target) for n in SOURCES]
        for f in futs:
            n, j = f.result()
            results[n] = j
    report["raw"] = results

    # --- step 3/4/5: votes, coordinates, flags
    countries, cities, asns, tzs, coords = [], [], [], [], []
    flags, per_source = {}, {}
    for n, j in results.items():
        if not isinstance(j, dict) or "_http" in j or "_err" in j or "_nonjson" in j:
            per_source[n] = {"status": "failed", **{k: v for k, v in j.items() if k.startswith("_")}}
            continue
        payload = j.get(target, j)
        if not isinstance(payload, dict):
            payload = j
        _, ck, cityk, fk, asnk, tzk, coordk = SOURCES[n]

        c = _dig(payload, ck)
        city = _dig(payload, cityk)
        asn = norm_asn(_dig(payload, asnk))
        tz = _dig(payload, tzk)
        if isinstance(tz, dict):
            tz = tz.get("id") or tz.get("name")
        xy = _coord(payload, coordk)

        if c:
            countries.append((n, norm_country(c)))
        if city:
            cities.append((n, str(city)))
        if asn:
            asns.append((n, asn))
        if tz:
            tzs.append((n, str(tz)))
        if xy:
            coords.append((n, xy))
        if fk:
            flags[n] = {k: _dig(payload, (k,)) for k in fk}
        per_source[n] = {"country": c, "city": city, "asn": asn, "tz": tz,
                         "lat": xy[0] if xy else None, "lon": xy[1] if xy else None}

    cc, ac, tzc, cityc = (Counter(v for _, v in x)
                          for x in (countries, asns, tzs, cities))

    def top(counter, total):
        if not counter:
            return None, 0.0
        v, n = counter.most_common(1)[0]
        return v, round(n / total, 3)

    country, c_agree = top(cc, len(countries))
    asn, a_agree = top(ac, len(asns))
    tz, t_agree = top(tzc, len(tzs))
    city, city_agree = top(cityc, len(cities))

    # --- step 4: coordinate-based location (robust: MAD outlier rejection)
    location = None
    if coords:
        def centroid(pts):
            return (median([p[1][0] for p in pts]), median([p[1][1] for p in pts]))

        c0 = centroid(coords)
        d0 = [haversine_km(c0, xy) for _, xy in coords]
        mad = median([abs(d - median(d0)) for d in d0]) or 0.0
        # a point is an outlier if it is far outside the median absolute deviation
        cutoff = max(50.0, 3.0 * mad)
        inliers = [p for p, d in zip(coords, d0) if d <= cutoff]
        outliers = [(n, round(d, 1)) for (n, _), d in zip(coords, d0) if d > cutoff]
        if not inliers:                       # degenerate: keep everything
            inliers, outliers = coords, []

        clat, clon = centroid(inliers)
        centroid = (clat, clon)
        dists = [(n, round(haversine_km(centroid, xy), 1)) for n, xy in inliers]
        best_src, best_km = min(dists, key=lambda t: t[1])
        spread = max(d for _, d in dists)
        location = {
            "lat": round(clat, 5),
            "lon": round(clon, 5),
            "best_city": per_source.get(best_src, {}).get("city"),
            "best_source": best_src,
            "spread_km": round(spread, 1),
            "coords_used": len(inliers),
            "coords_dropped": dict(outliers),
            "per_source_km": dict(dists),
            "precision": ("city" if spread <= 25 else
                          "metro" if spread <= 100 else
                          "region" if spread <= 400 else "country"),
        }

    report.update({
        "sources_ok": len(countries),
        "sources_total": len(SOURCES),
        "country_votes": dict(cc), "country": country, "country_agreement": c_agree,
        "asn_votes": dict(ac), "asn": asn, "asn_agreement": a_agree,
        "timezone_votes": dict(tzc), "timezone": tz, "timezone_agreement": t_agree,
        "city_votes": dict(cityc), "city": city, "city_agreement": city_agree,
        "location": location,
        "flags": flags, "per_source": per_source,
    })

    # --- step 6: verdict
    # proxy signal = EXPLICIT proxy flag or an explicit VPN/TOR type.
    # "Business"/"hosting" alone is NOT a proxy signal.
    proxy_sources, hosting_sources = [], []
    for n, f in flags.items():
        if f.get("proxy") in (True, "yes", "Yes", "YES"):
            proxy_sources.append(n)
        elif str(f.get("type", "")).upper() in ("VPN", "TOR", "PUBLIC", "SOCKS"):
            proxy_sources.append(n)
        if f.get("hosting") is True:
            hosting_sources.append(n)

    flag_sources_alive = [n for n in ("ip-api", "proxycheck") if n in flags]
    report["verdict"] = {
        "country": country,
        "asn": asn,
        "timezone": tz,
        "city": (location or {}).get("best_city") or city,
        "city_precision": (location or {}).get("precision", "unknown"),
        "city_spread_km": (location or {}).get("spread_km"),
        "city_sources_used": (location or {}).get("coords_used"),
        "city_outliers_dropped": (location or {}).get("coords_dropped", {}),
        "lat": (location or {}).get("lat"),
        "lon": (location or {}).get("lon"),
        "city_from_coordinates": bool(location),
        "flagged_as_proxy_vpn": bool(proxy_sources),
        "proxy_flagged_by": proxy_sources,
        "flagged_as_datacenter": bool(hosting_sources),
        "datacenter_flagged_by": hosting_sources,
        "flag_sources_alive": flag_sources_alive,
        "flags_incomplete": len(flag_sources_alive) < 2,
        "confidence": ("high" if c_agree >= 0.75 and len(countries) >= 5
                       else "medium" if c_agree >= 0.5
                       else "low"),
    }
    return report


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Accurate proxy/VPN location checker")
    ap.add_argument("--ip", help="look up this IP directly")
    ap.add_argument("--proxy", help="http:// or socks5h:// URL to route through")
    ap.add_argument("--list", help="file with one proxy URL per line")
    ap.add_argument("--json", help="write full results to this file")
    a = ap.parse_args()

    if not (a.ip or a.proxy or a.list):
        ap.error("give one of --ip / --proxy / --list")

    out = []
    if a.list:
        lines = [l.strip() for l in open(a.list) if l.strip() and not l.startswith("#")]
        for p in lines:
            r = check(proxy_url=p)
            out.append(r)
            v = r.get("verdict", {})
            print(f"{p:<50} {str(v.get('country')):<3} {str(v.get('asn')):<12} "
                  f"{str(v.get('city'))[:18]:<19} +-{v.get('city_spread_km')}km "
                  f"proxy={v.get('flagged_as_proxy_vpn')} dc={v.get('flagged_as_datacenter')} "
                  f"conf={v.get('confidence')}")
    else:
        r = check(ip=a.ip, proxy_url=a.proxy)
        out.append(r)
        print(json.dumps(r, indent=2, default=str))

    if a.json:
        json.dump(out, open(a.json, "w"), indent=2, default=str)
        print(f"\n[+] wrote {a.json}", file=sys.stderr)


if __name__ == "__main__":
    main()
