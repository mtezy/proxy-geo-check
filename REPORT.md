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

| IP | provider answers (city) | agreement |
|---|---|---|
| 8.8.8.8 | Ashburn / Mountain View (×5) / San Jose | 0.625 |
| 193.24.210.222 | Frankfurt (×4) / Hanau (×2) / Gelnhausen / Sonnenberg | 0.5 |
| 51.15.0.1 | Haarlem (×4) / Amsterdam (×4) | 0.5 |

Country was unanimous (8/8) for all of them. **City is inherently unreliable.**

---

## 2. Sources tested head-to-head

| source | country | city | ASN | proxy flag | free tier | verdict |
|---|---|---|---|---|---|---|
| **proxycheck.io** | ✅ | ✅ | ✅ | **`proxy:yes`, `type:VPN/TOR`** | 100/day, 1000/day +key | best free proxy classifier |
| ip-api.com | ✅ | ✅ | ✅ | `proxy`, `hosting`, `mobile` | 45 req/min, batch 100 | good; proxy flag less consistent |
| ipapi.is | ✅ | ✅ | ✅ | paid only | geo only | flags are NOT free |
| ipwho.is | ✅ | ✅ | ✅ | – | – | tz field is an object |
| ipinfo.io | ✅ | ✅ | ✅ | – | – | |
| freeipapi.com | ✅ | ✅ | – | – | – | |
| api.db-ip.com | ✅ | ✅ | – | – | – | |
| ipwhois.app | ✅ | ✅ | ✅ | – | – | |

### Dead / blocked endpoints
- scamalytics.com → Cloudflare 403
- ipqualityscore free key → `invalid or unauthorized key`
- iphub v2 → `Empty API key`
- reallyfreegeoip.org, api.ipapi.com → Cloudflare 403
- ipapi.co → aggressive 429
- worldtimeapi.org → SSLError / Cloudflare, down
- timeapi.io → 400, requires explicit `ipAddress` param

---

## 3. The method that works

### 3.1 Learn the real exit IP
Route a request **through** the proxy to an echo service:
`https://api.ipify.org?format=json`, `https://ipinfo.io/json`, `https://api.myip.com`.

If the echoed IP ≠ the configured IP → the proxy is **chaining** (rotating / upstream hop).

### 3.2 Look up that IP in 8 sources, in parallel, directly
These are IP-parameter lookups. Routing them *through* the proxy would only make the
service report the proxy's own geo for its host-based fields, so query them direct.

### 3.3 Normalise before voting — this is the single biggest accuracy win
`Russia` / `Russian Federation` / `RU` must collapse to one key, otherwise the vote is
meaningless. Measured effect:

```
before normalisation:  country_votes = {Russia:5, Russian Federation:2, RU:1}  agreement = 0.625
after  normalisation:  country_votes = {RU:8}                                  agreement = 1.0
```

Same for ASN: `AS208677 Cloud Technologies LLC` → `AS208677`.

### 3.4 Report flags per source, never merge them
Sources disagree, so collapsing them into one boolean destroys information:

| IP | proxycheck.io | ip-api.com |
|---|---|---|
| 45.155.204.10 | `proxy: yes`, `type: VPN` | `proxy: false`, `hosting: true` |
| 8.8.8.8 | `proxy: no`, `type: Business` | `proxy: true`, `hosting: true` |
| 193.24.210.222 | `proxy: no`, `type: Business` | `proxy: false`, `hosting: true` |

Note `type: Business` is **not** a proxy signal — only an explicit `proxy: yes`
or a `type` of `VPN`/`TOR`/`PUBLIC`/`SOCKS` counts.

### 3.5 Score confidence from agreement
- country agreement ≥ 0.75 with ≥ 5 sources OK → **high**
- ≥ 0.5 → **medium**
- else → **low**

---

## 4. Verified results

### 4.1 Direct IP, datacenter
```
$ python proxy_geo_check.py --ip 45.155.204.10
country : RU  {RU: 8}                agree 1.0
asn     : AS208677                   agree 1.0
timezone: Europe/Moscow              agree 0.833
city    : Moscow (UNRELIABLE)        agree 0.875
proxy   : True  ['proxycheck']
dc      : True  ['ip-api']
conf    : high
```

### 4.2 Full pipeline through a real proxy (SSH SOCKS → VPS)
```
$ ssh -N -D 127.0.0.1:1080 vps-1year
$ python proxy_geo_check.py --proxy socks5h://127.0.0.1:1080

exit_ip : 193.24.210.222   (via https://api.ipify.org?format=json)
country : DE  {DE: 8}      agree 1.0
asn     : AS35042          agree 1.0
timezone: Europe/Berlin    agree 1.0
proxy   : False []
conf    : high
```
The exit IP was correctly detected *through* the proxy, and the lookup matched the
known location of that VPS — a real end-to-end validation, not a description.

### 4.3 The pitfall: perfect country, useless city
```
$ python proxy_geo_check.py --ip 8.8.8.8
country: US  agree 1.0
city   : Mountain View  agree 0.625   →  Ashburn(1) / Mountain View(5) / San Jose(2)
```

---

## 5. GitHub repositories — what is actually worth using

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

## 6. Recommended stack

| need | tool |
|---|---|
| offline / bulk geo | MaxMind GeoLite2 or DB-IP Lite MMDB |
| proxy / VPN classification | **proxycheck.io** (free key, 1000/day) |
| cross-verification at volume | ip-api.com batch (100 IPs/request, 45 req/min) |
| single-proxy deep check | `proxy_geo_check.py` from this repo |

---

## 7. Files

- `proxy_geo_check.py` — the tool
- `README.md` — usage
- `REPORT.md` — this document
