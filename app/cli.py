"""A redis-cli console that runs inside the cluster.

The safety model asks REDIS what a command is, rather than relying on a list
that drifts with every release: `COMMAND INFO` reports `admin`, `write` and
`readonly` flags plus ACL categories, and that is authoritative for the exact
server version you are talking to.

Three tiers, each needing a deliberate unlock:
  read   GET, SCAN, INFO, TTL, MEMORY USAGE...   always allowed
  write  SET, DEL, EXPIRE, RENAME...             needs write mode
  admin  CONFIG SET, ACL SETUSER, CLIENT KILL... needs admin mode

Destructive commands are refused outright in every mode -- there is no unlock.
Wiping a keyspace or repointing replication from a browser tab, on an app this
has no login of its own, is not a thing to put behind a checkbox.

That list is curated rather than derived, because Redis's own flags do not draw
the line in the right place: on this cluster, COMMAND INFO reports eight
commands as both `write` and `@dangerous`, and three of them (SORT, RESTORE,
PFDEBUG) are ordinary data operations that a reader legitimately needs. Flags
decide the tier; this list decides what is off the table entirely.
"""
from __future__ import annotations

import shlex
from typing import Any

from . import ocp

# Refused in every mode. Each one either destroys data wholesale, stops the
# server, or rewrites the replication topology -- the three things this console
# must never be the route to. Day-2 covers the legitimate versions of these
# with a confirmation step and a known-good target.
NEVER = {
    "flushall":   "wipes every key in every database",
    "flushdb":    "wipes every key in the current database",
    "shutdown":   "stops the server",
    "debug":      "can deliberately crash or stall the server (DEBUG SEGFAULT, DEBUG SLEEP)",
    "replicaof":  "repoints replication and discards this server's data",
    "slaveof":    "repoints replication and discards this server's data",
    "failover":   "forces a failover outside the operator's control",
    "swapdb":     "swaps two whole databases under live clients",
    "migrate":    "moves keys out to another server",
}

# Subcommands of container commands that are equally off the table.
NEVER_SUB = {
    ("cluster", "reset"):   "destroys this node's cluster membership",
    ("cluster", "forget"):  "evicts a node from the cluster",
    ("cluster", "failover"): "forces a failover outside the operator's control",
    ("script", "flush"):    "drops every cached Lua script",
    ("function", "flush"):  "drops every registered function",
}

# Container commands report no flags of their own; these subcommands are reads.
SAFE_SUBCOMMANDS = {
    "config": {"get"},
    "acl": {"list", "whoami", "cat", "getuser", "users"},
    "client": {"list", "info", "id", "getname", "no-evict", "no-touch"},
    "cluster": {"info", "nodes", "slots", "shards", "myid", "countkeysinslot"},
    "command": {"count", "docs", "info", "list", "getkeys"},
    "memory": {"usage", "stats", "doctor"},
    "latency": {"latest", "history", "doctor"},
    "slowlog": {"get", "len"},
    "object": {"encoding", "freq", "idletime", "refcount"},
    "xinfo": {"stream", "groups", "consumers"},
    "pubsub": {"channels", "numsub", "numpat", "shardchannels"},
    "script": {"exists"},
    "function": {"list", "stats", "dump"},
}


def classify(kubeconfig: str, ns: str, pod: str, auth: list[str],
             command: str) -> dict[str, Any]:
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return {"tier": "invalid", "reason": f"could not parse: {exc}"}
    if not parts:
        return {"tier": "invalid", "reason": "empty command"}

    base = parts[0].lower()
    sub = parts[1].lower() if len(parts) > 1 else ""

    if base in NEVER:
        return {"tier": "never", "base": base,
                "reason": f"{base.upper()} is refused from this console in every mode: it "
                          f"{NEVER[base]}. There is no unlock for this. If you genuinely "
                          f"need it, use the Day-2 screen or an oc rsh session, where it "
                          f"is a deliberate act against a named target."}
    if (base, sub) in NEVER_SUB:
        return {"tier": "never", "base": f"{base} {sub}",
                "reason": f"{base.upper()} {sub.upper()} is refused from this console in "
                          f"every mode: it {NEVER_SUB[(base, sub)]}. There is no unlock "
                          f"for this."}

    p = ocp.run(kubeconfig, ["exec", "-n", ns, pod, "--", "redis-cli", *auth,
                             "COMMAND", "INFO", base], check=False, timeout=30)
    out = (p.stdout or "").lower()
    if not out.strip():
        return {"tier": "unknown", "base": base,
                "reason": f"Redis does not recognise '{base}'."}

    flags = set(out.split())

    if base in SAFE_SUBCOMMANDS:
        if sub in SAFE_SUBCOMMANDS[base]:
            return {"tier": "read", "base": f"{base} {sub}"}
        label = f"{base} {sub}".strip().upper()
        return {"tier": "admin", "base": f"{base} {sub}".strip(),
                "reason": f"'{label}' can change server state."}

    if "admin" in flags:
        return {"tier": "admin", "base": base}
    if "write" in flags:
        return {"tier": "write", "base": base}

    tier = {"tier": "read", "base": base}
    if "@dangerous" in flags and "readonly" in flags:
        tier["warn"] = (f"{base.upper()} is O(N) over the whole keyspace and Redis is "
                        "single-threaded, so it blocks every other client while it runs. "
                        "Use SCAN on anything but a toy dataset.")
    return tier


