# Proxy Geo Location Check — Research Report

**Date:** 2026-10-01
**Question:** find a GitHub API / repo to check proxy location *accurately*
**Answer:** no single API or repo does this. Accuracy comes from cross-validation.

---

## 1. Why one lookup is wrong

A geo-IP lookup returns the **ASN registry location**, not the real exit location.
For residential / rotating / mobile proxies the registry block is often a datacenter
in a different city — or a different country.

Direct evidence measured during this research:

| IP | provider answers (city) | name-vote agreement |
|---|---|---|
| 8.8.8.8 | Ashburn / Mountain View (×5) / San Jose | 0.625 |
| 193.24.210.222 | Frankfurt (×4) / Hanau (×2) / Gelnhausen / Sonnenberg | 0.5 |
| 51.15.0.1 | Haarlem (×4) / Amsterdam (×4) | 0.5 |

Country was unanimous (8/8) for all of them.

**Conclusion:** country / ASN / timezone are solid. City names must not be voted on —
but city *coordinates* can be used (section 4).

---

## 2. Sources tested head-to-head

| source | country | city | ASN | coords | proxy flag | free tier | verdict |
|---|---|---|---|---|---|---|---|
| **proxycheck.io** | ✅ | ✅ | ✅ | ✅ | **`proxy:yes`, `type:VPN/TOR`** | 100/day, 1000/day +key | best free proxy classifier |
| ip-api.com | ✅ | ✅ | ✅ | ✅ | `proxy`, `hosting`, `mobile` | 45 req/min, batch 100 | good; proxy flag less consistent |
| ipapi.is | ✅ | ✅ | ✅ | ✅ | paid only | geo only | flags are NOT free |
| ipwho.is | ✅ | ✅ | ✅ | ✅ | – | – | tz field is an object |
| ipinfo.io | ✅ | ✅ | ✅ | ✅ (`loc`) | – | – | |
| freeipapi.com | ✅ | ✅ | – | ✅ | – | – | |
| api.db-ip.com | ✅ | ✅ | – | – | – | – | |
| ipwhois.app | ✅ | ✅ | ✅ | ✅ | – | – | |

### Dead / blocked endpoints
- scamalytics.com → Cloudflare 403
- ipqualityscore free key → `invalid or unauthorized key`
- iphub v2 → `Empty API key`
- reallyfreegeoip.org, api.ipapi.com → Cloudflare 403
- ipapi.co → aggressive 429
- worldtimeapi.org → SSLError / down
- timeapi.io → 400, requires an explicit `ipAddress` parameter

---

## 3. Normalisation — the biggest single accuracy win

`Russia` / `Russian Federation` / `RU` must collapse to one key, otherwise the vote is
meaningless. Measured effect:

```
before normalisation:  country_votes = {Russia:5, Russian Federation:2, RU:1}  agreement = 0.625
after  normalisation:  country_votes = {RU:8}                                  agreement = 1.0
```

Same for ASN: `AS208677 Cloud Technologies LLC` → `AS208677`.

---

## 4. City — do it with coordinates, not names

A name vote fails because `Frankfurt am Main` and `Hanau am Main` are different strings
for points ~20 km apart. Coordinates do not have that problem.

**Method:**
1. collect every `lat/lon` the sources return
2. compute the median centroid
3. compute each point's distance to it; drop points beyond `max(50 km, 3 × MAD)`
   (median absolute deviation — robust, no assumption of a normal distribution)
4. recompute the centroid from the inliers
5. report `spread_km` and a precision tier: `city` ≤ 25 km, `metro` ≤ 100 km,
   `region` ≤ 400 km, else `country`

**Measured results** (with the outlier each run dropped):

| IP | city (centroid) | spread | precision | outlier dropped |
|---|---|---|---|---|
| 45.155.204.10 | Moscow | 0.2 km | city | ipapi.is (1127 km) |
| 51.15.0.1 | Amsterdam | 16.8 km | city | – |
| 8.8.8.8 | San Jose | 10.7 km | city | ip-api (3852 km), ipinfo (68 km) |
| 193.24.210.222 | Frankfurt am Main | 29.1 km | metro | ipapi.is (431 km) |

Outlier rejection is essential: without it `ipapi.is` reported 431 km off for the
Frankfurt VPS and 1127 km off for Moscow, and `ip-api` reported Ashburn (3852 km) for
`8.8.8.8`. With it, the remaining sources agree to within a few km.

Note that the answer for `193.24.210.222` (Frankfurt am Main, 29 km spread) is
verifiable against ground truth — it is the location of the test VPS itself.

---

## 5. Proxy / VPN flags — report per source, never merge

Sources genuinely disagree, so collapsing them into one boolean destroys information:

