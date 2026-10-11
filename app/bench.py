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
        snap[pod] = {"used": mem.get("used_memory_human", "?"),
                     "used_bytes": int(mem.get("used_memory", 0) or 0),
                     "rss": mem.get("used_memory_rss_human", "?"),
                     "frag": mem.get("mem_fragmentation_ratio", "?"),
                     "maxmemory": mem.get("maxmemory_human", "?"),
                     "keys": keys, "role": role}
        job.log(f"    {pod:<28} {role:<9} keys={keys:<9} used={snap[pod]['used']}")
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

    job.step(3, total, "Aggregate throughput against the service endpoint")
    if spec.include_writes:
        job.log("  WRITES ENABLED -- this inserts keys into the live keyspace.")
        job.log(f"  On an eviction policy like allkeys-lru, inserting up to "
                f"{spec.keyspace:,} keys can evict real application data.")
        tests = "set,get"
    else:
        job.log("  read-only run (GET). Enable writes for SET figures.")
        tests = "get"
    agg = _bench(job, kubeconfig, ns, pods[0], t.host, t.port, bench_auth,
                 tests, spec, cluster, "aggregate")

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

    job.result["report"] = _report(ns, name, t, spec, before, after, agg,
                                   per_pod, cluster,
                                   _slots(kubeconfig, ns, pods[0], auth) if cluster else {})
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


def _report(ns, name, t, spec, before, after, agg, per_pod, cluster, slots) -> str:
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
    a("## Throughput through the service endpoint")
    a("")
    if agg:
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
    a("| Pod | Role | Keys before | Keys after | Memory before | Memory after | Frag |")
    a("|---|---|---:|---:|---:|---:|---:|")
    for pod in sorted(before):
        b, af = before[pod], after.get(pod, {})
        a(f"| `{pod}` | {af.get('role', b['role'])} | {b['keys']:,} | "
          f"{af.get('keys', 0):,} | {b['used']} | {af.get('used', '?')} | "
          f"{af.get('frag', '?')} |")
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
