#!/usr/bin/env python3
"""
proxy_geo_check.py — Accurate proxy/VPN location checker.

WHY ONE API IS NOT ENOUGH
-------------------------
A geo lookup returns the ASN *registry* location, not the real exit location.
For residential / rotating / mobile proxies that is frequently a datacenter
city or simply the wrong one. Example measured here: 8.8.8.8 is reported as
Ashburn, Mountain View AND San Jose by three different providers.

WHAT IS ACTUALLY RELIABLE (measured)
------------------------------------
  country   -> yes, if you normalise the strings before voting
  ASN       -> yes
  proxy/VPN -> only as a *per-source flag*, sources disagree; report both
  city      -> NO. Agreement is 0.12-0.5 even for a well-known IP.
               Kept in the report for reference, never used in the verdict.

METHOD
------
  1. ECHO   route a request THROUGH the proxy to an echo service to learn the
            real exit IP (also detects chaining: exit IP != configured IP)
  2. GEO    look that IP up in 8 independent sources IN PARALLEL
            (direct, NOT through the proxy: these are IP-parameter lookups,
             routing them via the proxy only leaks the proxy's own geo)
  3. VOTE   normalise country strings, then majority-vote on country + ASN
  4. FLAGS  collect proxy / hosting / type flags per source and report them
            side by side instead of collapsing them into one boolean
  5. SCORE  confidence from cross-source agreement

USAGE
-----
  python proxy_geo_check.py --ip 45.155.204.10
  python proxy_geo_check.py --proxy socks5h://127.0.0.1:1080     # socks5h = DNS via proxy
  python proxy_geo_check.py --list proxies.txt --json out.json

  NOTE: use socks5h:// (not socks5://) so DNS is resolved through the proxy;
  with socks5:// the lookup resolves locally and can leak your own geo.
  Requires: pip install requests pysocks
"""

import argparse, json, sys, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

try:
    import requests
except ImportError:
    sys.exit("pip install requests pysocks")

UA = {"User-Agent": "curl/8.5.0"}

# --------------------------------------------------------------------------
# Geo sources. Each entry:
#   name -> (url, country_path, city_path, flag_keys, asn_key, tz_key)
# flag_keys are read verbatim from the payload and reported, never merged.
# --------------------------------------------------------------------------
SOURCES = {
    "ip-api": (
        "http://ip-api.com/json/{ip}?fields=status,country,countryCode,city,isp,org,as,proxy,hosting,mobile,timezone",
        ("country",), ("city",), ("proxy", "hosting", "mobile"), ("as",), ("timezone",)),
    "proxycheck": (
        "https://proxycheck.io/v2/{ip}?vpn=1&asn=1",
        ("country",), ("city",), ("proxy", "type"), ("asn",), ("timezone",)),
    "ipwho.is": (
        "https://ipwho.is/{ip}",
        ("country",), ("city",), None, ("connection", "asn"), ("timezone", "id")),
    "ipinfo": (
        "https://ipinfo.io/{ip}/json",
        ("country",), ("city",), None, ("org",), ("timezone",)),
    "freeipapi": (
        "https://freeipapi.com/api/json/{ip}",
        ("countryName",), ("cityName",), None, None, ("timeZone",)),
    "db-ip": (
        "https://api.db-ip.com/v2/free/{ip}",
        ("countryName",), ("city",), None, None, ("timeZone",)),
    "ipapi.is": (
        "https://api.ipapi.is/?q={ip}",
        ("country",), ("city",), None, ("asn",), ("timezone",)),
    "ipwhois.app": (
        "https://ipwhois.app/json/{ip}",
        ("country",), ("city",), None, ("asn",), ("timezone",)),
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


# --------------------------------------------------------------------------
def query_source(name, tpl, ip, timeout=15):
    """Look up an IP directly (NOT through the proxy — these are IP lookups)."""
    try:
        r = requests.get(tpl.format(ip=ip), timeout=timeout, headers=UA)
        if r.status_code != 200:
            return name, {"_http": r.status_code}
        try:
            return name, r.json()
        except ValueError:
            return name, {"_nonjson": r.text[:100]}
    except Exception as e:
        return name, {"_err": type(e).__name__}


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

    # --- step 2: parallel geo lookup, direct
    results = {}
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as ex:
        futs = [ex.submit(query_source, n, SOURCES[n][0], target) for n in SOURCES]
        for f in futs:
            n, j = f.result()
            results[n] = j
    report["raw"] = results

    # --- step 3/4: votes + per-source flags
    countries, cities, asns, tzs = [], [], [], []
    flags, per_source = {}, {}
    for n, j in results.items():
        if not isinstance(j, dict) or "_http" in j or "_err" in j or "_nonjson" in j:
            continue
        payload = j.get(target, j)
        if not isinstance(payload, dict):
            payload = j
        _, ck, cityk, fk, asnk, tzk = SOURCES[n]

        c = _dig(payload, ck)
        city = _dig(payload, cityk)
        asn = norm_asn(_dig(payload, asnk))
        tz = _dig(payload, tzk)
        if isinstance(tz, dict):
            tz = tz.get("id") or tz.get("name")
        if c:
            countries.append((n, norm_country(c)))
        if city:
            cities.append((n, str(city)))
        if asn:
            asns.append((n, asn))
        if tz:
            tzs.append((n, str(tz)))
        if fk:
            flags[n] = {k: _dig(payload, (k,)) for k in fk}
        per_source[n] = {"country": c, "city": city, "asn": asn, "tz": tz}

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

    report.update({
        "sources_ok": len(countries),
        "country_votes": dict(cc), "country": country, "country_agreement": c_agree,
        "asn_votes": dict(ac), "asn": asn, "asn_agreement": a_agree,
        "timezone_votes": dict(tzc), "timezone": tz, "timezone_agreement": t_agree,
        "city_votes": dict(cityc), "city": city, "city_agreement": city_agree,
        "flags": flags, "per_source": per_source,
    })

    # --- step 5: verdict
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

    report["verdict"] = {
        "country": country,
        "asn": asn,
        "timezone": tz,
        "city_unreliable": True,
        "city_reported": city,
        "flagged_as_proxy_vpn": bool(proxy_sources),
        "proxy_flagged_by": proxy_sources,
        "flagged_as_datacenter": bool(hosting_sources),
        "datacenter_flagged_by": hosting_sources,
        "source_disagreement": sorted({n for n, _ in countries}) and len(set(v for _, v in countries)) > 1,
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
            print(f"{p:<55} {v.get('country','?'):<3} {v.get('asn','?'):<12} "
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