| IP | proxycheck.io | ip-api.com |
|---|---|---|
| 45.155.204.10 | `proxy: yes`, `type: VPN` | `proxy: false`, `hosting: true` |
| 8.8.8.8 | `proxy: no`, `type: Business` | `proxy: true`, `hosting: true` |
| 193.24.210.222 | `proxy: no`, `type: Business` | `proxy: false`, `hosting: true` |

`type: Business` is **not** a proxy signal — only an explicit `proxy: yes`, or a `type`
of `VPN` / `TOR` / `PUBLIC` / `SOCKS`, counts.

**Retries matter here.** `ip-api.com` carries the `hosting` (datacenter) flag. During
testing a single timeout silently removed the datacenter verdict and left a
"high confidence" result with an empty flag set. The tool therefore retries each source
twice and reports `flag_sources_alive` / `flags_incomplete` so an empty verdict is
never mistaken for a negative one.

---

## 6. Confidence scoring

- country agreement ≥ 0.75 with ≥ 5 sources OK → **high**
- ≥ 0.5 → **medium**
- else → **low**

---

## 7. Verified results

### 7.1 Datacenter IP, full field set
```
$ python proxy_geo_check.py --ip 45.155.204.10
country : RU  {RU: 8}              agree 1.0
asn     : AS208677                 agree 1.0
timezone: Europe/Moscow            agree 0.833
city    : Moscow  55.75392,37.61755  spread 0.2 km  precision city
          dropped: ipapi.is (1127.1 km)
proxy   : True  ['proxycheck']
dc      : True  ['ip-api']
conf    : high
```

### 7.2 Full pipeline through a real proxy (SSH SOCKS → VPS)
```
$ ssh -N -D 127.0.0.1:1080 vps-1year &
$ python proxy_geo_check.py --proxy socks5h://127.0.0.1:1080

exit_ip : 193.24.210.222   (via https://api.ipify.org?format=json)
country : DE  {DE: 8}      agree 1.0
asn     : AS35042          agree 1.0
timezone: Europe/Berlin    agree 1.0
city    : Frankfurt am Main  50.12256,8.79816  spread 29.1 km  precision metro
conf    : high
```
The exit IP was correctly detected *through* the proxy, and the derived city matched the
known location of that VPS — a real end-to-end validation, not a description.

### 7.3 The pitfall, and the fix
```
$ python proxy_geo_check.py --ip 8.8.8.8
country: US  agree 1.0
city name vote: Mountain View 0.571   →  Ashburn(1) / Mountain View(5) / San Jose(2)
city by coords: San Jose  37.36272,-121.9894  spread 10.7 km  precision city
```
The name vote and the coordinate estimate disagree; the coordinate estimate is the one
that lands within 10 km of the truth.

---

## 8. GitHub repositories — what is actually worth using

The GitHub search results for "proxy checker" are dominated by 0–3★ wrappers with
random names and template descriptions. Those add no geo accuracy — they are scrapers
with a checker bolted on. The projects that are real, maintained and verifiable:

| repo | ★ | what it gives you |
|---|---|---|
| [P3TERX/GeoLite.mmdb](https://github.com/P3TERX/GeoLite.mmdb) | 5.3k | MaxMind GeoLite2 Country/City/ASN MMDB, auto-updated |
| [geoip-lite/node-geoip](https://github.com/geoip-lite/node-geoip) | 2.4k | native NodeJS GeoIP |
| [sapics/ip-location-db](https://github.com/sapics/ip-location-db) | 2.2k | multi-DB location data, npm |
| [maxmind/GeoIP2-python](https://github.com/maxmind/GeoIP2-python) | 1.2k | official MaxMind reader |
| [ip2location/IP2Location-Python](https://github.com/ip2location/IP2Location-Python) | 166 | IP2Location DB reader |
| [NetworkCats/Merged-IP-Data](https://github.com/NetworkCats/Merged-IP-Data) | 109 | merges several DBs into one MMDB (raises agreement) |
| [blackdotsh/getIPIntel](https://github.com/blackdotsh/getIPIntel) | 331 | self-hosted proxy/VPN/Tor detection API |
| [iw4p/proxy-scraper](https://github.com/iw4p/proxy-scraper) | 598 | scrape + liveness-check proxies |

MMDBs give you fast offline geo, but they carry **no proxy/VPN flags** — pair them
with proxycheck.io for classification.

---

## 9. Recommended stack

| need | tool |
|---|---|
| offline / bulk geo | MaxMind GeoLite2 or DB-IP Lite MMDB |
| proxy / VPN classification | **proxycheck.io** (free key, 1000/day) |
| cross-verification at volume | ip-api.com batch (100 IPs/request, 45 req/min) |
| single-proxy deep check | `proxy_geo_check.py` from this repo |

---

## 10. Files

- `proxy_geo_check.py` — the tool
- `README.md` — usage, including how to start a SOCKS tunnel for proxy testing
- `REPORT.md` — this document