def _pod_names(kubeconfig: str, ns: str, name: str) -> list[str]:
    raw = ocp.jsonpath(kubeconfig, ["get", "pods", "-n", ns, "-l", f"app={name}",
                                    "--field-selector=status.phase=Running"],
                       '{range .items[*]}{.metadata.name}{" "}{end}') or ""
    return sorted(raw.split())


def _password(kubeconfig: str, ns: str, name: str) -> str:
    import base64
    for key in ("redis-password", "password"):
        raw = ocp.run(kubeconfig, ["get", "secret", f"{name}-auth", "-n", ns, "-o",
                                   f"jsonpath={{.data.{key}}}"],
                      check=False, timeout=30).stdout.strip()
        if raw:
            try:
                return base64.b64decode(raw).decode()
            except Exception:
                continue
    return ""


def _auth(username: str, password: str) -> list[str]:
    if username and username != "default":
        return ["--user", username, "--pass", password, "--no-auth-warning"]
    if password:
        return ["-a", password, "--no-auth-warning"]
    return []


def pods(kubeconfig: str, ns: str, name: str) -> dict[str, Any]:
    """Pod list with each one's replication role, so the console can warn before
    you aim a write at a replica instead of after Redis rejects it."""
    names = _pod_names(kubeconfig, ns, name)
    if not names:
        return {"pods": [], "error": f"no running pods for '{name}' in '{ns}'"}
    auth = _auth("", _password(kubeconfig, ns, name))
    out = []
    for pod in names:
        p = ocp.run(kubeconfig, ["exec", "-n", ns, pod, "--", "redis-cli", *auth,
                                 "INFO", "replication"], check=False, timeout=20)
        role = ""
        for line in (p.stdout or "").splitlines():
            if line.startswith("role:"):
                # Redis still says "slave" on the wire; say replica to the operator
                role = "primary" if line.strip() == "role:master" else "replica"
                break
        out.append({"name": pod, "role": role})
    out.sort(key=lambda d: (d["role"] != "primary", d["name"]))
    return {"pods": out}


def execute(kubeconfig: str, ns: str, name: str, command: str, mode: str = "read",
            pod: str = "", username: str = "", password: str = "") -> dict[str, Any]:
    names = _pod_names(kubeconfig, ns, name)
    if not names:
        return {"error": f"no running pods found for '{name}' in '{ns}'"}
    target = pod if pod in names else names[0]

    if not password:
        password = _password(kubeconfig, ns, name)
    auth = _auth(username, password)

    verdict = classify(kubeconfig, ns, target, auth, command)
    tier = verdict["tier"]

    allowed = {"read": {"read"},
               "write": {"read", "write"},
               "admin": {"read", "write", "admin"}}.get(mode, {"read"})

    if tier in ("invalid", "unknown", "never"):
        return {"pod": target, "tier": tier, "blocked": True,
                "output": verdict.get("reason", "refused")}
    if tier not in allowed:
        article = "an" if tier == "admin" else "a"
        return {"pod": target, "tier": tier, "blocked": True,
                "output": f"'{verdict['base'].upper()}' is {article} {tier} command. "
                          f"Switch the console to {tier} mode to run it."}

    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return {"pod": target, "blocked": True, "output": str(exc)}

    p = ocp.run(kubeconfig, ["exec", "-n", ns, target, "--", "redis-cli", *auth, *parts],
                check=False, timeout=60)
    body = ((p.stdout or "") + (p.stderr or "")).rstrip()
    return {"pod": target, "tier": tier, "blocked": False,
            "warn": verdict.get("warn", ""), "output": body or "(empty)",
            "username": username or "default"}
