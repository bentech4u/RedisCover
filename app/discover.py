"""Find Redis installations that already exist on the cluster.

Two sources:
  * things this app deployed  -> label app.kubernetes.io/managed-by=redis-deployer
  * anything else that looks like Redis -> image name, or a RedisEnterpriseCluster

PVCs are reported with their StorageClass reclaim policy, so the uninstall
screen can say exactly what a deletion would destroy rather than warning in
the abstract.
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import ocp

MANAGED_LABEL = "app.kubernetes.io/managed-by"
MANAGED_VALUE = "redis-deployer"
_SKIP_NS = re.compile(r"^(openshift|kube)-")
_REDIS_IMAGE = re.compile(r"(^|/)(redis|valkey)([:@/-]|$)", re.I)


def _json(kubeconfig: str, args: list[str], timeout: int = 90) -> dict:
    p = ocp.run(kubeconfig, [*args, "-o", "json"], check=False, timeout=timeout)
    if p.returncode != 0:
        return {}
    try:
        return json.loads(p.stdout)
    except Exception:
        return {}


def _storage_index(kubeconfig: str) -> tuple[dict[str, str], dict[str, list[dict]]]:
    reclaim: dict[str, str] = {}
    for item in _json(kubeconfig, ["get", "sc"], 60).get("items", []):
        reclaim[item["metadata"]["name"]] = item.get("reclaimPolicy", "Delete")

    by_ns: dict[str, list[dict]] = {}
    for item in _json(kubeconfig, ["get", "pvc", "-A"]).get("items", []):
        md, spec, st = item["metadata"], item["spec"], item.get("status", {})
        sc = spec.get("storageClassName") or ""
        by_ns.setdefault(md["namespace"], []).append({
            "name": md["name"],
            "size": (st.get("capacity") or {}).get("storage")
                    or spec.get("resources", {}).get("requests", {}).get("storage"),
            "storage_class": sc or "(default)",
            "reclaim": reclaim.get(sc, "?"),
            "phase": st.get("phase"),
        })
    return reclaim, by_ns


def _namespace_phases(kubeconfig: str) -> dict[str, str]:
    out = {}
    for item in _json(kubeconfig, ["get", "ns"], 60).get("items", []):
        out[item["metadata"]["name"]] = (item.get("status") or {}).get("phase", "")
    return out


def discover(kubeconfig: str) -> dict[str, Any]:
    _, pvcs_by_ns = _storage_index(kubeconfig)
    ns_phase = _namespace_phases(kubeconfig)
    releases: list[dict] = []
    enterprise_ns: set[str] = set()

    # ---------------- Redis Enterprise ----------------
    recs = _json(kubeconfig, ["get", "rec", "-A"], 60).get("items", [])
    dbs_by_ns: dict[str, list[dict]] = {}
    for item in _json(kubeconfig, ["get", "redb", "-A"], 60).get("items", []):
        md, st, spec = item["metadata"], item.get("status", {}), item["spec"]
        eps = st.get("internalEndpoints") or [{}]
        dbs_by_ns.setdefault(md["namespace"], []).append({
            "name": md["name"],
            "status": st.get("status"),
            "version": st.get("version"),
            # the real endpoint comes from status, not from what we asked for
            "port": eps[0].get("port") or spec.get("databasePort"),
            "host": eps[0].get("host"),
            "shards": (st.get("shardStatuses") or {}).get("active") or spec.get("shardCount"),
            "memory": spec.get("memorySize"),
            "eviction": spec.get("evictionPolicy"),
            "secret": f"redb-{md['name']}",
        })

    csv_by_ns: dict[str, str] = {}
    for item in _json(kubeconfig, ["get", "csv", "-A"], 90).get("items", []):
        name = item["metadata"]["name"]
        if "redis" in name.lower():
            csv_by_ns[item["metadata"]["namespace"]] = name

    for item in recs:
        md, spec, st = item["metadata"], item["spec"], item.get("status", {})
        ns = md["namespace"]
        enterprise_ns.add(ns)
        pods = _json(kubeconfig, ["get", "pods", "-n", ns], 60).get("items", [])
        lic = st.get("licenseStatus") or {}

        bits = [f"{spec.get('nodes', '?')} nodes"]
        if lic.get("shardsUsage"):
            bits.append(f"{lic['shardsUsage']} shards")
        if lic.get("licenseState"):
            exp = (lic.get("expirationDate") or "")[:10]
            trial = "trial" in (lic.get("features") or [])
            bits.append(f"licence {lic['licenseState']}"
                        + (" (TRIAL)" if trial else "")
                        + (f", expires {exp}" if exp else ""))
        if (st.get("persistenceStatus") or {}).get("succeeded"):
            bits.append(f"persistence {st['persistenceStatus']['succeeded']}")

        releases.append({
            "kind": "enterprise",
            "namespace": ns,
            "name": md["name"],
            # printer column: .spec.redisEnterpriseImageSpec.versionTag
            "version": (spec.get("redisEnterpriseImageSpec") or {}).get("versionTag", ""),
            "status": st.get("state") or "unknown",
            "detail": " | ".join(bits),
            "licence_expiry": (lic.get("expirationDate") or "")[:10],
            "licence_trial": "trial" in (lic.get("features") or []),
            "credentials_secret": st.get("clusterCredentialSecretName"),
            "databases": dbs_by_ns.get(ns, []),
            "operator_csv": csv_by_ns.get(ns),
            "pods": len(pods),
            "pvcs": pvcs_by_ns.get(ns, []),
            "managed": md.get("labels", {}).get(MANAGED_LABEL) == MANAGED_VALUE,
            "topology": "enterprise",
            "ns_phase": ns_phase.get(ns, ""),
            "deleting": bool(md.get("deletionTimestamp")) or ns_phase.get(ns) == "Terminating",
        })

    # ---------------- Community ----------------
    for kind in ("deployment", "statefulset"):
        for item in _json(kubeconfig, ["get", kind, "-A"]).get("items", []):
            md, spec = item["metadata"], item["spec"]
            ns, name = md["namespace"], md["name"]
            if ns in enterprise_ns:
                continue                                    # operator-owned
            if any(o.get("kind") == "RedisEnterpriseCluster"
                   for o in md.get("ownerReferences", [])):
                continue

            labels = md.get("labels", {}) or {}
            managed = labels.get(MANAGED_LABEL) == MANAGED_VALUE
            images = [c.get("image", "")
                      for c in spec["template"]["spec"].get("containers", [])]
            looks_redis = any(_REDIS_IMAGE.search(i.split("@")[0]) for i in images)

            if not managed and (not looks_redis or _SKIP_NS.match(ns)):
                continue

            st = item.get("status", {})
            sel = ",".join(f"{k}={v}" for k, v in
                           (spec.get("selector", {}).get("matchLabels") or {}).items())
            claims = [v["persistentVolumeClaim"]["claimName"]
                      for v in spec["template"]["spec"].get("volumes", [])
                      if "persistentVolumeClaim" in v]
            pvcs = [p for p in pvcs_by_ns.get(ns, []) if p["name"] in claims] or \
                   pvcs_by_ns.get(ns, [])

            releases.append({
                "kind": "community",
                "namespace": ns,
                "name": name,
                "workload": kind,
                "version": images[0] if images else "",
                "status": f"{st.get('readyReplicas', 0)}/{st.get('replicas', 0)} ready",
                "detail": sel,
                "databases": [],
                "pods": st.get("replicas", 0),
                "pvcs": pvcs,
                "managed": managed,
                "topology": labels.get("redis-deployer/topology", "standalone"),
                "ns_phase": ns_phase.get(ns, ""),
                "deleting": bool(md.get("deletionTimestamp"))
                            or ns_phase.get(ns) == "Terminating",
            })

    releases.sort(key=lambda r: (r["kind"] != "enterprise", r["namespace"], r["name"]))
    stuck = sorted(n for n, p in ns_phase.items()
                   if p == "Terminating" and n in {r["namespace"] for r in releases})
    return {"releases": releases,
            "namespaces": sorted({r["namespace"] for r in releases}),
            "terminating_namespaces": stuck}


# ---------------------------------------------------------------- leftovers

def leftovers(kubeconfig: str) -> dict[str, Any]:
    """Cluster-scoped debris an uninstall does not remove.

    CRDs are cluster-scoped, so deleting a namespace leaves them behind. A PV
    whose PVC is gone but whose reclaimPolicy is Retain stays 'Released' and
    keeps consuming real storage.
    """
    crds = []
    for item in _json(kubeconfig, ["get", "crd"], 90).get("items", []):
        name = item["metadata"]["name"]
        group = item["spec"]["group"]
        if not re.search(r"redis|valkey", name, re.I):
            continue
        kind = item["spec"]["names"]["kind"]
        versions = [v["name"] for v in item["spec"].get("versions", []) if v.get("served")]
        # is anything still using it?
        p = ocp.run(kubeconfig, ["get", name, "-A", "--no-headers"],
                    check=False, timeout=45)
        count = len([l for l in (p.stdout or "").splitlines() if l.strip()])
        crds.append({"name": name, "group": group, "kind": kind,
                     "versions": versions, "instances": count,
                     "owner": (item["metadata"].get("labels", {}) or {}).get(
                         "operators.coreos.com/" + group, "")})

    subs = {i["spec"]["name"] for i in
            _json(kubeconfig, ["get", "subscription", "-A"], 60).get("items", [])}
    in_use = bool(subs & {s for s in subs if re.search(r"redis|valkey", s, re.I)})

    pvs = []
    for item in _json(kubeconfig, ["get", "pv"], 60).get("items", []):
        st = item.get("status", {})
        if st.get("phase") != "Released":
            continue
        claim = item["spec"].get("claimRef") or {}
        pvs.append({"name": item["metadata"]["name"],
                    "size": item["spec"].get("capacity", {}).get("storage"),
                    "storage_class": item["spec"].get("storageClassName"),
                    "reclaim": item["spec"].get("persistentVolumeReclaimPolicy"),
                    "claim": f"{claim.get('namespace', '?')}/{claim.get('name', '?')}"})

    return {
        "crds": crds,
        "crds_orphaned": [c for c in crds if c["instances"] == 0] if not in_use else [],
        "operator_still_installed": in_use,
        "released_pvs": pvs,
    }
