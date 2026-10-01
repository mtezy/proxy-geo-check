# proxy-geo-check

Accurate proxy / VPN location checker.

One geo-IP lookup is not accurate: it returns the **ASN registry location**, not the
real exit. For residential / rotating / mobile proxies that is frequently a
datacenter city or the wrong city entirely. This tool cross-validates 8 independent
sources and reports the parts that are actually reliable.

## What is reliable (measured, not assumed)

| Field | Reliable? | Notes |
|---|---|---|
| country | ✅ | agreement 1.0 across 8 sources once strings are normalised |
| ASN | ✅ | agreement 1.0 |
| timezone | ✅ | agreement 0.83–1.0 |
| proxy / VPN flag | ⚠️ per-source | sources disagree; both flags are reported, never merged |
| city | ❌ | agreement 0.12–0.88; reported for reference, never used in the verdict |

Measured evidence: `8.8.8.8` is reported as Ashburn, Mountain View **and** San Jose by
three different providers. `45.155.204.10` is `proxy=yes` on proxycheck.io but
`proxy=False` on ip-api.com.

## Method

1. **ECHO** — route a request *through* the proxy to an echo service to learn the
   real exit IP. Detects chaining (exit IP ≠ configured IP).
2. **GEO** — look that IP up in 8 sources in parallel, **directly** (these are
   IP-parameter lookups; routing them through the proxy only leaks the proxy's own geo).
3. **VOTE** — normalise country strings, then majority-vote on country + ASN.
4. **FLAGS** — collect proxy / hosting / type flags per source and report them
   side by side instead of collapsing them into one boolean.
5. **SCORE** — confidence from cross-source agreement.

## Sources

| source | endpoint | proxy flags | free tier |
|---|---|---|---|
| ip-api.com | `/json/{ip}?fields=...proxy,hosting,mobile` | proxy, hosting, mobile | 45 req/min, batch 100 |
| **proxycheck.io** | `/v2/{ip}?vpn=1&asn=1` | **proxy, type (VPN/TOR)** | 100/day, 1000/day with free key |
| ipwho.is | `/{ip}` | – | – |
| ipinfo.io | `/{ip}/json` | – | – |
| freeipapi.com | `/api/json/{ip}` | – | – |
| api.db-ip.com | `/v2/free/{ip}` | – | – |
| ipapi.is | `/?q={ip}` | proxy/vpn **paid only** | – |
| ipwhois.app | `/json/{ip}` | – | – |

Dead / blocked: scamalytics (CF 403), ipqualityscore free key, iphub (key required),
reallyfreegeoip + api.ipapi.com (CF 403), ipapi.co (429).

## Usage

```bash
pip install requests pysocks

python proxy_geo_check.py --ip 45.155.204.10
python proxy_geo_check.py --proxy socks5h://127.0.0.1:1080
python proxy_geo_check.py --list proxies.txt --json out.json
```

Use `socks5h://`, not `socks5://` — with `socks5://` DNS resolves locally and can leak
your own geo. The tool rewrites `socks5://` to `socks5h://` automatically.

## Output

```json
{
  "exit_ip": "193.24.210.222",
  "country": "DE",   "country_agreement": 1.0,
  "asn": "AS35042",  "asn_agreement": 1.0,
  "timezone": "Europe/Berlin",
  "city": "Frankfurt am Main",
  "verdict": {
    "city_unreliable": true,
    "flagged_as_proxy_vpn": false,
    "flagged_as_datacenter": true,
    "datacenter_flagged_by": ["ip-api"],
    "confidence": "high"
  }
}
```

## Offline / bulk

For bulk or offline geo use a MaxMind GeoLite2 or DB-IP Lite MMDB
([P3TERX/GeoLite.mmdb](https://github.com/P3TERX/GeoLite.mmdb),
[geoip-lite/node-geoip](https://github.com/geoip-lite/node-geoip),
[maxmind/GeoIP2-python](https://github.com/maxmind/GeoIP2-python)) — but MMDBs carry
**no proxy flags**; pair them with proxycheck.io for classification.

## License

MIT
