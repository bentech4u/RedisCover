"""Test suite for a deployed Redis.

Three tiers by blast radius:
  1 SAFE       read-only, or writes confined to a scratch keyspace
  2 DISRUPTIVE kills pods; refuses to run over data it did not write
  3 LOAD       consumes real CPU/memory

Tests run from a throwaway pod INSIDE the cluster, never via port-forward --
port-forward tunnels through the API server and bypasses NetworkPolicy, which
would hide exactly the class of bug it is meant to catch.
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import ocp

SCRATCH = "__redisdeployer:test"
# Last-resort default only. The client normally reuses the TARGET RELEASE'S OWN
# image: it is already running on this cluster so it is certainly pullable, it
# needs no access to docker.io, and redis-cli then matches the server version.
FALLBACK_CLIENT_IMAGE = "docker.io/redis:7.4-alpine"


# ---------------------------------------------------------------- target

@dataclass
class Target:
    kind: str                 # community | opstree | enterprise
    namespace: str
    name: str
    topology: str = "standalone"
    host: str = ""
    read_host: str = ""
    port: int = 6379
    password: str = ""
    pods: list[str] = field(default_factory=list)
    primary: str = ""
    replicas: list[str] = field(default_factory=list)
    workload: str = "deployment"
    image: str = ""
    sentinel_host: str = ""
    sentinel_port: int = 26379
    master_group: str = "myMaster"
    watches: str = ""
    allowed_ns: list[str] = field(default_factory=list)


def _secret_value(kubeconfig: str, ns: str, name: str, key: str) -> str:
    raw = ocp.run(kubeconfig, ["get", "secret", name, "-n", ns,
                               "-o", f"jsonpath={{.data.{key}}}"],
                  check=False, timeout=45).stdout.strip()
    return base64.b64decode(raw).decode() if raw else ""


def resolve_target(kubeconfig: str, kind: str, namespace: str, name: str,
                   log=None, topology: str = "", password: str = "") -> Target:
    t = Target(kind=kind, namespace=namespace, name=name)
    if topology:
        # the caller already knows which CR this is; probing would find the
        # wrong one when a Replication and a Sentinel share a name
        t.topology = topology

    # Ask the API which custom resource exists, rather than reading a label off
    # whatever `oc get all` happens to return first. An operator-managed release
    # has both a RedisReplication and a RedisSentinel under the same name.
    if kind == "opstree" and not topology:
        for crd, topo, port in (("redissentinel", "sentinel", 26379),
                                ("rediscluster", "cluster", 6379),
                                ("redisreplication", "replication", 6379),
                                ("redis", "standalone", 6379)):
            if ocp.run(kubeconfig, ["get", crd, name, "-n", namespace],
                       check=False, timeout=30).returncode == 0:
                t.topology, t.port = topo, port
                break
    elif not topology:
        t.topology = ocp.jsonpath(
            kubeconfig, ["get", "all", "-n", namespace, "-l", f"app={name}"],
            "{.items[0].metadata.labels.redis-deployer/topology}") or "standalone"

    if ocp.run(kubeconfig, ["get", "statefulset", name, "-n", namespace],
               check=False, timeout=30).returncode == 0:
        t.workload = "statefulset"

    if kind == "enterprise":
        t.password = _secret_value(kubeconfig, namespace, f"redb-{name}", "password")
        eps = ocp.jsonpath(kubeconfig, ["get", "redb", name, "-n", namespace],
                           "{.status.internalEndpoints[0].host}")
        t.port = int(ocp.jsonpath(kubeconfig, ["get", "redb", name, "-n", namespace],
                                  "{.status.internalEndpoints[0].port}") or 6379)
        t.host = eps or f"{name}.{namespace}.svc"
        t.topology = "enterprise"
    else:
        for key in ("redis-password", "password"):
            t.password = _secret_value(kubeconfig, namespace, f"{name}-auth", key)
            if t.password:
                break
        domain = ocp.cluster_domain(kubeconfig, namespace)
        if t.topology == "sentinel":
            # Sentinel is a control plane, not a data endpoint. It answers
            # SENTINEL commands on 26379 and never serves keys. The data
            # endpoint is whichever node it currently calls the primary --
            # which is the whole point, so we ask it rather than guess.
            t.sentinel_host = f"{name}-sentinel.{namespace}.svc.{domain}"
            t.sentinel_port = 26379
            t.master_group = ocp.jsonpath(
                kubeconfig, ["get", "redissentinel", name, "-n", namespace],
                "{.spec.redisSentinelConfig.masterGroupName}") or "myMaster"
            t.watches = ocp.jsonpath(
                kubeconfig, ["get", "redissentinel", name, "-n", namespace],
                "{.spec.redisSentinelConfig.redisReplicationName}") or ""
            # the data endpoint is resolved AFTER the pod list exists -- asking
            # Sentinel requires a pod to exec into
        else:
            t.host = f"{name}.{namespace}.svc.{domain}"
        if t.topology == "replication":
            t.read_host = f"{name}-read.{namespace}.svc.{domain}"

    selector = f"app={name}"
    if t.topology in ("sentinel", "replication", "cluster"):
        # both sets of pods share app=<name>; role= separates them
        role = {"sentinel": "sentinel", "replication": "replication"}.get(t.topology)
        if role:
            selector += f",role={role}"
    p = ocp.run(kubeconfig, ["get", "pods", "-n", namespace, "-l", selector,
                             "-o", "jsonpath={range .items[*]}{.metadata.name}{\" \"}{end}"],
                check=False, timeout=45)
    t.pods = sorted((p.stdout or "").split())
    if not t.pods:
        p = ocp.run(kubeconfig, ["get", "pods", "-n", namespace, "--no-headers",
                                 "-o", "custom-columns=N:.metadata.name"],
                    check=False, timeout=45)
        t.pods = sorted(l.strip() for l in (p.stdout or "").splitlines() if l.strip())

    # who is the primary, asked rather than assumed. Sentinel pods run
    # redis-sentinel and have no data role, so probing them is meaningless.
    for pod in ([] if t.topology == "sentinel" else t.pods):
        role = _role_of(kubeconfig, namespace, pod, t.password)
        if role == "master":
            t.primary = t.primary or pod
        elif role in ("slave", "replica"):
            t.replicas.append(pod)

    if t.pods:
        t.image = ocp.jsonpath(
            kubeconfig, ["get", "pod", t.pods[0], "-n", namespace],
            "{.spec.containers[0].image}")

    if password:
        t.password = password
        if log:
            log("  using the password supplied on the form, not the Secret")

    if t.topology == "sentinel":
        t.host, t.port = _ask_sentinel(kubeconfig, t, log)

    pol = ocp.jsonpath(
        kubeconfig, ["get", "networkpolicy", "-n", namespace],
        "{range .items[*].spec.ingress[*].from[*]}"
        "{.namespaceSelector.matchLabels.kubernetes\\.io/metadata\\.name}{\" \"}{end}")
    t.allowed_ns = [x for x in pol.split() if x]
    return t


def _ask_sentinel(kubeconfig: str, t: Target, log=None) -> tuple[str, int]:
    """Ask a Sentinel which node is currently the primary.

    This is the only correct way to find a Sentinel-managed primary: after a
    failover it is a different pod, and any hardcoded host is wrong.
    """
    auth = ["-a", t.password, "--no-auth-warning"] if t.password else []
    for pod in t.pods or []:
        p = ocp.run(kubeconfig, ["exec", "-n", t.namespace, pod, "--", "redis-cli",
                                 "-p", str(t.sentinel_port), *auth,
                                 "SENTINEL", "get-master-addr-by-name", t.master_group],
                    check=False, timeout=45)
        lines = [l.strip() for l in (p.stdout or "").splitlines() if l.strip()]
        if len(lines) >= 2 and lines[1].isdigit():
            if log:
                log(f"  sentinel reports the primary is {lines[0]}:{lines[1]}")
            return lines[0], int(lines[1])
    if log:
        log("  sentinel could not name a primary -- it may still be discovering, "
            "or it is not monitoring anything")
    return "", 6379


def _role_of(kubeconfig: str, ns: str, pod: str, password: str) -> str:
    auth = ["-a", password, "--no-auth-warning"] if password else []
    p = ocp.run(kubeconfig, ["exec", "-n", ns, pod, "--", "redis-cli", *auth,
                             "INFO", "replication"], check=False, timeout=45)
    m = re.search(r"role:(\w+)", p.stdout or "")
    return m.group(1) if m else ""


# ---------------------------------------------------------------- client pod

class Client:
    """A throwaway pod that runs redis-cli from inside the cluster."""

    def __init__(self, kubeconfig: str, namespace: str, log, image: str = ""):
        self.kc = kubeconfig
        self.ns = namespace
        self.log = log
        self.image = image or FALLBACK_CLIENT_IMAGE
        self.pod = f"redis-test-{int(time.time()) % 100000}"

    def start(self) -> None:
        self.log(f"  starting test client pod {self.ns}/{self.pod}")
        self.log(f"  image: {self.image}")
        p = ocp.run(self.kc, ["run", self.pod, "-n", self.ns, "--image", self.image,
                              "--restart=Never", "--command", "--", "sleep", "3600"],
                    check=False, timeout=90)
        if p.returncode != 0:
            raise RuntimeError(
                f"could not create the test client pod: "
                f"{(p.stderr or p.stdout or '').strip()[:200]}")
        try:
            ocp.wait_for(self.kc, ["get", "pod", self.pod, "-n", self.ns],
                         "{.status.phase}", "Running", timeout=180,
                         label="test client", log=None)
        except TimeoutError:
            reason = ocp.jsonpath(
                self.kc, ["get", "pod", self.pod, "-n", self.ns],
                "{.status.containerStatuses[0].state.waiting.reason}")
            msg = ocp.jsonpath(
                self.kc, ["get", "pod", self.pod, "-n", self.ns],
                "{.status.containerStatuses[0].state.waiting.message}")
            hint = ""
            if "ImagePull" in (reason or "") or "Err" in (reason or ""):
                hint = (f" -- this cluster cannot pull '{self.image}'. On a restricted "
                        "or disconnected cluster, set a client image from a registry it "
                        "can reach (the 'Test client image' field).")
            raise RuntimeError(
                f"the test client pod never started ({reason or 'unknown'}){hint}"
                + (f"\n  {msg}" if msg else ""))

    def stop(self) -> None:
        ocp.run(self.kc, ["delete", "pod", self.pod, "-n", self.ns,
                          "--ignore-not-found", "--grace-period=0", "--force"],
                check=False, timeout=90)

    def cli(self, target: Target, *args: str, host: str = "", port: int = 0,
            no_auth: bool = False, timeout: int = 60) -> tuple[int, str]:
        """Run redis-cli in the client pod.

        Wrapped in `timeout` because redis-cli has no connect-timeout flag: a
        blocked route (a NetworkPolicy that does not list this namespace) would
        otherwise hang until the oc exec itself times out, turning a clear
        'connection refused' into a mystery stall.
        """
        import shlex
        auth = [] if no_auth or not target.password else [
            "-a", target.password, "--no-auth-warning"]
        inner = ["redis-cli", "-h", host or target.host,
                 "-p", str(port or target.port), *auth, *args]
        wall = max(5, timeout - 10)
        script = f"timeout {wall} " + " ".join(shlex.quote(a) for a in inner)
        p = ocp.run(self.kc, ["exec", "-n", self.ns, self.pod, "--", "sh", "-c", script],
                    check=False, timeout=timeout)
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        if p.returncode == 124 or (p.returncode == 143 and not out):
            return 124, f"TIMEOUT after {wall}s connecting to {host or target.host}"
        return p.returncode, out

    def reachable(self, target: Target) -> tuple[bool, str]:
        rc, out = self.cli(target, "PING", timeout=20)
        return ("PONG" in out), out


# ---------------------------------------------------------------- registry

@dataclass
class Result:
    id: str
    name: str
    status: str            # pass | fail | skip | warn
    detail: str = ""
    metrics: dict = field(default_factory=dict)


TESTS: list[dict] = []


def test(tid: str, name: str, tier: int, *, topologies: list[str] | None = None,
         disruptive: bool = False, describe: str = "", expects: dict | None = None):
    def deco(fn: Callable):
        TESTS.append({"id": tid, "name": name, "tier": tier, "fn": fn,
                      "topologies": topologies, "disruptive": disruptive,
                      "describe": describe, "expects": expects or {}})
        return fn
    return deco


def applicable(topology: str) -> list[dict]:
    out = []
    for t in TESTS:
        if t["topologies"] and topology not in t["topologies"]:
            continue
        out.append({k: v for k, v in t.items() if k != "fn"})
    return out


# ================================================================ TIER 1 - safe

@test("auth", "Authentication is enforced", 1,
      describe="PING with the password must succeed; PING without it must be refused.")
def t_auth(ctx) -> Result:
    c, t = ctx.client, ctx.target
    rc, out = c.cli(t, "PING")
    if "PONG" not in out:
        return Result("auth", "Authentication is enforced", "fail",
                      f"authenticated PING failed: {out}")
    if not t.password:
        return Result("auth", "Authentication is enforced", "warn",
                      "no password is set -- anyone who can reach the port has full access")
    rc, out = c.cli(t, "PING", no_auth=True)
    if "NOAUTH" not in out:
        return Result("auth", "Authentication is enforced", "fail",
                      f"unauthenticated PING was NOT refused (got: {out[:80]}) -- "
                      "requirepass may not have taken effect")
    return Result("auth", "Authentication is enforced", "pass",
                  "authenticated PING -> PONG; unauthenticated -> NOAUTH")


@test("roundtrip", "Data round-trip and types", 1,
      describe="SET/GET/DEL, TTL expiry, and the list/hash/set types.")
def t_roundtrip(ctx) -> Result:
    c, t = ctx.client, ctx.target
    k = f"{SCRATCH}:rt"
    checks = []
    c.cli(t, "SET", k, "hello")
    _, v = c.cli(t, "GET", k)
    checks.append(("string", v == "hello", v))

    c.cli(t, "SET", f"{k}:ttl", "x", "EX", "30")
    _, ttl = c.cli(t, "TTL", f"{k}:ttl")
    checks.append(("ttl", ttl.isdigit() and 0 < int(ttl) <= 30, ttl))

    c.cli(t, "RPUSH", f"{k}:list", "a", "b", "c")
    _, ln = c.cli(t, "LLEN", f"{k}:list")
    checks.append(("list", ln.strip().endswith("3"), ln))

    c.cli(t, "HSET", f"{k}:hash", "f", "v")
    _, hv = c.cli(t, "HGET", f"{k}:hash", "f")
    checks.append(("hash", hv == "v", hv))

    c.cli(t, "SADD", f"{k}:set", "x", "y")
    _, sc = c.cli(t, "SCARD", f"{k}:set")
    checks.append(("set", sc.strip().endswith("2"), sc))

    _, d = c.cli(t, "DEL", k)
    checks.append(("del", d.strip().endswith("1"), d))

    bad = [f"{n} (got {v!r})" for n, ok, v in checks if not ok]
    if bad:
        return Result("roundtrip", "Data round-trip and types", "fail", "; ".join(bad))
    return Result("roundtrip", "Data round-trip and types", "pass",
                  "string, ttl, list, hash, set, delete all behaved")


@test("config", "Configuration actually took effect", 1,
      describe="Reads maxmemory, eviction policy and persistence back from the "
               "running server -- a ConfigMap that never loaded looks fine in oc get.")
def t_config(ctx) -> Result:
    c, t = ctx.client, ctx.target
    wanted = ["maxmemory", "maxmemory-policy", "appendonly", "dir"]
    _, out = c.cli(t, "CONFIG", "GET", *wanted)
    lines = [l for l in out.splitlines() if l.strip()]
    cfg = dict(zip(lines[::2], lines[1::2]))
    if not cfg:
        return Result("config", "Configuration actually took effect", "skip",
                      "CONFIG GET returned nothing (Enterprise restricts it)")
    mm = int(cfg.get("maxmemory", "0") or 0)
    detail = ", ".join(f"{k}={v}" for k, v in cfg.items())
    status = "pass"
    notes = []
    if mm == 0:
        status = "warn"
        notes.append("maxmemory is 0 (unlimited) -- Redis will grow until the "
                     "container hits its memory limit and is OOMKilled")
    if cfg.get("maxmemory-policy") == "noeviction":
        notes.append("noeviction: writes are REJECTED when full (datastore behaviour)")
    else:
        notes.append(f"{cfg.get('maxmemory-policy')}: keys are silently evicted "
                     "when full (cache behaviour)")
    return Result("config", "Configuration actually took effect", status,
                  detail + " | " + "; ".join(notes), {"config": cfg})


@test("persistence", "Persistence is armed", 1,
      describe="Checks AOF/RDB status as the server reports it.")
def t_persistence(ctx) -> Result:
    c, t = ctx.client, ctx.target
    _, out = c.cli(t, "INFO", "persistence")
    info = dict(l.split(":", 1) for l in out.splitlines() if ":" in l and not l.startswith("#"))
    aof = info.get("aof_enabled", "0").strip()
    rdb_ok = info.get("rdb_last_bgsave_status", "").strip()
    loading = info.get("loading", "0").strip()
    if aof != "1" and rdb_ok not in ("ok",):
        return Result("persistence", "Persistence is armed", "warn",
                      "neither AOF nor RDB is active -- a full restart loses everything",
                      info)
    bits = []
    if aof == "1":
        bits.append(f"AOF on (last write status: {info.get('aof_last_write_status','?').strip()})")
    if rdb_ok:
        bits.append(f"RDB last bgsave: {rdb_ok}")
    if loading == "1":
        bits.append("currently LOADING from disk")
    return Result("persistence", "Persistence is armed", "pass", "; ".join(bits), info)


@test("replication", "Replication is healthy", 1,
      topologies=["replication", "sentinel", "enterprise"],
      describe="Every replica linked to the primary, and the offset lag between them.")
def t_replication(ctx) -> Result:
    c, t = ctx.client, ctx.target
    if not t.primary:
        return Result("replication", "Replication is healthy", "skip",
                      "no primary identified")
    auth = ["-a", t.password, "--no-auth-warning"] if t.password else []
    p = ocp.run(ctx.kubeconfig, ["exec", "-n", t.namespace, t.primary, "--",
                                 "redis-cli", *auth, "INFO", "replication"],
                check=False, timeout=45)
    info = p.stdout or ""
    connected = int((re.search(r"connected_slaves:(\d+)", info) or [0, 0])[1])
    slaves = re.findall(r"^slave\d+:ip=([\d.]+),port=\d+,state=(\w+),offset=(\d+),lag=(\d+)",
                        info, re.M)
    expected = len(t.replicas)
    detail = f"primary {t.primary}: connected_slaves={connected}"
    for ip, state, off, lag in slaves:
        detail += f" | {ip} state={state} lag={lag}s"
    if expected and connected < expected:
        return Result("replication", "Replication is healthy", "fail",
                      detail + f" -- expected {expected} replicas. "
                      "A NetworkPolicy that omits peer traffic is the usual cause.",
                      {"connected": connected, "expected": expected})
    if any(s != "online" for _, s, _, _ in slaves):
        return Result("replication", "Replication is healthy", "warn",
                      detail + " -- a replica is still syncing")
    return Result("replication", "Replication is healthy", "pass", detail,
                  {"connected": connected})


@test("propagation", "Writes reach the replicas", 1,
      topologies=["replication", "sentinel"],
      describe="Writes to the primary and measures how long each replica takes to converge.")
def t_propagation(ctx) -> Result:
    c, t = ctx.client, ctx.target
    if not t.primary or not t.replicas:
        return Result("propagation", "Writes reach the replicas", "skip",
                      "no replicas found")
    auth = ["-a", t.password, "--no-auth-warning"] if t.password else []
    key, val = f"{SCRATCH}:prop", f"v{int(time.time())}"
    ocp.run(ctx.kubeconfig, ["exec", "-n", t.namespace, t.primary, "--",
                             "redis-cli", *auth, "SET", key, val], timeout=45)
    start = time.time()
    pending = set(t.replicas)
    lags = {}
    while pending and time.time() - start < 15:
        for pod in list(pending):
            p = ocp.run(ctx.kubeconfig, ["exec", "-n", t.namespace, pod, "--",
                                         "redis-cli", *auth, "GET", key],
                        check=False, timeout=30)
            if val in (p.stdout or ""):
                lags[pod] = round(time.time() - start, 2)
                pending.discard(pod)
        if pending:
            time.sleep(0.5)
    if pending:
        return Result("propagation", "Writes reach the replicas", "fail",
                      f"did not converge within 15s: {', '.join(sorted(pending))}")
    detail = ", ".join(f"{k} in {v}s" for k, v in sorted(lags.items()))
    return Result("propagation", "Writes reach the replicas", "pass",
                  detail + " (replication is ASYNCHRONOUS -- read-after-write "
                  "through the read Service may still return a stale value)", lags)


@test("readonly", "Replicas refuse writes", 1,
      topologies=["replication", "sentinel"],
      describe="A replica must reject SET with READONLY.")
def t_readonly(ctx) -> Result:
    if not ctx.target.replicas:
        return Result("readonly", "Replicas refuse writes", "skip", "no replicas")
    t = ctx.target
    auth = ["-a", t.password, "--no-auth-warning"] if t.password else []
    pod = t.replicas[0]
    p = ocp.run(ctx.kubeconfig, ["exec", "-n", t.namespace, pod, "--", "redis-cli",
                                 *auth, "SET", f"{SCRATCH}:ro", "x"],
                check=False, timeout=45)
    out = (p.stdout or "") + (p.stderr or "")
    if "READONLY" in out:
        return Result("readonly", "Replicas refuse writes", "pass",
                      f"{pod} correctly refused a write")
    return Result("readonly", "Replicas refuse writes", "fail",
                  f"{pod} ACCEPTED a write -- it will diverge from the primary: {out[:90]}")


@test("sentinel", "Sentinel is monitoring a primary", 1, topologies=["sentinel"],
      describe="Asks Sentinel who the primary is, how many replicas and other "
               "sentinels it sees, and whether quorum can actually be reached.")
def t_sentinel(ctx) -> Result:
    t = ctx.target
    if not t.pods:
        return Result("sentinel", "Sentinel is monitoring a primary", "skip", "no pods")
    pod = t.pods[0]

    auth = ["-a", t.password, "--no-auth-warning"] if t.password else []

    def sent(*args):
        p = ocp.run(ctx.kubeconfig, ["exec", "-n", t.namespace, pod, "--", "redis-cli",
                                     "-p", str(t.sentinel_port), *auth, "SENTINEL", *args],
                    check=False, timeout=45)
        return (p.stdout or "") + (p.stderr or "")

    masters = sent("masters")
    if "Connection refused" in masters:
        return Result("sentinel", "Sentinel is monitoring a primary", "fail",
                      f"nothing is listening on {t.sentinel_port} in {pod}. The pod is "
                      "probably running the plain redis image instead of redis-sentinel.")
    if not masters.strip():
        return Result("sentinel", "Sentinel is monitoring a primary", "fail",
                      f"SENTINEL masters returned nothing -- it is not monitoring "
                      f"'{t.watches or 'anything'}'")

    addr = sent("get-master-addr-by-name", t.master_group).split()
    info = {}
    lines = [l.strip() for l in masters.splitlines() if l.strip()]
    for k, v in zip(lines[::2], lines[1::2]):
        info[k] = v
    slaves = info.get("num-slaves", "?")
    sentinels = info.get("num-other-sentinels", "?")
    quorum = info.get("quorum", "?")
    flags = info.get("flags", "?")

    detail = (f"group '{t.master_group}' primary={':'.join(addr[:2]) if len(addr) >= 2 else '?'} "
              f"flags={flags} replicas={slaves} other-sentinels={sentinels} quorum={quorum}")
    if "s_down" in flags or "o_down" in flags:
        hint = ""
        probe = ocp.run(ctx.kubeconfig,
                        ["exec", "-n", t.namespace, pod, "--", "sh", "-c",
                         f"timeout 5 redis-cli -h {addr[0]} -p {addr[1] if len(addr) > 1 else 6379} PING"],
                        check=False, timeout=30) if len(addr) >= 2 else None
        if probe and "NOAUTH" in ((probe.stdout or "") + (probe.stderr or "")):
            hint = (" -- Sentinel can REACH the primary but cannot authenticate to it: "
                    "its config has no 'sentinel auth-pass' line for this group. Add "
                    f"`sentinel auth-pass {t.master_group} <password>` to the Sentinel's "
                    "extra redis.conf directives, or the failover it is supposed to "
                    "perform will never happen.")
        return Result("sentinel", "Sentinel is monitoring a primary", "fail",
                      detail + (hint or " -- the primary is marked DOWN"))
    try:
        if int(sentinels) + 1 < int(quorum):
            return Result("sentinel", "Sentinel is monitoring a primary", "fail",
                          detail + f" -- only {int(sentinels)+1} sentinel(s) can vote but "
                          f"quorum is {quorum}; a failover could never be agreed")
    except ValueError:
        pass
    return Result("sentinel", "Sentinel is monitoring a primary", "pass", detail,
                  {"replicas": slaves, "sentinels": sentinels, "quorum": quorum})


@test("cluster", "Cluster state and slot coverage", 1, topologies=["cluster"],
      describe="CLUSTER INFO state, and that all 16384 hash slots are assigned.")
def t_cluster(ctx) -> Result:
    c, t = ctx.client, ctx.target
    _, out = c.cli(t, "CLUSTER", "INFO")
    state = (re.search(r"cluster_state:(\w+)", out) or ["", "?"])[1]
    slots = int((re.search(r"cluster_slots_assigned:(\d+)", out) or [0, 0])[1])
    known = int((re.search(r"cluster_known_nodes:(\d+)", out) or [0, 0])[1])
    detail = f"state={state} slots_assigned={slots}/16384 known_nodes={known}"
    if state != "ok" or slots != 16384:
        return Result("cluster", "Cluster state and slot coverage", "fail", detail)
    return Result("cluster", "Cluster state and slot coverage", "pass", detail,
                  {"slots": slots, "nodes": known})


@test("networkpolicy", "NetworkPolicy allows and denies correctly", 1,
      describe="Connects from an allowed namespace and from a denied one. "
               "A policy tested in one direction only is not a control.")
def t_networkpolicy(ctx) -> Result:
    t = ctx.target
    if not t.allowed_ns:
        return Result("networkpolicy", "NetworkPolicy allows and denies correctly",
                      "skip", "no NetworkPolicy restricts this release -- every pod "
                              "on the cluster can reach it")
    if ctx.client.ns not in t.allowed_ns:
        return Result("networkpolicy", "NetworkPolicy allows and denies correctly",
                      "warn", f"test client runs in '{ctx.client.ns}', which the policy "
                              f"does not list ({', '.join(t.allowed_ns)}) -- rerun from "
                              "an allowed namespace to exercise the real path")
    rc, out = ctx.client.cli(t, "PING", timeout=20)
    if "PONG" not in out:
        return Result("networkpolicy", "NetworkPolicy allows and denies correctly",
                      "fail", f"allowed namespace '{ctx.client.ns}' could NOT connect: {out[:80]}")
    return Result("networkpolicy", "NetworkPolicy allows and denies correctly", "pass",
                  f"reachable from allowed namespace '{ctx.client.ns}'; "
                  f"policy lists {', '.join(t.allowed_ns)}")


# ============================================ TIER 2 - disruptive

def _seed(ctx, n: int = 1000) -> str:
    """Write a known keyset so data loss becomes a number, not an opinion."""
    c, t = ctx.client, ctx.target
    tag = f"{SCRATCH}:seed:{int(time.time())}"
    c.cli(t, "EVAL",
          f"for i=1,{n} do redis.call('SET', KEYS[1]..':'..i, i) end return 1",
          "1", tag, timeout=120)
    return tag


def _count(ctx, tag: str, host: str = "") -> int:
    _, out = ctx.client.cli(ctx.target, "EVAL",
                            "local c=0 for i=1,tonumber(ARGV[1]) do "
                            "if redis.call('EXISTS', KEYS[1]..':'..i)==1 then c=c+1 end end return c",
                            "1", tag, "1000", host=host, timeout=120)
    m = re.search(r"(\d+)", out)
    return int(m.group(1)) if m else -1


def _write_outage(ctx, deadline: float = 300.0) -> float:
    """Seconds until a write succeeds again. This is the number the app team
    needs for their retry and timeout configuration."""
    c, t = ctx.client, ctx.target
    start = time.time()
    while time.time() - start < deadline:
        rc, out = c.cli(t, "SET", f"{SCRATCH}:probe", "1", timeout=15)
        if out.strip().endswith("OK"):
            return round(time.time() - start, 1)
        time.sleep(1)
    return -1.0


@test("kill_primary", "Hard-kill the primary", 2, disruptive=True,
      describe="Deletes the primary pod with --grace-period=0 --force: no clean "
               "shutdown, no final save. Measures the write outage and verifies "
               "the seeded keyset survives.",
      expects={
          "standalone": "~30s outage while the pod restarts; data intact via AOF",
          "replication": "writes fail until the SAME pod returns -- no promotion",
          "sentinel": "a replica is promoted; outage measured in seconds",
          "cluster": "the shard's replica is promoted automatically",
          "enterprise": "the replica shard is promoted by the cluster manager",
      })
def t_kill_primary(ctx) -> Result:
    c, t = ctx.client, ctx.target
    pod = t.primary or (t.pods[0] if t.pods else "")
    if not pod:
        return Result("kill_primary", "Hard-kill the primary", "skip", "no pod found")

    tag = _seed(ctx)
    before = _count(ctx, tag)
    ctx.log(f"  seeded {before} keys, primary is {pod}")
    ctx.log(f"  deleting {pod} with --grace-period=0 --force")
    ocp.run(ctx.kubeconfig, ["delete", "pod", pod, "-n", t.namespace,
                             "--grace-period=0", "--force"],
            check=False, timeout=120, log=ctx.log)

    outage = _write_outage(ctx)
    ctx.log(f"  writes resumed after {outage}s")

    time.sleep(5)
    after = _count(ctx, tag)
    new_primary = ""
    for p in t.pods:
        if _role_of(ctx.kubeconfig, t.namespace, p, t.password) == "master":
            new_primary = p
            break
    promoted = bool(new_primary and new_primary != pod)

    detail = (f"outage {outage}s | keys before {before}, after {after} | "
              f"primary was {pod}, now {new_primary or '?'}"
              f"{' (PROMOTED)' if promoted else ' (same pod restarted)'}")
    if outage < 0:
        return Result("kill_primary", "Hard-kill the primary", "fail",
                      "writes never resumed within 300s | " + detail)
    if after < before:
        return Result("kill_primary", "Hard-kill the primary", "fail",
                      f"DATA LOSS: {before - after} of {before} keys missing | " + detail,
                      {"outage_s": outage, "lost": before - after})
    return Result("kill_primary", "Hard-kill the primary", "pass", detail,
                  {"outage_s": outage, "promoted": promoted,
                   "keys_before": before, "keys_after": after})


@test("kill_replica", "Kill a replica", 2, disruptive=True,
      topologies=["replication", "sentinel", "cluster", "enterprise"],
      describe="Deletes a replica pod. The primary should keep serving writes "
               "throughout, and the replica should rejoin and resync.")
def t_kill_replica(ctx) -> Result:
    c, t = ctx.client, ctx.target
    if not t.replicas:
        return Result("kill_replica", "Kill a replica", "skip", "no replicas")
    pod = t.replicas[0]
    ctx.log(f"  deleting replica {pod}")
    failures = 0
    ocp.run(ctx.kubeconfig, ["delete", "pod", pod, "-n", t.namespace,
                             "--grace-period=0", "--force"],
            check=False, timeout=120, log=ctx.log)
    for _ in range(20):
        rc, out = c.cli(t, "SET", f"{SCRATCH}:dur", "1", timeout=15)
        if not out.strip().endswith("OK"):
            failures += 1
        time.sleep(1)

    rejoined = False
    for _ in range(30):
        if _role_of(ctx.kubeconfig, t.namespace, pod, t.password) in ("slave", "replica"):
            rejoined = True
            break
        time.sleep(5)

    detail = (f"{failures}/20 writes failed while {pod} was down; "
              f"replica {'rejoined' if rejoined else 'did NOT rejoin within 150s'}")
    if failures:
        return Result("kill_replica", "Kill a replica", "fail",
                      "losing a REPLICA should never interrupt writes | " + detail)
    return Result("kill_replica", "Kill a replica", "pass" if rejoined else "warn",
                  detail, {"write_failures": failures, "rejoined": rejoined})


@test("restart_cycle", "Stop and start (scale 0 -> 1)", 2, disruptive=True,
      describe="A clean maintenance stop: scales the workload to zero, then back. "
               "Verifies the PVC reattaches and the data is still there.")
def t_restart_cycle(ctx) -> Result:
    t = ctx.target
    if t.kind != "community":
        return Result("restart_cycle", "Stop and start (scale 0 -> 1)", "skip",
                      "operator-managed releases are scaled by their operator, not directly")
    tag = _seed(ctx)
    before = _count(ctx, tag)
    target = f"{t.workload}/{t.name}"
    replicas = ocp.jsonpath(ctx.kubeconfig, ["get", t.workload, t.name, "-n", t.namespace],
                            "{.spec.replicas}") or "1"
    ctx.log(f"  scaling {target} to 0")
    ocp.run(ctx.kubeconfig, ["scale", target, "-n", t.namespace, "--replicas=0"],
            timeout=90, log=ctx.log)
    time.sleep(10)
    ctx.log(f"  scaling {target} back to {replicas}")
    ocp.run(ctx.kubeconfig, ["scale", target, "-n", t.namespace,
                             f"--replicas={replicas}"], timeout=90, log=ctx.log)
    ocp.run(ctx.kubeconfig, ["rollout", "status", target, "-n", t.namespace,
                             "--timeout=600s"], check=False, timeout=660, log=ctx.log)
    outage = _write_outage(ctx)
    after = _count(ctx, tag)
    detail = f"stop/start outage {outage}s | keys before {before}, after {after}"
    if after < before:
        return Result("restart_cycle", "Stop and start (scale 0 -> 1)", "fail",
                      f"DATA LOSS: {before - after} keys missing | " + detail)
    return Result("restart_cycle", "Stop and start (scale 0 -> 1)", "pass", detail,
                  {"outage_s": outage})


# ============================================ TIER 3 - load

@test("benchmark", "Throughput and latency baseline", 3,
      describe="redis-benchmark for SET and GET: operations per second and the "
               "p50/p99 latency, as a baseline to compare against later.")
def t_benchmark(ctx) -> Result:
    c, t = ctx.client, ctx.target
    auth = ["-a", t.password] if t.password else []
    cmd = ["exec", "-n", c.ns, c.pod, "--", "redis-benchmark",
           "-h", t.host, "-p", str(t.port), *auth,
           "-t", "set,get", "-n", "20000", "-c", "20", "-q"]
    p = ocp.run(ctx.kubeconfig, cmd, check=False, timeout=300)
    out = (p.stdout or "") + (p.stderr or "")
    if not out.strip():
        return Result("benchmark", "Throughput and latency baseline", "skip",
                      "redis-benchmark produced no output")
    metrics = {}
    for line in out.splitlines():
        m = re.match(r"(\w+)[^:]*:\s*([\d.]+) requests per second", line.strip())
        if m:
            metrics[m.group(1)] = float(m.group(2))
    for line in out.splitlines():
        ctx.log("    " + line)
    return Result("benchmark", "Throughput and latency baseline", "pass",
                  ", ".join(f"{k} {v:,.0f} ops/s" for k, v in metrics.items()) or out[:120],
                  metrics)


@test("eviction", "Eviction policy behaves as configured", 3, disruptive=True,
      describe="Fills past maxmemory and checks the policy does what you chose: "
               "allkeys-lru evicts silently, noeviction REJECTS writes. Getting "
               "this wrong is a production incident on the day the cache fills.")
def t_eviction(ctx) -> Result:
    c, t = ctx.client, ctx.target
    _, out = c.cli(t, "CONFIG", "GET", "maxmemory", "maxmemory-policy")
    lines = [l for l in out.splitlines() if l.strip()]
    cfg = dict(zip(lines[::2], lines[1::2]))
    mm = int(cfg.get("maxmemory", "0") or 0)
    policy = cfg.get("maxmemory-policy", "?")
    if mm == 0:
        return Result("eviction", "Eviction policy behaves as configured", "skip",
                      "maxmemory is unlimited -- nothing to test, but note Redis will "
                      "grow until the container is OOMKilled")

    _, before = c.cli(t, "INFO", "stats")
    ev_before = int((re.search(r"evicted_keys:(\d+)", before) or [0, 0])[1])
    ctx.log(f"  maxmemory={mm} policy={policy}; filling with 1MB values")
    rejected = False
    for i in range(int(mm / 1_000_000) + 20):
        rc, o = c.cli(t, "EVAL",
                      "return redis.call('SET', KEYS[1], string.rep('x', 1000000))",
                      "1", f"{SCRATCH}:fill:{i}", timeout=30)
        if "OOM" in o:
            rejected = True
            break
    _, after = c.cli(t, "INFO", "stats")
    ev_after = int((re.search(r"evicted_keys:(\d+)", after) or [0, 0])[1])
    evicted = ev_after - ev_before
    c.cli(t, "EVAL",
          "local ks=redis.call('KEYS', KEYS[1]..':*') for i,k in ipairs(ks) do "
          "redis.call('DEL', k) end return #ks", "1", f"{SCRATCH}:fill", timeout=120)

    detail = f"policy={policy}, evicted {evicted} keys, writes {'REJECTED (OOM)' if rejected else 'kept succeeding'}"
    if policy == "noeviction":
        ok = rejected and evicted == 0
        return Result("eviction", "Eviction policy behaves as configured",
                      "pass" if ok else "fail",
                      detail + " -- expected: writes rejected, nothing evicted "
                      "(datastore behaviour)", {"evicted": evicted, "rejected": rejected})
    ok = evicted > 0 and not rejected
    return Result("eviction", "Eviction policy behaves as configured",
                  "pass" if ok else "warn",
                  detail + " -- expected: old keys evicted, writes keep succeeding "
                  "(cache behaviour). Your application must treat every read as a "
                  "possible miss.", {"evicted": evicted, "rejected": rejected})


# ---------------------------------------------------------------- runner

@dataclass
class Ctx:
    kubeconfig: str
    target: Target
    client: Client
    log: Callable


def run_suite(job, kubeconfig: str, kind: str, namespace: str, name: str,
              test_ids: list[str], client_ns: str = "",
              topology: str = "", password: str = "",
              client_image: str = "") -> dict:
    job.log(f"Testing {kind} release {namespace}/{name}"
            + (f" ({topology})" if topology else ""))
    job.step(1, 4, "Resolving the target")
    t = resolve_target(kubeconfig, kind, namespace, name, log=job.log,
                       topology=topology, password=password)
    job.log(f"  topology : {t.topology}")
    job.log(f"  endpoint : {t.host}:{t.port}")
    job.log(f"  pods     : {', '.join(t.pods) or 'none'}")
    if t.primary:
        job.log(f"  primary  : {t.primary}   replicas: {', '.join(t.replicas) or 'none'}")
    if t.allowed_ns:
        job.log(f"  policy   : reachable only from {', '.join(t.allowed_ns)}")

    selected = [x for x in TESTS if x["id"] in test_ids
                and (not x["topologies"] or t.topology in x["topologies"])]
    disruptive = [x for x in selected if x["disruptive"]]

    job.step(2, 4, "Safety check")
    _, dbsize = "", ""
    if not client_ns:
        client_ns = t.allowed_ns[0] if t.allowed_ns else namespace
        if t.allowed_ns:
            job.log(f"  running the test client from '{client_ns}' so the real "
                    "network path (including the NetworkPolicy) is exercised")
    # reuse the release's own image unless told otherwise: it is demonstrably
    # pullable here, and redis-cli then matches the server version
    image = client_image or t.image or FALLBACK_CLIENT_IMAGE
    if not client_image and t.image:
        job.log(f"  test client will reuse the release's image ({t.image})")
    client = Client(kubeconfig, client_ns, job.log, image=image)
    client.start()
    try:
        ctx = Ctx(kubeconfig, t, client, job.log)

        # fail fast and explain, rather than letting every test time out
        ok, why = client.reachable(t)
        if not ok:
            hint = ""
            if t.allowed_ns and client_ns not in t.allowed_ns:
                hint = (f"\n  A NetworkPolicy restricts this release to "
                        f"{', '.join(t.allowed_ns)}, and the test client is running in "
                        f"'{client_ns}'. That is the policy working correctly -- rerun "
                        f"with the client in one of the allowed namespaces.")
            raise RuntimeError(
                f"the test client in '{client_ns}' cannot reach {t.host}:{t.port} "
                f"({why}){hint}")
        job.log(f"  reachable from '{client_ns}'")

        rc, out = client.cli(t, "DBSIZE")
        existing = int(re.search(r"(\d+)", out).group(1)) if re.search(r"\d", out) else 0
        job.log(f"  database currently holds {existing} keys")
        if disruptive and existing > 0:
            job.log("")
            job.log(f"  !! {existing} keys already exist. Disruptive tests kill pods.")
            job.log("  !! Proceeding because you confirmed, but this is NOT a test-only "
                    "database.")

        job.step(3, 4, f"Running {len(selected)} test(s)")
        results: list[Result] = []
        for i, spec in enumerate(selected, 1):
            job.log("")
            job.log(f"--- [{i}/{len(selected)}] {spec['name']}"
                    + ("  (DISRUPTIVE)" if spec["disruptive"] else ""))
            if spec["expects"] and t.topology in spec["expects"]:
                job.log(f"    expected for {t.topology}: {spec['expects'][t.topology]}")
            try:
                r = spec["fn"](ctx)
            except Exception as exc:                          # noqa: BLE001
                r = Result(spec["id"], spec["name"], "fail", f"test raised: {exc}")
            results.append(r)
            mark = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skip": "SKIP"}[r.status]
            job.log(f"    [{mark}] {r.detail}")

        job.step(4, 4, "Cleaning up scratch keys")
        client.cli(t, "EVAL",
                   "local ks=redis.call('KEYS', ARGV[1]) for i,k in ipairs(ks) do "
                   "redis.call('DEL', k) end return #ks", "0", f"{SCRATCH}*", timeout=120)
    finally:
        client.stop()

    counts = {s: sum(1 for r in results if r.status == s)
              for s in ("pass", "fail", "warn", "skip")}
    job.log("")
    job.log("=" * 60)
    job.log(f"  {counts['pass']} passed, {counts['fail']} failed, "
            f"{counts['warn']} warnings, {counts['skip']} skipped")

    report = _report(t, results, counts)
    return {"kind": "test", "target": {"namespace": namespace, "name": name,
                                       "topology": t.topology, "host": t.host,
                                       "port": t.port},
            "counts": counts,
            "results": [r.__dict__ for r in results],
            "report": report,
            "failed": counts["fail"] > 0}


def _report(t: Target, results: list[Result], counts: dict) -> str:
    lines = [
        f"# Redis test report - {t.namespace}/{t.name}",
        "",
        f"| | |", "|---|---|",
        f"| Topology | {t.topology} |",
        f"| Endpoint | `{t.host}:{t.port}` |",
        f"| Pods | {', '.join(t.pods) or '-'} |",
        f"| Primary | {t.primary or '-'} |",
        f"| Replicas | {', '.join(t.replicas) or '-'} |",
        f"| Run at | {time.strftime('%Y-%m-%d %H:%M:%S %Z')} |",
        f"| Result | **{counts['pass']} passed, {counts['fail']} failed, "
        f"{counts['warn']} warnings, {counts['skip']} skipped** |",
        "", "## Results", "",
        "| Test | Status | Detail |", "|---|---|---|",
    ]
    for r in results:
        lines.append(f"| {r.name} | {r.status.upper()} | {r.detail} |")

    timings = {r.name: r.metrics["outage_s"] for r in results
               if "outage_s" in r.metrics}
    if timings:
        lines += ["", "## Measured write outage", "",
                  "Give these to the application team - they size the client's "
                  "retry and timeout settings.", "",
                  "| Event | Writes unavailable for |", "|---|---|"]
        for k, v in timings.items():
            lines.append(f"| {k} | {v}s |")
    return "\n".join(lines)
