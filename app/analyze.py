"""Keyspace analyzer.

Measures what a running Redis actually holds, so the sizing calculator works
from data instead of guesses. Three sources, because each answers something
the others cannot:

  INFO memory   authoritative totals -- and `used_memory_overhead` is the only
                honest way to learn this instance's real per-key overhead
  a RANDOMKEY sample   distribution: key length, value size, TTL ratio, types
  --bigkeys     the outliers a sample will almost certainly miss, which are
                usually what causes latency spikes
"""
from __future__ import annotations

import re
from typing import Any

from . import ocp

# One round trip instead of N. RANDOMKEY is a random command, which older Redis
# refused inside scripts; Redis 7 replicates effects, so this is fine there, and
# the caller falls back to per-key calls if it is rejected.
SAMPLE_LUA = """
local n = tonumber(ARGV[1])
local out = {}
for i = 1, n do
  local k = redis.call('RANDOMKEY')
  if k then
    local t = redis.call('TYPE', k)['ok']
    local m = redis.call('MEMORY', 'USAGE', k)
    local ttl = redis.call('TTL', k)
    local vlen = 0
    if t == 'string' then vlen = redis.call('STRLEN', k)
    elseif t == 'list' then vlen = redis.call('LLEN', k)
    elseif t == 'hash' then vlen = redis.call('HLEN', k)
    elseif t == 'set' then vlen = redis.call('SCARD', k)
    elseif t == 'zset' then vlen = redis.call('ZCARD', k)
    end
    out[#out+1] = string.format('%s|%d|%d|%d|%d', t, m or 0, ttl, #k, vlen)
  end
end
return out
"""


def _cli(kubeconfig: str, ns: str, pod: str, pw: str, *args: str,
         timeout: int = 120) -> str:
    auth = ["-a", pw, "--no-auth-warning"] if pw else []
    p = ocp.run(kubeconfig, ["exec", "-n", ns, pod, "--", "redis-cli", *auth, *args],
                check=False, timeout=timeout)
    return ((p.stdout or "") + (p.stderr or "")).strip()


