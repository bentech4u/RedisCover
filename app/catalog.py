"""Version catalogue.

Community versions are a curated static list (plus a custom-image escape hatch).
Each entry declares `features`, which the UI uses to decide WHICH FIELDS TO SHOW —
that is the "fill the details based on the version" behaviour.

Enterprise versions are not listed here: they are read live from the cluster's
PackageManifest, because what you can install depends on the catalog sources
that particular cluster subscribes to.
"""
from __future__ import annotations

COMMUNITY_VERSIONS = [
    {
        "id": "redis-8.2",
        "label": "Redis 8.2 (alpine)",
        "family": "redis",
        "version": "8.2",
        "image": "docker.io/redis:8.2-alpine",
        "notes": "Current stable. AGPL/RSAL tri-licence.",
        "features": ["multipart_aof", "acl", "maxmemory_clients", "io_threads"],
        "defaults": {"maxmemory": "512mb", "memory_limit": "1Gi", "cpu_limit": "1"},
    },
    {
        "id": "redis-8.0",
        "label": "Redis 8.0 (alpine)",
        "family": "redis",
        "version": "8.0",
        "image": "docker.io/redis:8.0-alpine",
        "notes": "First release after the licence change back to AGPL.",
        "features": ["multipart_aof", "acl", "maxmemory_clients", "io_threads"],
        "defaults": {"maxmemory": "512mb", "memory_limit": "1Gi", "cpu_limit": "1"},
    },
    {
        "id": "redis-7.4",
        "label": "Redis 7.4 (alpine)",
        "family": "redis",
        "version": "7.4",
        "image": "docker.io/redis:7.4-alpine",
        "notes": "Widely deployed. Proven on OpenShift with arbitrary UIDs.",
        "features": ["multipart_aof", "acl", "io_threads"],
        "defaults": {"maxmemory": "256mb", "memory_limit": "512Mi", "cpu_limit": "500m"},
    },
    {
        "id": "redis-7.2",
        "label": "Redis 7.2 (alpine)",
        "family": "redis",
        "version": "7.2",
        "image": "docker.io/redis:7.2-alpine",
        "notes": "Long-lived 7.x line.",
        "features": ["multipart_aof", "acl", "io_threads"],
        "defaults": {"maxmemory": "256mb", "memory_limit": "512Mi", "cpu_limit": "500m"},
    },
    {
        "id": "redis-6.2",
        "label": "Redis 6.2 (alpine)",
        "family": "redis",
        "version": "6.2",
        "image": "docker.io/redis:6.2-alpine",
        "notes": "Legacy. Single-file AOF, no multi-part rewrite.",
        "features": ["acl"],
        "defaults": {"maxmemory": "256mb", "memory_limit": "512Mi", "cpu_limit": "500m"},
    },
    {
        "id": "valkey-8",
        "label": "Valkey 8 (alpine)",
        "family": "valkey",
        "version": "8",
        "image": "docker.io/valkey/valkey:8-alpine",
        "notes": "BSD-licensed Redis fork (Linux Foundation). Drop-in replacement.",
        "features": ["multipart_aof", "acl", "maxmemory_clients", "io_threads"],
        "defaults": {"maxmemory": "512mb", "memory_limit": "1Gi", "cpu_limit": "1"},
    },
]

EVICTION_POLICIES = [
    ("noeviction", "noeviction - reject writes when full (datastore)"),
    ("allkeys-lru", "allkeys-lru - evict least-recently-used (cache)"),
    ("allkeys-lfu", "allkeys-lfu - evict least-frequently-used (cache)"),
    ("volatile-lru", "volatile-lru - evict LRU among keys with a TTL"),
    ("volatile-ttl", "volatile-ttl - evict shortest TTL first"),
    ("allkeys-random", "allkeys-random - evict at random"),
]

PERSISTENCE_MODES = [
    ("aof", "AOF - append-only log, <=1s loss (durable)"),
    ("rdb", "RDB - periodic snapshots only"),
    ("both", "AOF + RDB - belt and braces"),
    ("none", "None - pure in-memory cache, nothing on disk"),
]

REDB_EVICTION = [
    "noeviction", "allkeys-lru", "allkeys-lfu", "allkeys-random",
    "volatile-lru", "volatile-lfu", "volatile-ttl", "volatile-random",
]

# Enum spellings differ across operator releases; the app verifies against
# `oc explain` at deploy time and reports a clear error rather than guessing.
REDB_PERSISTENCE = [
    "disabled",
    "aofEverySecond",
    "aofEveryWrite",
    "snapshotEvery1Hour",
    "snapshotEvery6Hour",
    "snapshotEvery12Hour",
]


def community_version(vid: str) -> dict | None:
    for v in COMMUNITY_VERSIONS:
        if v["id"] == vid:
            return v
    return None


# ---------------------------------------------------------------- Opstree

OPSTREE_PACKAGE = "redis-operator"
OPSTREE_GROUP = "redis.redis.opstreelabs.in"

