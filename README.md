# proxy-geo-check

Accurate proxy / VPN location checker — **country, ASN, city and proxy flags**.

One geo-IP lookup is not accurate: it returns the **ASN registry location**, not the
real exit. For residential / rotating / mobile proxies that is frequently a
datacenter city or the wrong city entirely. This tool cross-validates 8 independent
sources and reports the parts that are actually reliable.

## What is reliable (measured, not assumed)

| Field | How | Reliability |
|---|---|---|
| country | majority vote over 8 sources, strings normalised | ✅ agreement 1.0 |
| ASN | majority vote, normalised | ✅ agreement 1.0 |
| timezone | majority vote | ✅ agreement 0.83–1.0 |
| **city** | **median centroid of returned coordinates + MAD outlier rejection** | ✅ ±0.2–29 km on the test IPs |
| proxy / VPN flag | reported per source, never merged | ⚠️ sources genuinely disagree |

City is *not* computed by voting on city-name strings — `Frankfurt am Main` vs
`Hanau am Main` vs `Gelnhausen` are different strings for points ~20 km apart, so a
name vote is meaningless. Instead every source's `lat/lon` is collected, outliers are
dropped with a median-absolute-deviation filter, and the remaining points are reduced
to a median centroid. The report carries `spread_km` and a `precision` tier
(`city` ≤25 km, `metro` ≤100 km, `region` ≤400 km, else `country`), so you know how
much to trust the point.

## Method

1. **ECHO** — route a request *through* the proxy to an echo service to learn the
   real exit IP. Detects chaining (exit IP ≠ configured IP).
2. **GEO** — look that IP up in 8 sources in parallel, **directly** and **with 2
   retries each** (these are IP-parameter lookups; routing them through the proxy only
   leaks the proxy's own geo). Retries matter: `ip-api.com` carries the `hosting`
   flag, and a single timeout silently removes the datacenter verdict.
3. **VOTE** — normalise country strings, then majority-vote on country + ASN.
4. **LOCATE** — median centroid of all coordinates, MAD outlier rejection, spread in km.
5. **FLAGS** — collect proxy / hosting / type flags per source and report them
   side by side instead of collapsing them into one boolean.
6. **SCORE** — confidence from cross-source agreement.

## Sources

| source | endpoint | proxy flags | coords | free tier |
|---|---|---|---|---|
| ip-api.com | `/json/{ip}?fields=...proxy,hosting,mobile,lat,lon` | proxy, hosting, mobile | ✅ | 45 req/min, batch 100 |
| **proxycheck.io** | `/v2/{ip}?vpn=1&asn=1` | **proxy, type (VPN/TOR)** | ✅ | 100/day, 1000/day with free key |
| ipwho.is | `/{ip}` | – | ✅ | – |
| ipinfo.io | `/{ip}/json` | – | ✅ (`loc` string) | – |
| freeipapi.com | `/api/json/{ip}` | – | ✅ | – |
| api.db-ip.com | `/v2/free/{ip}` | – | – | – |
| ipapi.is | `/?q={ip}` | proxy/vpn **paid only** | ✅ | – |
| ipwhois.app | `/json/{ip}` | – | ✅ | – |

Dead / blocked: scamalytics (CF 403), ipqualityscore free key, iphub (key required),
reallyfreegeoip + api.ipapi.com (CF 403), ipapi.co (429), worldtimeapi.org (SSL/down),
timeapi.io (needs explicit `ipAddress`).

## Usage

```bash
pip install requests pysocks

python proxy_geo_check.py --ip 45.155.204.10
python proxy_geo_check.py --proxy socks5h://127.0.0.1:1080
python proxy_geo_check.py --list proxies.txt --json out.json
```

### Testing through a real proxy

The proxy path needs a SOCKS listener. Start one, run the check, then stop it:

```bash
# 1. start a SOCKS5 tunnel (DNS resolves through the tunnel)
ssh -N -D 127.0.0.1:1080 you@your-server &

# 2. run the checker through it
python proxy_geo_check.py --proxy socks5h://127.0.0.1:1080

# 3. stop it again
pkill -f 'ssh -N -D 127.0.0.1:1080'
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
  "location": {
    "lat": 50.12256, "lon": 8.79816,
    "best_city": "Frankfurt am Main",
    "spread_km": 29.1,
    "precision": "metro",
    "coords_used": 6,
    "coords_dropped": {"ipapi.is": 431.3}
  },
  "verdict": {
    "city": "Frankfurt am Main", "city_precision": "metro", "city_spread_km": 29.1,
    "flagged_as_proxy_vpn": false,
    "flagged_as_datacenter": true,
    "datacenter_flagged_by": ["ip-api"],
    "flag_sources_alive": ["ip-api", "proxycheck"],
    "flags_incomplete": false,
    "confidence": "high"
  }
}
```

`flags_incomplete: true` means one of the two flag sources (ip-api, proxycheck) did not
answer — treat an empty proxy/datacenter verdict with suspicion in that case.

## Offline / bulk

For bulk or offline geo use a MaxMind GeoLite2 or DB-IP Lite MMDB
([P3TERX/GeoLite.mmdb](https://github.com/P3TERX/GeoLite.mmdb),
[geoip-lite/node-geoip](https://github.com/geoip-lite/node-geoip),
[maxmind/GeoIP2-python](https://github.com/maxmind/GeoIP2-python)) — but MMDBs carry
**no proxy flags**; pair them with proxycheck.io for classification.

## License

MIT
