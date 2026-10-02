"""Generates the document you hand to the application team.

Everything here is read from the running release -- endpoints, credentials
location, cache size, eviction policy, the NetworkPolicy allow list -- because
a handover written from a template is how an app team ends up pointing at an
endpoint that was renamed three deployments ago.

It states the topology's failure behaviour plainly. A document that lists a
hostname and stops leaves the reader to assume the cache is always there, and
that assumption is what turns a 40-second pod restart into an application
outage.
"""
from __future__ import annotations

import datetime
from typing import Any, Optional

from . import ocp

# What a client must set for a cache. These are not arbitrary: a cache call that
# can block longer than the request it is accelerating has stopped being an
# optimisation, and an unbounded pool turns one slow Redis into thread
# exhaustion across the whole application.
TIMEOUTS = [
    ("Connect timeout", "1s",
     "Redis is one network hop away inside the cluster. If a connection has not "
     "established in a second, the pod is gone, not busy."),
    ("Command / read timeout", "250ms - 1s",
     "Redis serves from RAM in microseconds. Anything approaching a second means "
     "it is unreachable or blocked, and you want the database path instead."),
    ("Retries per call", "1, then give up",
     "The fallback IS the retry. Retrying a dead endpoint several times just "
     "multiplies the delay before the database read starts."),
    ("Pool max-wait", "<= command timeout",
     "Without this, threads queue for a connection to a dead server and the "
     "timeout you configured never applies."),
    ("Circuit breaker", "open after ~5 consecutive failures, retry after 10-30s",
     "Stops every request paying the timeout while Redis is known to be down."),
]

ERRORS = [
    ("Connection refused / timeout", "The pod is gone or restarting.",
     "Treat as a cache miss. Read the database."),
    ("READONLY You can't write against a read only replica",
     "A write reached a replica.",
     "Check you are using the write endpoint. Then treat as a miss."),
    ("MASTERDOWN Link with MASTER is down",
     "A replica lost its primary and is serving stale data.",
     "Treat as a miss."),
    ("OOM command not allowed when used memory > 'maxmemory'",
     "The cache is full and Redis could not free enough memory to accept the "
     "write -- either the policy does not evict, or nothing was eligible.",
     "Writes to the cache fail; reads still work. Alert the platform team."),
    ("NOAUTH / WRONGPASS", "Credentials wrong or rotated.",
     "Fail loudly -- this is a config error, not a cache miss. Do NOT silently "
     "fall through, or you will never notice."),
    ("NOPERM this user has no permissions",
     "The ACL user is scoped more tightly than the command needs.",
     "Fail loudly. A config error, not a miss."),
    ("LOADING Redis is loading the dataset in memory",
     "The pod restarted and is replaying its AOF.",
     "Treat as a miss. Resolves on its own in seconds."),
]


def _fqdn(svc: str, ns: str, domain: str) -> str:
    return "{0}.{1}.svc.{2}".format(svc, ns, domain)


def _classify_services(services: list[dict]) -> dict[str, Optional[dict]]:
    """Which Service is the write endpoint, which is the read one.

    The write Service is the one pinned to a single pod -- it carries a
    statefulset.kubernetes.io/pod-name selector. Headless has no cluster IP.
    Whatever is left and has a cluster IP is the read endpoint.
    """
    write = read = headless = None
    for s in services:
        sel = s.get("selector") or ""
        if "statefulset.kubernetes.io/pod-name" in sel:
            write = s
        elif s.get("cluster_ip") in ("None", None, ""):
            headless = s
        else:
            read = s
    if write is None and read is not None:
        # standalone: one Service does both
        write = read
    return {"write": write, "read": read, "headless": headless}