def _info(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def analyze(kubeconfig: str, ns: str, pod: str, password: str,
            samples: int = 300, log=None) -> dict[str, Any]:
    def note(m):
        if log:
            log(m)

    res: dict[str, Any] = {"pod": pod, "namespace": ns, "samples_requested": samples}

    note("  reading INFO memory / keyspace / stats")
    mem = _info(_cli(kubeconfig, ns, pod, password, "INFO", "memory"))
    ks = _cli(kubeconfig, ns, pod, password, "INFO", "keyspace")
    stats = _info(_cli(kubeconfig, ns, pod, password, "INFO", "stats"))

    m = re.search(r"db0:keys=(\d+),expires=(\d+)", ks)
    keys = int(m.group(1)) if m else 0
    expires = int(m.group(2)) if m else 0

    used = int(mem.get("used_memory", 0) or 0)
    dataset = int(mem.get("used_memory_dataset", 0) or 0)
    overhead = int(mem.get("used_memory_overhead", 0) or 0)
    res.update({
        "keys": keys,
        "keys_with_ttl": expires,
        "ttl_ratio": round(expires / keys, 3) if keys else 0,
        "used_memory": used,
        "used_memory_human": mem.get("used_memory_human"),
        "used_memory_dataset": dataset,
        "used_memory_overhead": overhead,
        "maxmemory": int(mem.get("maxmemory", 0) or 0),
        "maxmemory_human": mem.get("maxmemory_human"),
        "maxmemory_policy": mem.get("maxmemory_policy"),
        "fragmentation_ratio": float(mem.get("mem_fragmentation_ratio", 0) or 0),
        "rss": int(mem.get("used_memory_rss", 0) or 0),
        "evicted_keys": int(stats.get("evicted_keys", 0) or 0),
        "keyspace_hits": int(stats.get("keyspace_hits", 0) or 0),
        "keyspace_misses": int(stats.get("keyspace_misses", 0) or 0),
    })
    if keys:
        # the measured figure, not a textbook one
        res["measured_bytes_per_key"] = round(used / keys, 1)
        res["measured_dataset_per_key"] = round(dataset / keys, 1) if dataset else None

    if not keys:
        res["empty"] = True
        return res

    note(f"  sampling {samples} random keys")
    raw = _cli(kubeconfig, ns, pod, password, "EVAL", SAMPLE_LUA, "0", str(samples),
               timeout=180)
    rows = []
    for line in raw.splitlines():
        parts = line.strip().split("|")
        if len(parts) == 5:
            try:
                rows.append((parts[0], int(parts[1]), int(parts[2]),
                             int(parts[3]), int(parts[4])))
            except ValueError:
                pass

    if not rows:
        res["sample_error"] = raw[:200] or "sampling returned nothing"
    else:
        types: dict[str, int] = {}
        klens, mems, vlens, ttls = [], [], [], []
        for t, mbytes, ttl, klen, vlen in rows:
            types[t] = types.get(t, 0) + 1
            klens.append(klen)
            mems.append(mbytes)
            vlens.append(vlen)
            ttls.append(ttl)
        n = len(rows)
        dominant = max(types, key=types.get)
        avg_mem = sum(mems) / n
        avg_klen = sum(klens) / n
        avg_vlen = sum(vlens) / n
        res["sample"] = {
            "n": n,
            "types": {k: round(v / n * 100) for k, v in sorted(
                types.items(), key=lambda x: -x[1])},
            "dominant_type": dominant,
            "avg_key_length": round(avg_klen, 1),
            "avg_memory_per_key": round(avg_mem, 1),
            "avg_value_size": round(avg_vlen, 1),
            "with_ttl_pct": round(sum(1 for t in ttls if t >= 0) / n * 100),
            "p50_memory": sorted(mems)[n // 2],
            "p95_memory": sorted(mems)[int(n * 0.95)] if n > 20 else max(mems),
            "max_memory_in_sample": max(mems),
        }
        # for strings avg_value_size is bytes; for containers it is element count
        res["sizing_suggestion"] = {
            "keys": keys,
            "keyLen": int(round(avg_klen)),
            "valLen": int(round(avg_vlen)) if dominant == "string"
                      else max(1, int(round(avg_mem - avg_klen - 64))),
            "type": dominant,
            "ttl": res["ttl_ratio"] > 0.5,
        }

    note("  scanning for outliers (--bigkeys)")
    auth = ["-a", password, "--no-auth-warning"] if password else []
    p = ocp.run(kubeconfig, ["exec", "-n", ns, pod, "--", "redis-cli", *auth,
                             "--bigkeys"], check=False, timeout=300)
    big = (p.stdout or "")
    res["bigkeys"] = [l.strip() for l in big.splitlines()
                      if "Biggest" in l or "biggest" in l]
    res["bigkeys_raw"] = big[-2500:]

    note("  reading SLOWLOG")
    slow = _cli(kubeconfig, ns, pod, password, "SLOWLOG", "GET", "10")
    res["slowlog_raw"] = slow[:2000]
    res["slowlog_count"] = _cli(kubeconfig, ns, pod, password, "SLOWLOG", "LEN")

    note("  reading command stats")
    cmds = _info(_cli(kubeconfig, ns, pod, password, "INFO", "commandstats"))
    top = []
    for k, v in cmds.items():
        calls = re.search(r"calls=(\d+)", v)
        usec = re.search(r"usec_per_call=([\d.]+)", v)
        if calls:
            top.append({"command": k.replace("cmdstat_", ""),
                        "calls": int(calls.group(1)),
                        "usec_per_call": float(usec.group(1)) if usec else 0})
    res["top_commands"] = sorted(top, key=lambda c: -c["calls"])[:10]
    res["slowest_commands"] = sorted(
        [c for c in top if c["calls"] > 10], key=lambda c: -c["usec_per_call"])[:5]

    res["findings"] = _findings(res)
    return res


def _findings(r: dict) -> list[dict]:
    """Turn the numbers into the handful of statements that change a decision."""
    out: list[dict] = []
    keys = r.get("keys", 0)
    sm = r.get("sample") or {}
    mean = r.get("measured_bytes_per_key")
    p50 = sm.get("p50_memory")

    # A few enormous keys distort the mean, and the mean is what people size with.
    if mean and p50:
        skew = mean / p50
        r["typical_bytes_per_key"] = p50
        r["skew"] = round(skew, 1)
        if skew >= 2:
            out.append({
                "level": "warn",
                "title": f"Key sizes are heavily skewed ({skew:.1f}x)",
                "detail": f"The mean is {mean:.0f} bytes per key but the median is only "
                          f"{p50} bytes. A small number of very large keys dominates the "
                          "memory. Size capacity from the mean, but hunt the outliers "
                          "below -- big keys block the event loop on every access.",
            })

    for line in r.get("bigkeys", []):
        m = re.search(r'Biggest\s+(\w+)\s+found\s+"([^"]+)"\s+has\s+(\d+)\s+(bytes|fields|items|members)', line)
        if m and m.group(4) == "bytes" and int(m.group(3)) > 1_000_000:
            out.append({
                "level": "warn",
                "title": f"Large key: {m.group(2)} ({int(m.group(3)):,} bytes)",
                "detail": "Redis is single-threaded. Reading or deleting a multi-megabyte "
                          "key stalls every other client for the duration. Split it, or "
                          "move it out of Redis.",
            })

    mm = r.get("maxmemory", 0)
    used = r.get("used_memory", 0)
    if mm and used:
        pct = used / mm * 100
        if pct > 80:
            out.append({"level": "err",
                        "title": f"Memory is {pct:.0f}% of maxmemory",
                        "detail": "At 100% the eviction policy takes over. Raise maxmemory "
                                  "(and the container limit with it) or reduce the working set."})
        elif pct > 60:
            out.append({"level": "warn",
                        "title": f"Memory is {pct:.0f}% of maxmemory",
                        "detail": "Plan the next size now rather than during an incident."})

    if r.get("evicted_keys", 0) > 0:
        out.append({"level": "warn",
                    "title": f"{r['evicted_keys']:,} keys have been evicted",
                    "detail": "The working set does not fit in maxmemory. That is correct "
                              "behaviour for a cache, and data loss for a datastore."})

    hits, misses = r.get("keyspace_hits", 0), r.get("keyspace_misses", 0)
    if hits + misses > 1000:
        ratio = hits / (hits + misses) * 100
        r["hit_ratio"] = round(ratio, 1)
        if ratio < 80:
            out.append({"level": "warn",
                        "title": f"Hit ratio is {ratio:.0f}%",
                        "detail": "Most reads are missing. Either the cache is too small, "
                                  "TTLs are too short, or the access pattern is not cacheable."})

    frag = r.get("fragmentation_ratio", 0)
    if frag > 1.5 and used > 100 * 1024 * 1024:
        out.append({"level": "warn",
                    "title": f"Fragmentation ratio {frag}",
                    "detail": "The OS has given Redis noticeably more memory than it is "
                              "using. Consider activedefrag, or a restart during a window."})

    ttl = r.get("ttl_ratio", 0)
    if keys and ttl < 0.5 and (r.get("maxmemory_policy") or "").startswith("volatile"):
        out.append({"level": "err",
                    "title": f"Only {ttl*100:.0f}% of keys have a TTL, but the policy is "
                             f"{r.get('maxmemory_policy')}",
                    "detail": "volatile-* policies can only evict keys that have an expiry. "
                              "When full, Redis will start REJECTING WRITES instead."})

    if not out:
        out.append({"level": "ok", "title": "Nothing concerning found",
                    "detail": "Memory, hit ratio, fragmentation and key distribution all "
                              "look reasonable."})
    return out
