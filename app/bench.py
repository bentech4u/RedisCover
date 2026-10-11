"""Benchmarking, written to answer where the time and the data actually go.

redis-benchmark alone reports one number for the whole release, which is the
least useful shape for a clustered or replicated deployment: a slow node, an
unbalanced shard or a replica that is not being read from all average away
into a single ops/sec figure that looks fine.

So this runs the aggregate benchmark AND measures each pod on its own, then
puts the memory and key distribution beside the throughput. An imbalance is a
fact about your keyspace, not something you can tune away with more clients.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from . import ocp, redistests

# redis-benchmark names its keys key:__rand_int__, so with -r it writes
# key:000000000001 .. key:<keyspace>. Cleanup targets exactly that prefix and
# nothing else, and still shows the count before deleting.
BENCH_KEY_GLOB = "key:*"


def _csv_rows(out: str) -> list[dict[str, Any]]:
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith('"') or line.startswith('"test"'):
            continue
        f = [c.strip('"') for c in line.split('","')]
        f[-1] = f[-1].rstrip('"')
        if len(f) < 8:
            continue
        try:
            rows.append({"test": f[0], "rps": float(f[1]), "avg": float(f[2]),
                         "min": float(f[3]), "p50": float(f[4]), "p95": float(f[5]),
                         "p99": float(f[6]), "max": float(f[7])})
        except ValueError:
            continue
    return rows


def _info(kubeconfig: str, ns: str, pod: str, auth: list[str],
          section: str = "") -> dict[str, str]:
    args = ["exec", "-n", ns, pod, "--", "redis-cli", *auth, "INFO"]
    if section:
        args.append(section)
    p = ocp.run(kubeconfig, args, check=False, timeout=30)
    d = {}
    for line in (p.stdout or "").splitlines():
        if ":" in line and not line.startswith("#"):
            k, _, v = line.partition(":")
            d[k.strip()] = v.strip()
    return d


def _dbsize(kubeconfig: str, ns: str, pod: str, auth: list[str]) -> int:
    p = ocp.run(kubeconfig, ["exec", "-n", ns, pod, "--", "redis-cli", *auth, "DBSIZE"],
                check=False, timeout=30)
    m = re.search(r"\d+", p.stdout or "")
    return int(m.group()) if m else 0


def _slots(kubeconfig: str, ns: str, pod: str, auth: list[str]) -> dict[str, str]:
    """pod name -> slot range, for a cluster. Empty for anything else."""
    p = ocp.run(kubeconfig, ["exec", "-n", ns, pod, "--", "redis-cli", *auth,
                             "CLUSTER", "NODES"], check=False, timeout=30)
    out = {}
    for line in (p.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 9:
            continue
        addr = parts[1]
        name = addr.split(",")[-1] if "," in addr else addr
        flags = parts[2]
        rng = " ".join(parts[8:]) if "master" in flags else ""
        out[name] = rng
    return out


def endpoints(kubeconfig: str, ns: str, name: str) -> dict[str, Optional[str]]:
    """The write Service (pinned to one pod) and the read Service (all pods).

    Benchmarking only the write endpoint measures one pod no matter how many
    replicas you run, which is why read scaling never shows up in a single
    number. Both are resolved so the two can be compared directly.
    """
    raw = ocp.run(kubeconfig, ["get", "svc", "-n", ns, "-l", f"app={name}", "-o",
                               "custom-columns=N:.metadata.name,IP:.spec.clusterIP,"
                               "SEL:.spec.selector", "--no-headers"],
                  check=False, timeout=60).stdout or ""
    write = read = None
    for line in raw.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        nm, ip, sel = parts
        if "statefulset.kubernetes.io/pod-name" in sel:
            write = nm
        elif ip in ("None", "<none>"):
            continue                      # headless
        else:
            read = nm
    if write is None:
        write = read                      # standalone: one Service does both
    return {"write": write, "read": read}


def _container_limit(kubeconfig: str, ns: str, pod: str) -> str:
    return ocp.jsonpath(
        kubeconfig, ["get", "pod", pod, "-n", ns],
        "{.spec.containers[0].resources.limits.memory}") or ""


def _to_bytes(v: str) -> int:
    """Kubernetes quantity -> bytes. Gi/Mi are binary, G/M decimal."""
    m = re.match(r"^\s*([\d.]+)\s*([KMGTP]i?)?\s*$", v or "")
    if not m:
        return 0
    n = float(m.group(1))
    u = (m.group(2) or "").strip()
    mult = {"": 1, "K": 10**3, "Ki": 1024, "M": 10**6, "Mi": 1024**2,
            "G": 10**9, "Gi": 1024**3, "T": 10**12, "Ti": 1024**4,
            "P": 10**15, "Pi": 1024**5}.get(u, 1)
    return int(n * mult)


def _human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TiB"


def _snapshot(job, kubeconfig: str, ns: str, pods: list[str], auth: list[str],
              label: str) -> dict[str, dict]:
    job.log(f"  {label}:")
    snap = {}
    for pod in pods:
        mem = _info(kubeconfig, ns, pod, auth, "memory")
        rep = _info(kubeconfig, ns, pod, auth, "replication")
        keys = _dbsize(kubeconfig, ns, pod, auth)
        role = rep.get("role", "?")
        role = {"master": "primary", "slave": "replica"}.get(role, role)
        used = int(mem.get("used_memory", 0) or 0)
        rss = int(mem.get("used_memory_rss", 0) or 0)
        maxm = int(mem.get("maxmemory", 0) or 0)
        limit = _to_bytes(_container_limit(kubeconfig, ns, pod))
        snap[pod] = {"used": mem.get("used_memory_human", "?"),
                     "used_bytes": used, "rss_bytes": rss,
                     "rss": mem.get("used_memory_rss_human", "?"),
                     "frag": mem.get("mem_fragmentation_ratio", "?"),
                     "maxmemory": mem.get("maxmemory_human", "?"),
                     "maxmemory_bytes": maxm, "limit_bytes": limit,
                     # cache fullness, and how close RSS is to being OOMKilled
                     "pct_maxmemory": (100.0 * used / maxm) if maxm else 0.0,
                     "pct_limit": (100.0 * rss / limit) if limit else 0.0,
                     "keys": keys, "role": role}
        pm = f"{snap[pod]['pct_maxmemory']:.1f}% of maxmemory" if maxm else "no maxmemory"
        pl = f"{snap[pod]['pct_limit']:.1f}% of limit" if limit else "no limit"
        job.log(f"    {pod:<26} {role:<8} keys={keys:<8} "
                f"used={snap[pod]['used']:<9} {pm}, rss {pl}")
    return snap


def _bench(job, kubeconfig: str, ns: str, from_pod: str, host: str, port: int,
           auth: list[str], tests: str, spec, cluster: bool,
           label: str) -> list[dict]:
    """One redis-benchmark run, reported as CSV so the percentiles are exact.

    `auth` here must NOT carry --no-auth-warning: that is a redis-cli flag, and
    redis-benchmark rejects unknown options outright with exit 1 and a usage
    dump, so every run returns nothing.
    """
    cmd = ["exec", "-n", ns, from_pod, "--", "redis-benchmark",
           "-h", host, "-p", str(port), *auth,
           "-t", tests, "-n", str(spec.requests), "-c", str(spec.clients),
           "-d", str(spec.datasize), "-r", str(spec.keyspace),
           "-P", str(spec.pipeline), "--csv"]
    if cluster:
        cmd.append("--cluster")
    job.log(f"  {label}: -t {tests} -n {spec.requests} -c {spec.clients} "
            f"-d {spec.datasize}B -P {spec.pipeline}"
            + (" --cluster" if cluster else ""))
    p = ocp.run(kubeconfig, cmd, check=False, timeout=900)
    rows = _csv_rows((p.stdout or "") + (p.stderr or ""))
    if not rows:
        err = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
        job.log("    FAILED -- no parseable output:")
        for line in err[:4]:
            job.log("      " + line)
    for r in rows:
        job.log(f"    {r['test']:<8} {r['rps']:>12,.0f} ops/s   "
                f"p50 {r['p50']:>6.3f}ms  p95 {r['p95']:>6.3f}ms  "
                f"p99 {r['p99']:>6.3f}ms  max {r['max']:>7.3f}ms")
    return rows


def run(job, kubeconfig: str, spec) -> None:
    ns, name = spec.namespace, spec.name
    total = 6 if spec.cleanup else 5

    job.step(1, total, "Resolving the release")
    t = redistests.resolve_target(kubeconfig, spec.kind, ns, name,
                                  log=job.log, topology=spec.topology or "")
    # two shapes: redis-cli takes --no-auth-warning, redis-benchmark rejects it
    auth = ["-a", t.password, "--no-auth-warning"] if t.password else []
    bench_auth = ["-a", t.password] if t.password else []
    pods = list(t.pods or [])
    if not pods:
        raise RuntimeError(f"no pods found for {ns}/{name}")
    cluster = t.topology == "cluster"
    job.log(f"  topology {t.topology} | {len(pods)} pod(s) | endpoint {t.host}:{t.port}")

    if cluster:
        sl = _slots(kubeconfig, ns, pods[0], auth)
        job.log("  shard map -- which pod owns which hash slots:")
        for pod, rng in sorted(sl.items()):
            job.log(f"    {pod:<28} {rng or '(replica)'}")

    job.step(2, total, "Before: memory and key distribution")
    before = _snapshot(job, kubeconfig, ns, pods, auth, "per pod")

    job.step(3, total, "Throughput through the Service endpoint(s)")
    if spec.include_writes:
        job.log("  WRITES ENABLED -- this inserts keys into the live keyspace.")
        job.log(f"  On an eviction policy like allkeys-lru, inserting up to "
                f"{spec.keyspace:,} keys can evict real application data.")
        tests = "set,get"
    else:
        job.log("  read-only run (GET). Enable writes for SET figures.")
        tests = "get"

    eps = endpoints(kubeconfig, ns, name)
    domain = ocp.cluster_domain(kubeconfig, ns)
    wanted = ["write", "read"] if spec.endpoint == "both" else [spec.endpoint]
    agg_by_ep: dict[str, list[dict]] = {}
    for which in wanted:
        svc = eps.get(which)
        if not svc:
            job.log(f"  no {which} Service for this release; skipping")
            continue
        if which == "read" and not cluster and t.topology == "standalone":
            job.log("  standalone has no separate read endpoint; skipping")
            continue
        host = f"{svc}.{ns}.svc.{domain}"
        # a write against the read Service lands on a replica and fails READONLY
        ep_tests = "get" if (which == "read" and spec.include_writes) else tests
        if ep_tests != tests:
            job.log("  read endpoint load-balances onto replicas, which refuse "
                    "writes -- running GET only against it")
        rows = _bench(job, kubeconfig, ns, pods[0], host, t.port, bench_auth,
                      ep_tests, spec, cluster, f"{which} endpoint ({svc})")
        if rows:
            agg_by_ep[which] = rows
    agg = agg_by_ep.get("write") or agg_by_ep.get("read") or []

    per_pod: dict[str, list[dict]] = {}
    if spec.per_pod:
        job.step(4, total, "Per-pod throughput -- finds the slow node an average hides")
        for pod in pods:
            ip = ocp.jsonpath(kubeconfig, ["get", "pod", pod, "-n", ns],
                              "{.status.podIP}")
            if not ip:
                continue
            # a replica refuses SET, so only ever read directly from a pod
            per_pod[pod] = _bench(job, kubeconfig, ns, pod, ip, t.port, bench_auth,
                                  "get", spec, False, pod)
    else:
        job.step(4, total, "Per-pod throughput (skipped)")

    job.step(5, total, "After: what the run changed")
    after = _snapshot(job, kubeconfig, ns, pods, auth, "per pod")

    if spec.cleanup:
        job.step(6, total, "Removing benchmark keys")
        _cleanup(job, kubeconfig, ns, pods, auth, before, after)

    slots = _slots(kubeconfig, ns, pods[0], auth) if cluster else {}
    job.result["report"] = _report(ns, name, t, spec, before, after, agg,
                                   per_pod, cluster, slots, agg_by_ep, eps)
    job.result["report_html"] = _report_html(ns, name, t, spec, before, after,
                                             per_pod, cluster, slots, agg_by_ep, eps)
    job.result["aggregate"] = agg
    job.log("")
    job.log("Report ready -- open the Benchmark tab to read or download it.")


def _cleanup(job, kubeconfig, ns, pods, auth, before, after) -> None:
    primaries = [p for p in pods if after.get(p, {}).get("role") == "primary"]
    if not primaries:
        job.log("  no primary found; nothing deleted")
        return
    for pod in primaries:
        p = ocp.run(kubeconfig,
                    ["exec", "-n", ns, pod, "--", "redis-cli", *auth, "--scan",
                     "--pattern", BENCH_KEY_GLOB], check=False, timeout=300)
        keys = [k for k in (p.stdout or "").split() if k]
        job.log(f"  {pod}: {len(keys)} key(s) match {BENCH_KEY_GLOB}")
        if not keys:
            continue
        # delete in batches; one DEL per key would be thousands of round trips
        for i in range(0, len(keys), 500):
            ocp.run(kubeconfig, ["exec", "-n", ns, pod, "--", "redis-cli", *auth,
                                 "DEL", *keys[i:i + 500]], check=False, timeout=120)
        job.log(f"  {pod}: deleted {len(keys)}")


def _report(ns, name, t, spec, before, after, agg, per_pod, cluster, slots,
            agg_by_ep=None, eps=None) -> str:
    d: list[str] = []
    a = d.append
    a(f"# Redis benchmark: {ns}/{name}")
    a("")
    a(f"Topology **{t.topology}**, {len(before)} pod(s), endpoint "
      f"`{t.host}:{t.port}`.")
    a(f"Load: {spec.requests:,} requests, {spec.clients} clients, "
      f"{spec.datasize} byte values, pipeline {spec.pipeline}, "
      f"keyspace {spec.keyspace:,}.")
    a("")
    a("## Throughput through the Service endpoint(s)")
    a("")
    agg_by_ep = agg_by_ep or {}
    eps = eps or {}
    if agg_by_ep:
        a("| Endpoint | Service | Operation | ops/sec | avg | p50 | p95 | p99 | max |")
        a("|---|---|---|---:|---:|---:|---:|---:|---:|")
        for which, rows in agg_by_ep.items():
            for r in rows:
                a(f"| {which} | `{eps.get(which, '?')}` | {r['test']} | {r['rps']:,.0f} "
                  f"| {r['avg']:.3f} ms | {r['p50']:.3f} ms | {r['p95']:.3f} ms "
                  f"| {r['p99']:.3f} ms | {r['p99']:.3f} ms |")
        a("")
        w = next((r["rps"] for r in agg_by_ep.get("write", []) if r["test"] == "GET"), 0)
        rr = next((r["rps"] for r in agg_by_ep.get("read", []) if r["test"] == "GET"), 0)
        if w and rr:
            a(f"**Read endpoint against write endpoint: {rr:,.0f} vs {w:,.0f} ops/sec "
              f"({rr / w:.2f}x).** The write endpoint is one pod; the read endpoint "
              f"spreads across all of them. If that ratio is near 1.0 the client is "
              f"not actually getting read scaling -- usually because connections are "
              f"long-lived and kube-proxy balances per connection, not per request.")
            a("")
    elif agg:
        a("| Operation | ops/sec | avg | p50 | p95 | p99 | max |")
        a("|---|---:|---:|---:|---:|---:|---:|")
        for r in agg:
            a(f"| {r['test']} | {r['rps']:,.0f} | {r['avg']:.3f} ms | {r['p50']:.3f} ms "
              f"| {r['p95']:.3f} ms | {r['p99']:.3f} ms | {r['max']:.3f} ms |")
        a("")
        a("Read p99 against p50 is the number to watch: a p99 several times the "
          "p50 means a few requests are waiting on something -- a fork during "
          "an AOF rewrite, an O(N) command from another client, or CPU "
          "contention on the node -- and averages will never show it.")
    else:
        a("_No benchmark output was produced._")
    a("")
    if per_pod:
        a("## Per pod, measured directly")
        a("")
        a("Each pod benchmarked on its own IP, bypassing the Service.")
        a("")
        if cluster:
            a("The aggregate above spreads across the primaries by hash slot, so it "
              "blends them: a single slow shard is averaged away there and visible "
              "only here.")
        else:
            a("This topology's write endpoint resolves to **one pod** -- the primary "
              "-- so the aggregate above is not an average of these. It is the same "
              "pod measured through the Service, which makes the two directly "
              "comparable and the difference between them the cost of the hop.")
        a("")
        a("| Pod | Role | ops/sec | p50 | p99 |")
        a("|---|---|---:|---:|---:|")
        rates = {}
        for pod, rows in per_pod.items():
            if not rows:
                continue
            r = rows[0]
            rates[pod] = r["rps"]
            a(f"| `{pod}` | {after.get(pod, {}).get('role', '?')} | {r['rps']:,.0f} "
              f"| {r['p50']:.3f} ms | {r['p99']:.3f} ms |")
        a("")
        # the Service hop is measurable: same pod, with and without kube-proxy
        if agg and not cluster:
            direct = next((v for k, v in rates.items()
                           if after.get(k, {}).get("role") == "primary"), 0)
            via = agg[0]["rps"]
            if direct and via and direct > via:
                a(f"**The Service costs {100 * (1 - via / direct):.0f}% of throughput** "
                  f"on the primary: {direct:,.0f} ops/s directly against "
                  f"{via:,.0f} through `{t.host}`. That gap is kube-proxy and the "
                  f"extra network hop, not Redis. It is the price of a stable "
                  f"endpoint and a client that does not track pod IPs -- worth "
                  f"knowing, not worth removing.")
                a("")
        if len(rates) > 1:
            hi, lo = max(rates.values()), min(rates.values())
            if lo > 0 and hi / lo >= 1.3:
                slowest = min(rates, key=rates.get)
                a(f"**Spread of {hi / lo:.1f}x between fastest and slowest pod.** "
                  f"`{slowest}` is the laggard. Pods are usually near-identical, so "
                  f"check what else runs on its node and whether it has a CPU limit "
                  f"the others do not.")
            else:
                a(f"Pods are within {hi / max(lo, 1):.2f}x of each other, which is "
                  "the healthy case.")
    a("")
    a("## Memory and key distribution")
    a("")
    a("| Pod | Role | Keys | Used | % of maxmemory | RSS | % of container limit | Frag |")
    a("|---|---|---:|---:|---:|---:|---:|---:|")
    for pod in sorted(before):
        b, af = before[pod], after.get(pod, {})
        a(f"| `{pod}` | {af.get('role', b['role'])} | {af.get('keys', 0):,} | "
          f"{af.get('used', '?')} | {af.get('pct_maxmemory', 0):.1f}% | "
          f"{af.get('rss', '?')} | {af.get('pct_limit', 0):.1f}% | "
          f"{af.get('frag', '?')} |")
    a("")
    hot = [p for p in after if after[p].get("pct_limit", 0) >= 80]
    if hot:
        a(f"**{len(hot)} pod(s) above 80% of the container memory limit.** RSS, not "
          "`used_memory`, is what the kubelet compares against the limit, and "
          "crossing it is an OOMKill with no warning: " + ", ".join(f"`{p}`" for p in hot))
        a("")
    full = [p for p in after if after[p].get("pct_maxmemory", 0) >= 80]
    if full:
        a(f"**{len(full)} pod(s) above 80% of `maxmemory`.** At 100% the eviction "
          "policy decides what happens -- keys silently disappear, or writes start "
          "failing with OOM: " + ", ".join(f"`{p}`" for p in full))
        a("")
    if cluster:
        a("### Shard ownership")
        a("")
        a("| Pod | Hash slots |")
        a("|---|---|")
        for pod, rng in sorted(slots.items()):
            a(f"| `{pod}` | {rng or '_replica — serves no slots_'} |")
        a("")
        masters = {p: after[p]["keys"] for p in after
                   if after[p]["role"] == "primary"}
        if len(masters) > 1 and max(masters.values()) > 0:
            hi, lo = max(masters.values()), max(min(masters.values()), 1)
            a(f"Key counts across primaries: {', '.join(f'{k.split(chr(45))[-1]}={v:,}' for k, v in sorted(masters.items()))}.")
            if hi / lo >= 1.5:
                a("")
                a(f"**Skew of {hi / lo:.1f}x.** Redis distributes by CRC16 of the key, "
                  "so an even spread needs varied key names. Heavy use of hash tags "
                  "`{...}` forces keys into one slot and therefore one shard, which "
                  "is how one pod ends up holding most of the data and taking most "
                  "of the traffic.")
    else:
        a("Every replica holds a **full copy** of the primary's data, so key counts "
          "matching across pods is correct and expected -- this topology does not "
          "shard. Adding replicas adds read capacity and redundancy, never capacity.")
    a("")
    a("## Reading this")
    a("")
    a("* **ops/sec is bounded by the network round trip, not by Redis.** Redis "
      "serves from RAM in microseconds; most of each millisecond above is the hop. "
      "Raising `-P` (pipeline) is what shows Redis's real ceiling.")
    a("* **These numbers describe this cluster on this day.** Keep the run "
      "parameters identical when comparing, or you are comparing the parameters.")
    a("* **A benchmark with writes enabled competes with your application** for the "
      "same single thread, and on an `allkeys-*` policy it can evict live data.")
    return "\n".join(d)


# --------------------------------------------------------------- HTML report

_HTML_CSS = """
:root{--bg:#0f1218;--panel:#161b24;--panel2:#1b2130;--line:#283040;
 --fg:#d7dce5;--dim:#8b94a6;--accent:#4f8cff;--ok:#3fb950;--warn:#d29922;--err:#f85149}
*{box-sizing:border-box}
body{margin:0;padding:32px 24px;background:var(--bg);color:var(--fg);
 font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1040px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:16px;margin:30px 0 10px;padding-bottom:7px;border-bottom:1px solid var(--line)}
h3{font-size:13.5px;margin:20px 0 8px;color:var(--dim);text-transform:uppercase;
 letter-spacing:.06em}
.sub{color:var(--dim);font-size:13px;margin-bottom:18px}
.mono,code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
code{background:var(--panel2);padding:1px 5px;border-radius:4px}
table{width:100%;border-collapse:collapse;margin:10px 0 4px;font-size:13px}
th{text-align:left;color:var(--dim);font-weight:600;font-size:11.5px;
 text-transform:uppercase;letter-spacing:.05em;padding:7px 10px;border-bottom:1px solid var(--line)}
td{padding:8px 10px;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:0}
td.n,th.n{text-align:right;font-family:ui-monospace,Menlo,monospace}
.card{background:var(--panel);border:1px solid var(--line);border-radius:9px;
 padding:16px 18px;margin-bottom:18px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:12px;margin:14px 0 6px}
.kpi{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:13px 14px}
.kpi .v{font-size:21px;font-weight:650;font-family:ui-monospace,Menlo,monospace}
.kpi .l{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin-top:3px}
.bar{position:relative;height:7px;border-radius:4px;background:var(--panel2);
 overflow:hidden;min-width:90px}
.bar>span{position:absolute;inset:0 auto 0 0;border-radius:4px}
.b-ok>span{background:var(--ok)}.b-warn>span{background:var(--warn)}.b-err>span{background:var(--err)}
.note{border-left:3px solid var(--accent);background:var(--panel2);padding:11px 14px;
 border-radius:0 6px 6px 0;margin:12px 0;font-size:13px}
.note.w{border-left-color:var(--warn)}.note.e{border-left-color:var(--err)}
.dim{color:var(--dim)}.ok{color:var(--ok)}.warn{color:var(--warn)}.err{color:var(--err)}
footer{color:var(--dim);font-size:11.5px;margin-top:30px;padding-top:14px;
 border-top:1px solid var(--line)}
@media print{body{background:#fff;color:#111}.card{border-color:#ccc}}
"""


def _bar(pct: float) -> str:
    cls = "b-err" if pct >= 90 else "b-warn" if pct >= 75 else "b-ok"
    w = max(0.0, min(100.0, pct))
    return (f'<div class="bar {cls}"><span style="width:{w:.1f}%"></span></div>')


def _esc(x: Any) -> str:
    return (str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _report_html(ns, name, t, spec, before, after, per_pod, cluster, slots,
                 agg_by_ep, eps) -> str:
    import datetime
    h: list[str] = []
    a = h.append
    a("<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    a("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    a(f"<title>Redis benchmark - {_esc(ns)}/{_esc(name)}</title>")
    a(f"<style>{_HTML_CSS}</style></head><body><div class='wrap'>")
    a(f"<h1>Redis benchmark</h1>")
    a(f"<div class='sub'><span class='mono'>{_esc(ns)}/{_esc(name)}</span> &middot; "
      f"topology <strong>{_esc(t.topology)}</strong> &middot; {len(before)} pod(s) "
      f"&middot; {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}</div>")

    # headline numbers
    pick = agg_by_ep.get("write") or agg_by_ep.get("read") or []
    get = next((r for r in pick if r["test"] == "GET"), None)
    st = next((r for r in pick if r["test"] == "SET"), None)
    a("<div class='kpis'>")
    if get:
        a(f"<div class='kpi'><div class='v'>{get['rps']:,.0f}</div>"
          f"<div class='l'>reads / sec</div></div>")
        a(f"<div class='kpi'><div class='v'>{get['p99']:.2f} ms</div>"
          f"<div class='l'>read p99</div></div>")
    if st:
        a(f"<div class='kpi'><div class='v'>{st['rps']:,.0f}</div>"
          f"<div class='l'>writes / sec</div></div>")
        a(f"<div class='kpi'><div class='v'>{st['p99']:.2f} ms</div>"
          f"<div class='l'>write p99</div></div>")
    tot = sum(v.get("keys", 0) for k, v in after.items()
              if v.get("role") == "primary") or sum(
                  v.get("keys", 0) for v in after.values())
    a(f"<div class='kpi'><div class='v'>{tot:,}</div><div class='l'>keys held</div></div>")
    a("</div>")

    a(f"<div class='sub'>Load: {spec.requests:,} requests &middot; {spec.clients} clients "
      f"&middot; {spec.datasize} byte values &middot; pipeline {spec.pipeline} "
      f"&middot; keyspace {spec.keyspace:,}</div>")

    # endpoints
    a("<div class='card'><h2>Service endpoints</h2>")
    if agg_by_ep:
        a("<table><tr><th>Endpoint</th><th>Service</th><th>Serves</th><th>Op</th>"
          "<th class='n'>ops/sec</th><th class='n'>p50</th><th class='n'>p95</th>"
          "<th class='n'>p99</th><th class='n'>max</th></tr>")
        serves = {"write": "the primary only", "read": "every pod"}
        for which, rows in agg_by_ep.items():
            for r in rows:
                a(f"<tr><td>{which}</td><td class='mono'>{_esc(eps.get(which,'?'))}</td>"
                  f"<td class='dim'>{serves.get(which,'')}</td><td>{r['test']}</td>"
                  f"<td class='n'>{r['rps']:,.0f}</td><td class='n'>{r['p50']:.3f}</td>"
                  f"<td class='n'>{r['p95']:.3f}</td><td class='n'>{r['p99']:.3f}</td>"
                  f"<td class='n'>{r['max']:.3f}</td></tr>")
        a("</table>")
        w = next((r["rps"] for r in agg_by_ep.get("write", []) if r["test"] == "GET"), 0)
        rr = next((r["rps"] for r in agg_by_ep.get("read", []) if r["test"] == "GET"), 0)
        if w and rr:
            cls = "" if rr / w >= 1.2 else " w"
            a(f"<div class='note{cls}'><strong>Read endpoint vs write endpoint: "
              f"{rr:,.0f} vs {w:,.0f} ops/sec ({rr/w:.2f}x).</strong> The write "
              f"endpoint is one pod; the read endpoint spreads across all of them. "
              f"A ratio near 1.0 means the client is not getting read scaling -- "
              f"usually because connections are long-lived and kube-proxy balances "
              f"per connection, not per request.</div>")
    else:
        a("<div class='note e'>No benchmark output was produced.</div>")
    a("</div>")

    # per pod
    if per_pod:
        a("<div class='card'><h2>Per pod, measured directly</h2>")
        a("<div class='sub'>Each pod benchmarked on its own IP, bypassing the Service.</div>")
        a("<table><tr><th>Pod</th><th>Role</th><th class='n'>ops/sec</th>"
          "<th class='n'>p50</th><th class='n'>p99</th></tr>")
        rates = {}
        for pod, rows in per_pod.items():
            if not rows:
                continue
            r = rows[0]
            rates[pod] = r["rps"]
            a(f"<tr><td class='mono'>{_esc(pod)}</td>"
              f"<td>{_esc(after.get(pod,{}).get('role','?'))}</td>"
              f"<td class='n'>{r['rps']:,.0f}</td><td class='n'>{r['p50']:.3f}</td>"
              f"<td class='n'>{r['p99']:.3f}</td></tr>")
        a("</table>")
        if len(rates) > 1:
            hi, lo = max(rates.values()), max(min(rates.values()), 1)
            if hi / lo >= 1.3:
                a(f"<div class='note w'><strong>{hi/lo:.1f}x spread between fastest "
                  f"and slowest pod.</strong> <code>{_esc(min(rates, key=rates.get))}"
                  f"</code> is the laggard. Pods are usually near-identical, so check "
                  f"what else runs on its node and whether its CPU limit differs.</div>")
            else:
                a(f"<div class='note'>Pods are within {hi/lo:.2f}x of each other, "
                  f"which is the healthy case.</div>")
        a("</div>")

    # memory utilisation
    a("<div class='card'><h2>Pod memory utilisation</h2>")
    a("<table><tr><th>Pod</th><th>Role</th><th class='n'>Keys</th><th class='n'>Used</th>"
      "<th>of maxmemory</th><th class='n'>RSS</th><th>of container limit</th>"
      "<th class='n'>Frag</th></tr>")
    for pod in sorted(after):
        d = after[pod]
        pm, pl = d.get("pct_maxmemory", 0.0), d.get("pct_limit", 0.0)
        a(f"<tr><td class='mono'>{_esc(pod)}</td><td>{_esc(d.get('role','?'))}</td>"
          f"<td class='n'>{d.get('keys',0):,}</td><td class='n'>{_esc(d.get('used','?'))}</td>"
          f"<td>{_bar(pm)}<span class='dim mono'>{pm:.1f}% of "
          f"{_esc(d.get('maxmemory','-'))}</span></td>"
          f"<td class='n'>{_esc(d.get('rss','?'))}</td>"
          f"<td>{_bar(pl)}<span class='dim mono'>{pl:.1f}% of "
          f"{_human(d.get('limit_bytes',0)) if d.get('limit_bytes') else 'no limit'}"
          f"</span></td>"
          f"<td class='n'>{_esc(d.get('frag','?'))}</td></tr>")
    a("</table>")
    a("<div class='note'>Two different ceilings. <code>used_memory</code> against "
      "<code>maxmemory</code> is how full the cache is, and Redis handles reaching it "
      "with the eviction policy. <strong>RSS against the container limit is what the "
      "kubelet watches</strong>, and crossing that is an OOMKill with no warning -- "
      "replica output buffers and fork copy-on-write count toward it but sit outside "
      "<code>maxmemory</code>.</div>")
    hot = [p for p in after if after[p].get("pct_limit", 0) >= 80]
    if hot:
        a(f"<div class='note e'><strong>Above 80% of the container limit:</strong> "
          + ", ".join(f"<code>{_esc(p)}</code>" for p in hot) + "</div>")
    full = [p for p in after if after[p].get("pct_maxmemory", 0) >= 80]
    if full:
        a(f"<div class='note w'><strong>Above 80% of maxmemory:</strong> "
          + ", ".join(f"<code>{_esc(p)}</code>" for p in full)
          + " &mdash; at 100% the eviction policy decides what happens.</div>")
    a("</div>")

    # shards / replication
    a("<div class='card'><h2>Data distribution</h2>")
    if cluster:
        a("<table><tr><th>Pod</th><th>Hash slots</th><th class='n'>Keys</th></tr>")
        for pod, rng in sorted(slots.items()):
            a(f"<tr><td class='mono'>{_esc(pod)}</td>"
              f"<td class='mono'>{_esc(rng) if rng else '<span class=dim>replica</span>'}</td>"
              f"<td class='n'>{after.get(pod,{}).get('keys',0):,}</td></tr>")
        a("</table>")
        masters = {p: after[p]["keys"] for p in after if after[p].get("role") == "primary"}
        if len(masters) > 1 and max(masters.values()) > 0:
            hi, lo = max(masters.values()), max(min(masters.values()), 1)
            if hi / lo >= 1.5:
                a(f"<div class='note w'><strong>{hi/lo:.1f}x key skew across "
                  f"primaries.</strong> Redis places keys by CRC16, so an even spread "
                  f"needs varied names. Hash tags <code>{{...}}</code> force keys into "
                  f"one slot and therefore one shard.</div>")
    else:
        a("<div class='note'>Every replica holds a <strong>full copy</strong> of the "
          "primary's data, so matching key counts are correct: this topology does not "
          "shard. Replicas add read capacity and redundancy, never capacity.</div>")
    a("</div>")

    a("<footer>Generated by RedisCover. Figures describe this cluster at this moment; "
      "keep the run parameters identical when comparing, or you are comparing the "
      "parameters. At pipeline 1 you are measuring the network round trip, not Redis."
      "</footer>")
    a("</div></body></html>")
    return "\n".join(h)
