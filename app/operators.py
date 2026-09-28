"""OperatorHub search.

The full PackageManifest JSON is ~22 MB / 617 packages on a stock OpenShift
cluster and takes about 3 seconds to fetch. Too slow to hit on every keystroke,
cheap enough to pull once: we fetch it, reduce it to a compact index (name,
provider, categories, description, channels, provided APIs) and cache that for
ten minutes. Search then runs in memory.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from . import ocp

_CACHE: dict[str, tuple[float, list[dict]]] = {}
_TTL = 600
MARKETPLACE = "openshift-marketplace"


def _trim(text: str, n: int = 400) -> str:
    text = " ".join((text or "").split())
    return text[: n - 1] + "…" if len(text) > n else text


def _entry(item: dict) -> dict:
    md = item.get("metadata", {})
    st = item.get("status", {})
    channels = st.get("channels", []) or []
    # describe the DEFAULT channel, not whichever happens to be first --
    # a "preview" channel is often older and declares fewer CRDs than "stable"
    default = st.get("defaultChannel")
    chan = next((c for c in channels if c.get("name") == default),
                channels[0] if channels else {})
    desc0 = chan.get("currentCSVDesc") or {}
    ann = desc0.get("annotations", {}) or {}

    apis = []
    for crd in ((desc0.get("customresourcedefinitions") or {}).get("owned") or []):
        apis.append({"kind": crd.get("kind"), "name": crd.get("name"),
                     "version": crd.get("version"),
                     "display": crd.get("displayName"),
                     "description": _trim(crd.get("description", ""), 160)})

    return {
        "name": md.get("name"),
        "catalog": st.get("catalogSource"),
        "catalog_display": st.get("catalogSourceDisplayName"),
        "provider": (st.get("provider") or {}).get("name") or ann.get("createdBy"),
        "default_channel": st.get("defaultChannel"),
        "display_name": desc0.get("displayName") or md.get("name"),
        "version": desc0.get("version"),
        "description": _trim(ann.get("description") or desc0.get("description", "")),
        "long_description": desc0.get("description", ""),
        "categories": [c.strip() for c in (ann.get("categories") or "").split(",") if c.strip()],
        "keywords": desc0.get("keywords", []) or [],
        "certified": str(ann.get("certified", "")).lower() == "true",
        "capabilities": ann.get("capabilities"),
        "support": ann.get("support"),
        "repository": ann.get("repository"),
        "infrastructure_features": [
            f.strip(' "') for f in
            (ann.get("operators.openshift.io/infrastructure-features") or "")
            .strip("[]").split(",") if f.strip(' "')
        ],
        "channels": [{"name": c.get("name"),
                      "csv": c.get("currentCSV"),
                      "version": (c.get("currentCSVDesc") or {}).get("version")}
                     for c in channels],
        "install_modes": [{"type": m.get("type"), "supported": m.get("supported")}
                          for m in (desc0.get("installModes") or [])],
        "provided_apis": apis,
    }


def index(kubeconfig: str, force: bool = False) -> list[dict]:
    hit = _CACHE.get(kubeconfig)
    if hit and not force and time.time() - hit[0] < _TTL:
        return hit[1]

    p = ocp.run(kubeconfig, ["get", "packagemanifests", "-n", MARKETPLACE, "-o", "json"],
                check=False, timeout=180)
    if p.returncode != 0:
        return hit[1] if hit else []
    try:
        items = json.loads(p.stdout).get("items", [])
    except Exception:
        return hit[1] if hit else []

    entries = [_entry(i) for i in items]
    entries.sort(key=lambda e: (e["name"] or "").lower())
    _CACHE[kubeconfig] = (time.time(), entries)
    return entries


def _score(e: dict, needles: list[str]) -> int:
    """Higher is better; 0 means no match on at least one needle."""
    name = (e["name"] or "").lower()
    disp = (e["display_name"] or "").lower()
    prov = (e["provider"] or "").lower()
    cats = " ".join(e["categories"]).lower()
    keys = " ".join(e["keywords"]).lower()
    desc = (e["description"] or "").lower()

    total = 0
    for n in needles:
        s = 0
        if name == n:
            s = 100
        elif name.startswith(n):
            s = 60
        elif n in name:
            s = 40
        elif n in disp:
            s = 30
        elif n in keys:
            s = 20
        elif n in cats:
            s = 15
        elif n in prov:
            s = 12
        elif n in desc:
            s = 8
        if s == 0:
            return 0                      # every needle must match somewhere
        total += s
    return total


def search(kubeconfig: str, q: str = "", catalog: str = "", certified: bool = False,
           category: str = "", limit: int = 60, force: bool = False) -> dict[str, Any]:
    entries = index(kubeconfig, force=force)
    needles = [w for w in (q or "").lower().split() if w]

    rows = []
    for e in entries:
        if catalog and e["catalog"] != catalog:
            continue
        if certified and not e["certified"]:
            continue
        if category and category not in e["categories"]:
            continue
        score = _score(e, needles) if needles else 1
        if score:
            rows.append((score, e))

    rows.sort(key=lambda r: (-r[0], (r[1]["name"] or "").lower()))
    total = len(rows)

    cats: dict[str, int] = {}
    catalogs: dict[str, int] = {}
    for e in entries:
        catalogs[e["catalog"]] = catalogs.get(e["catalog"], 0) + 1
        for c in e["categories"]:
            cats[c] = cats.get(c, 0) + 1

    slim = []
    for _, e in rows[:limit]:
        d = {k: e[k] for k in ("name", "catalog", "catalog_display", "provider",
                               "default_channel", "display_name", "version",
                               "description", "categories", "certified",
                               "capabilities", "support")}
        d["channel_count"] = len(e["channels"])
        d["api_kinds"] = [a["kind"] for a in e["provided_apis"]][:6]
        slim.append(d)

    return {"total": total, "shown": len(slim), "results": slim,
            "catalogs": [{"name": k, "count": v} for k, v in sorted(catalogs.items())],
            "categories": [{"name": k, "count": v}
                           for k, v in sorted(cats.items(), key=lambda x: -x[1])[:25]],
            "indexed": len(entries)}


def detail(kubeconfig: str, name: str) -> Optional[dict]:
    for e in index(kubeconfig):
        if e["name"] == name:
            return e
    return None


def installed(kubeconfig: str) -> list[dict]:
    """Subscriptions across the cluster, joined to their CSV phase."""
    subs = []
    p = ocp.run(kubeconfig, ["get", "subscription", "-A", "-o", "json"],
                check=False, timeout=90)
    if p.returncode != 0:
        return subs

    phases: dict[tuple[str, str], tuple[str, str]] = {}
    q = ocp.run(kubeconfig,
                ["get", "csv", "-A", "-o",
                 "custom-columns=NS:.metadata.namespace,NAME:.metadata.name,"
                 "VERSION:.spec.version,PHASE:.status.phase", "--no-headers"],
                check=False, timeout=90)
    for line in (q.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 4:
            phases[(parts[0], parts[1])] = (parts[2], parts[3])

    for item in json.loads(p.stdout).get("items", []):
        md, spec, st = item["metadata"], item["spec"], item.get("status", {})
        csv = st.get("installedCSV") or st.get("currentCSV") or ""
        ver, phase = phases.get((md["namespace"], csv), ("", ""))
        subs.append({
            "package": spec.get("name"),
            "namespace": md["namespace"],
            "channel": spec.get("channel"),
            "catalog": spec.get("source"),
            "approval": spec.get("installPlanApproval", "Automatic"),
            "csv": csv,
            "version": ver,
            "phase": phase,
        })
    subs.sort(key=lambda s: (s["package"] or "", s["namespace"]))
    return subs