def generate(kubeconfig: str, namespace: str, name: str, status: dict,
             client_namespace: str = "", kind: str = "community") -> dict[str, Any]:
    live = status.get("live") or {}
    services = status.get("services") or []
    policies = status.get("policies") or []
    res = status.get("resources") or {}
    pods = status.get("pods") or []

    domain = ocp.cluster_domain(kubeconfig, namespace)
    svc = _classify_services(services)
    port = (svc["write"] or svc["read"] or {}).get("ports", "6379").split(",")[0].strip()

    replicas = int(live.get("connected_slaves") or 0)
    topology = "replication" if replicas else "standalone"
    if (live.get("mode") or "") == "cluster":
        topology = "cluster"

    tls = ocp.run(kubeconfig, ["get", "statefulset", name, "-n", namespace, "-o",
                               "jsonpath={.spec.template.spec.containers[0].args}"],
                  check=False, timeout=30).stdout
    tls_on = "tls-port" in (tls or "")

    # Is the caller's namespace actually allowed through?
    allowed: list[str] = []
    for p in policies:
        allowed.extend(p.get("allows") or [])
    np_present = bool(policies)
    ns_ok = (not np_present) or (client_namespace and client_namespace in allowed)

    warnings: list[str] = []
    if client_namespace and np_present and not ns_ok:
        warnings.append(
            "'{0}' is NOT in the NetworkPolicy allow list ({1}). Connections from "
            "there will hang until they time out, which looks exactly like a slow "
            "Redis. Fix this before handing the document over.".format(
                client_namespace, ", ".join(allowed) or "nothing"))
    if not np_present:
        warnings.append(
            "No NetworkPolicy selects these pods, so every pod in the cluster can "
            "reach Redis on {0}.".format(port))
    if topology == "replication":
        warnings.append(
            "This topology has NO automatic failover. The document tells the app "
            "team to expect a write outage and to fall back to the database.")
    if not tls_on:
        warnings.append("TLS is not enabled; traffic is plaintext inside the cluster.")

    users = status.get("acl_users") or []
    secret = "{0}-auth".format(name)

    d = []
    a = d.append
    a("# Connecting to Redis: {0}/{1}".format(namespace, name))
    a("")
    a("Generated {0} from the running release. Every value below was read from "
      "the cluster, not from a template.".format(
          datetime.datetime.now().strftime("%Y-%m-%d %H:%M")))
    a("")
    a("---")
    a("")
    a("## 1. Endpoints")
    a("")
    a("| Purpose | Host | Port |")
    a("|---|---|---|")
    if svc["write"]:
        a("| **Writes** | `{0}` | {1} |".format(
            _fqdn(svc["write"]["name"], namespace, domain), port))
    if svc["read"] and svc["read"] is not svc["write"]:
        a("| **Reads** | `{0}` | {1} |".format(
            _fqdn(svc["read"]["name"], namespace, domain), port))
    if svc["headless"]:
        a("| Per-pod DNS (topology-aware clients only) | `{0}` | {1} |".format(
            _fqdn(svc["headless"]["name"], namespace, domain), port))
    a("")
    if topology == "replication" and svc["read"] and svc["read"] is not svc["write"]:
        a("The two endpoints are not interchangeable. The write endpoint resolves to "
          "**one pod** -- the primary. The read endpoint load-balances across all "
          "{0} pods, and replicas reject writes with `READONLY`.".format(len(pods)))
        a("")
        a("**If your client only accepts one host**, use the write endpoint for "
          "everything. It is correct for reads as well; you simply lose the read "
          "scaling, and you lose the ability to keep serving reads while the "
          "primary is down. See section 5.")
        a("")
    a("TLS: **{0}**.".format("enabled" if tls_on
                             else "not enabled - traffic is plaintext inside the cluster"))
    a("")
    a("## 2. Credentials")
    a("")
    a("The password is in a Secret. It is deliberately not printed in this "
      "document, so this file can be shared without leaking it.")
    a("")
    a("```bash")
    a("oc get secret {0} -n {1} -o jsonpath='{{.data.redis-password}}' | base64 -d".format(
        secret, namespace))
    a("```")
    a("")
    if users:
        a("Available users:")
        a("")
        a("| User | Keys | Commands |")
        a("|---|---|---|")
        for u in users:
            a("| `{0}` | `{1}` | `{2}` |".format(
                u.get("username", "?"), u.get("keys", "-"), u.get("commands", "-")))
        a("")
        a("Use a scoped user rather than `default` where one fits: Redis then "
          "enforces the limits itself, and a bug in your code cannot reach keys "
          "that are not yours.")
    else:
        a("Only the `default` user exists, which has full access to every key and "
          "command. Ask the platform team for a scoped ACL user if your application "
          "only needs a key prefix.")
    a("")
    a("## 3. What you are connecting to")
    a("")
    a("| | |")
    a("|---|---|")
    a("| Redis version | {0} |".format(live.get("version", "?")))
    a("| Topology | {0}{1} |".format(
        topology, " - 1 primary + {0} replica(s)".format(replicas) if replicas else ""))
    a("| Cache size (`maxmemory`) | {0} |".format(live.get("maxmemory", "?")))
    a("| Eviction policy | `{0}` |".format(live.get("maxmemory_policy", "?")))
    a("| Container memory limit | {0} |".format(res.get("memory_limit", "?")))
    a("| Persistence | {0} |".format(
        "AOF on" if live.get("aof_enabled") == "1" else "AOF off"))
    a("")
    pol = live.get("maxmemory_policy", "")
    if pol.startswith("allkeys"):
        a("`{0}` means **Redis will delete your keys without telling you** once the "
          "cache is full, including keys with no TTL. Never treat a value read from "
          "Redis as guaranteed to still be there, and never use it as the only copy "
          "of anything.".format(pol))
    elif pol.startswith("volatile"):
        a("`{0}` evicts only keys that carry a TTL. If you write keys without one, "
          "the cache fills with entries Redis cannot evict and writes start failing "
          "with `OOM`. **Set a TTL on everything.**".format(pol))
    elif pol == "noeviction":
        a("`noeviction` means that when the cache fills, **writes fail** with `OOM` "
          "rather than old keys being dropped. Reads keep working.")
    a("")
    a("## 4. Required client settings")
    a("")
    a("| Setting | Value | Why |")
    a("|---|---|---|")
    for setting, value, why in TIMEOUTS:
        a("| {0} | `{1}` | {2} |".format(setting, value, why))
    a("")
    a("These are not suggestions. An unbounded cache call is worse than no cache: "
      "it converts a Redis outage into an application outage by holding threads "
      "that would otherwise be serving the database path.")
    a("")
    a("## 5. What happens when Redis goes away")
    a("")
    if topology == "replication":
        a("**There is no automatic failover in this topology.** Nothing promotes a "
          "replica. Plan for the primary being unavailable.")
        a("")
        a("| Failure | Effect | Duration |")
        a("|---|---|---|")
        a("| Primary pod restarts | Writes fail. Reads via the read endpoint keep "
          "working. | ~30-60s, recovers on its own |")
        a("| Primary's node fails | Writes fail until the volume detaches and the "
          "pod reschedules. | Minutes |")
        a("| A replica restarts | No effect on writes; slightly less read capacity. "
          "| ~30-60s |")
        a("")
        a("The primary pod returns under the same name and the write endpoint "
          "follows it automatically, so the common case is self-healing. Your job "
          "is to survive the gap, not to page someone.")
    elif topology == "standalone":
        a("This is a single pod with no replicas. Any restart takes the whole cache "
          "away for ~30-60 seconds while the pod comes back and replays its AOF.")
    else:
        a("This topology promotes a replacement automatically; expect a short "
          "interruption rather than an open-ended outage.")
    a("")
    a("## 6. Errors you must handle")
    a("")
    a("| Error | Means | Do |")
    a("|---|---|---|")
    for err, means, do in ERRORS:
        a("| `{0}` | {1} | {2} |".format(err, means, do))
    a("")
    a("Note the split: connectivity and capacity problems are **cache misses** and "
      "should fall through silently. Authentication and permission problems are "
      "**configuration errors** and must be loud, or a typo in a password will look "
      "like a permanently slow application and nobody will know why.")
    a("")
    a("## 7. The contract")
    a("")
    a("1. **Redis holds a copy, never the only copy.** The database stays the system "
      "of record. Anything that exists only in Redis is lost on eviction or restart.")
    a("2. **Every cache failure falls through to the database.** Read on miss, "
      "populate, carry on.")
    a("3. **Write to the database first, then update or invalidate the cache.** If "
      "Redis is unreachable the invalidation is skipped, so on recovery it may "
      "briefly serve values the database has already changed.")
    a("4. **Put a TTL on every key.** It bounds how long that staleness can last and "
      "it is the only thing that does.")
    a("5. **The database must survive 100% of the load.** If it cannot cope without "
      "the cache, the fallback moves the outage rather than preventing it. This is "
      "a capacity question worth answering before go-live.")
    a("")
    if client_namespace:
        a("## 8. Network access")
        a("")
        if not np_present:
            a("No NetworkPolicy restricts these pods, so `{0}` can reach them.".format(
                client_namespace))
        elif ns_ok:
            a("`{0}` is in the NetworkPolicy allow list, so traffic is permitted.".format(
                client_namespace))
        else:
            a("**`{0}` is NOT currently allowed.** The NetworkPolicy "
              "`{1}` admits: {2}.".format(
                  client_namespace, policies[0].get("name", "?"),
                  ", ".join(allowed) or "nothing"))
            a("")
            a("A blocked connection does not get refused -- it hangs until your "
              "connect timeout fires, which looks identical to a slow Redis. Ask the "
              "platform team to add the namespace before you start testing.")
        a("")
    a("---")
    a("")
    a("Questions about capacity, failover or access go to the platform team. "
      "Questions about caching strategy, TTLs and the fallback path are yours.")

    return {"markdown": "\n".join(d), "warnings": warnings,
            "filename": "redis-handover-{0}-{1}.md".format(namespace, name)}