OPSTREE_TOPOLOGIES = [
    {
        "id": "standalone",
        "kind": "Redis",
        "label": "Standalone - one pod",
        "detail": "A single Redis. Restarted by Kubernetes if it dies (~30s). "
                  "No failover, no sharding. Any Redis client works.",
        "min_size": 1, "size_label": None, "ha": False, "client_aware": None,
    },
    {
        "id": "replication",
        "kind": "RedisReplication",
        "label": "Replication - primary + replicas",
        "detail": "One primary, N-1 replicas kept in sync. Read scaling and a warm "
                  "standby. On its own it does NOT fail over -- pair it with Sentinel.",
        "min_size": 3, "size_label": "Pods (primary + replicas)",
        "ha": False, "client_aware": None,
    },
    {
        "id": "sentinel",
        "kind": "RedisSentinel",
        "label": "Sentinel - replication + automatic failover",
        "detail": "Sentinel pods watch the primary and elect a replica when it fails. "
                  "Requires an existing RedisReplication to monitor.",
        "min_size": 3, "size_label": "Sentinel pods (odd number, for quorum)",
        "ha": True, "client_aware": "Sentinel-aware",
    },
    {
        "id": "cluster",
        "kind": "RedisCluster",
        "label": "Cluster - sharded across primaries",
        "detail": "Data split across N primaries by hash slot, each with a follower. "
                  "Horizontal scale plus HA. No cross-slot multi-key operations, "
                  "and only database 0.",
        "min_size": 3, "size_label": "Leader shards (each gets a follower)",
        "ha": True, "client_aware": "Cluster-aware",
    },
]

OPSTREE_VERSIONS = [
    # Verified against quay.io/opstree/redis. Opstree does NOT publish a tag for
    # every upstream Redis release, so these are real tags, not guesses -- an
    # invented tag gives you ImagePullBackOff with "manifest unknown".
    {"id": "v8.8", "label": "Redis 8.8", "image": "quay.io/opstree/redis:v8.8.2"},
    {"id": "v8.6", "label": "Redis 8.6", "image": "quay.io/opstree/redis:v8.6.6"},
    {"id": "v8.2", "label": "Redis 8.2", "image": "quay.io/opstree/redis:v8.2.9"},
    {"id": "v7.2", "label": "Redis 7.2", "image": "quay.io/opstree/redis:v7.2.3"},
    {"id": "v7.0", "label": "Redis 7.0", "image": "quay.io/opstree/redis:v7.0.12"},
    {"id": "v6.2", "label": "Redis 6.2 (legacy)", "image": "quay.io/opstree/redis:v6.2.14"},
]


def opstree_topology(tid: str) -> dict | None:
    for t in OPSTREE_TOPOLOGIES:
        if t["id"] == tid:
            return t
    return None


# ---------------------------------------------------------------- registry mirror

def apply_mirror(image: str, mirror: str, mode: str = "replace") -> str:
    """Rewrite an image reference through an internal mirror.

    Two conventions, because registries disagree:

      replace   docker.io/redis:8.2-alpine  ->  mirror.corp/redis:8.2-alpine
                the mirror hosts the repository at its own root

      prefix    docker.io/redis:8.2-alpine  ->  mirror.corp/docker.io/redis:8.2-alpine
                the mirror namespaces by upstream registry (Harbor proxy caches
                and Artifactory remote repos usually work this way)

    On OpenShift the cluster-level mechanism is ImageDigestMirrorSet, which
    rewrites transparently and is preferable. This is for clusters that do not
    have one configured.
    """
    if not mirror or not image:
        return image
    mirror = mirror.rstrip("/")

    # A registry is only present when there is a "/" AND the first segment
    # looks like a host. Without the "/" check, the tag's colon in
    # "redis:7.4-alpine" reads as a host:port.
    first, _, rest = image.partition("/")
    has_registry = bool(rest) and ("." in first or ":" in first
                                   or first == "localhost")
    registry = first if has_registry else "docker.io"
    path = rest if has_registry else image

    if mode == "prefix":
        return f"{mirror}/{registry}/{path}"
    return f"{mirror}/{path}"


# ---------------------------------------------------------------- redis ACL

# Verified against redis-server 8.2: a `user` line in redis.conf survives a
# restart, and the NOPERM errors below are real refusals, not warnings.
ACL_PRESETS = {
    "readonly": {
        "label": "Read-only",
        "rules": "+@read +@connection -@dangerous",
        "detail": "GET, MGET, EXISTS, SCAN within its keys. Cannot write, cannot "
                  "FLUSH, cannot CONFIG.",
    },
    "readwrite": {
        "label": "Read and write (typical application)",
        "rules": "+@read +@write +@connection +@transaction -@dangerous -@admin",
        "detail": "Everything an application needs on its own keys. Cannot FLUSHALL, "
                  "CONFIG SET, SHUTDOWN, DEBUG or read other prefixes.",
    },
    "full": {
        "label": "Full access to its own keys",
        "rules": "+@all -@admin -@dangerous",
        "detail": "Scripting and pubsub included, still scoped to its key pattern and "
                  "still unable to administer the server.",
    },
    "admin": {
        "label": "Administrator (same as default)",
        "rules": "+@all",
        "detail": "Every command on every key, including FLUSHALL and SHUTDOWN. "
                  "Only for an operator account.",
    },
}


def acl_line(username: str, password: str, key_pattern: str = "*",
             permissions: str = "readwrite", channels: str = "",
             enabled: bool = True) -> str:
    """One redis.conf `user` directive.

    Key patterns need the ~ prefix; several are space separated. An empty
    channel pattern becomes `resetchannels`, which denies pubsub entirely --
    safer than the `&*` that would otherwise be inherited.
    """
    rules = ACL_PRESETS.get(permissions, ACL_PRESETS["readwrite"])["rules"]
    keys = " ".join(f"~{k.strip()}" for k in key_pattern.split() if k.strip()) or "~*"
    chan = (" ".join(f"&{c.strip()}" for c in channels.split() if c.strip())
            if channels else "resetchannels")
    state = "on" if enabled else "off"
    return f"user {username} {state} >{password} {keys} {chan} {rules}"
