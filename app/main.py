"""FastAPI entrypoint.

Run on the installer node:   ./run.sh
Then open http://127.0.0.1:8800

Security notes
--------------
* Binds to 127.0.0.1 by default. Set DEPLOYER_HOST=0.0.0.0 only if you
  understand that this app holds cluster-admin credentials.
* The password you type is used once, for `oc login`, and never stored.
  What is kept is a per-session kubeconfig at /tmp/redis-deployer-sessions,
  mode 0600, deleted on logout or when the session expires.
* Passwords are stripped from every log line (see ocp._redact).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

from fastapi import Cookie, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import deploy, discover as discovery, ocp, operators as ophub
from . import analyze as keyspace
from . import inspect as inspector
from . import redistests
from . import opstree as ot
from .catalog import (
    COMMUNITY_VERSIONS,
    OPSTREE_TOPOLOGIES,
    OPSTREE_VERSIONS,
    EVICTION_POLICIES,
    PERSISTENCE_MODES,
    REDB_EVICTION,
    REDB_PERSISTENCE,
)
from .manifests import (
    community_manifests,
    dump,
    enterprise_operator,
    enterprise_rec,
    enterprise_redb,
    gen_password,
)
from .models import (CommunitySpec, EnterpriseSpec, LoginRequest,
                     OperatorInstallSpec, OpstreeSpec, TestRunSpec,
                     UninstallSpec)

STATIC = os.path.join(os.path.dirname(__file__), "static")
SESSION_TTL = 8 * 3600

app = FastAPI(title="OpenShift Redis Deployer", docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

SESSIONS: dict[str, dict] = {}


# ---------------------------------------------------------------- session

def _session(sid: Optional[str]) -> dict:
    s = SESSIONS.get(sid or "")
    if not s:
        raise HTTPException(401, "Not logged in")
    if time.time() - s["created"] > SESSION_TTL:
        ocp.logout(s["kubeconfig"])
        SESSIONS.pop(sid, None)  # type: ignore[arg-type]
        raise HTTPException(401, "Session expired, please log in again")
    return s


@app.get("/")
def index():
    """Serve index.html with cache-busted asset links.

    The browser was happily serving a stale style.css while picking up a fresh
    app.js, which produced a half-styled page. Stamping each asset URL with the
    file's mtime means a changed file gets a new URL and can never be served
    from cache.
    """
    html = open(os.path.join(STATIC, "index.html")).read()
    for asset in ("style.css", "app.js"):
        try:
            ver = int(os.path.getmtime(os.path.join(STATIC, asset)))
        except OSError:
            ver = 0
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={ver}")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/detect-server")
def detect_server():
    return {"server": ocp.detect_server()}


@app.post("/api/inspect")
def inspect_server(payload: dict):
    """Show what is at the other end BEFORE any credentials are sent."""
    server = (payload or {}).get("server", "").strip()
    if not server:
        raise HTTPException(400, "server is required")
    return inspector.inspect(server)


@app.post("/api/login")
def login(req: LoginRequest, response: Response):
    import uuid
    try:
        kubeconfig = ocp.login(req.server, req.username, req.password, req.insecure)
    except ocp.OcError as exc:
        raise HTTPException(401, str(exc).replace(req.password, "********")) from None
    except Exception as exc:                                    # noqa: BLE001
        raise HTTPException(500, f"Login failed: {exc}") from None

    sid = uuid.uuid4().hex
    user = ocp.whoami(kubeconfig)
    SESSIONS[sid] = {"kubeconfig": kubeconfig, "user": user,
                     "server": req.server, "created": time.time()}
    response.set_cookie("sid", sid, httponly=True, samesite="strict", max_age=SESSION_TTL)
    return {"user": user, "server": req.server,
            "cluster_admin": ocp.is_cluster_admin(kubeconfig)}


@app.post("/api/logout")
def logout(sid: Optional[str] = Cookie(None)):
    s = SESSIONS.pop(sid or "", None)
    if s:
        ocp.logout(s["kubeconfig"])
    return {"ok": True}


@app.get("/api/whoami")
def api_whoami(sid: Optional[str] = Cookie(None)):
    s = _session(sid)
    return {"user": s["user"], "server": s["server"]}


# ---------------------------------------------------------------- discovery

@app.get("/api/preflight")
def preflight(sid: Optional[str] = Cookie(None)):
    s = _session(sid)
    kc = s["kubeconfig"]

    nodes = []
    p = ocp.run(kc, ["get", "nodes", "-o", "json"], check=False, timeout=60)
    if p.returncode == 0:
        for item in json.loads(p.stdout).get("items", []):
            roles = [k.split("/", 1)[1] for k in item["metadata"].get("labels", {})
                     if k.startswith("node-role.kubernetes.io/")]
            alloc = item["status"].get("allocatable", {})
            nodes.append({
                "name": item["metadata"]["name"],
                "roles": roles,
                "cpu": alloc.get("cpu"),
                "memory": alloc.get("memory"),
                "schedulable": not item["spec"].get("taints"),
                "zone": item["metadata"].get("labels", {}).get("topology.kubernetes.io/zone"),
            })

    scs = []
    p = ocp.run(kc, ["get", "sc", "-o", "json"], check=False, timeout=60)
    if p.returncode == 0:
        for item in json.loads(p.stdout).get("items", []):
            ann = item["metadata"].get("annotations", {})
            prov = item.get("provisioner", "")
            scs.append({
                "name": item["metadata"]["name"],
                "provisioner": prov,
                "default": ann.get("storageclass.kubernetes.io/is-default-class") == "true",
                "expansion": item.get("allowVolumeExpansion", False),
                "binding": item.get("volumeBindingMode"),
                "reclaim": item.get("reclaimPolicy"),
                "kind": ocp.classify_storage(prov),
            })

    ver = ocp.run(kc, ["version", "-o", "json"], check=False, timeout=30)
    ocp_version = ""
    try:
        ocp_version = json.loads(ver.stdout).get("openshiftVersion", "")
    except Exception:
        pass

    workers = [n for n in nodes if n["schedulable"]]
    return {
        "openshift_version": ocp_version,
        "cluster_domain": ocp.cluster_domain(kc),
        "ingress_domain": ocp.jsonpath(kc, ["get", "ingress.config/cluster"],
                                       "{.spec.domain}"),
        "nodes": nodes,
        "schedulable_workers": len(workers),
        "storage_classes": scs,
        "zones_distinct": len({n["zone"] for n in workers if n["zone"]}),
    }


@app.get("/api/versions/community")
def versions_community():
    return {
        "versions": COMMUNITY_VERSIONS,
        "eviction_policies": [{"value": v, "label": l} for v, l in EVICTION_POLICIES],
        "persistence_modes": [{"value": v, "label": l} for v, l in PERSISTENCE_MODES],
    }


@app.get("/api/versions/opstree")
def versions_opstree(sid: Optional[str] = Cookie(None)):
    s = _session(sid)
    kc = s["kubeconfig"]
    installed = {}
    for kind in ("Redis", "RedisReplication", "RedisSentinel", "RedisCluster"):
        installed[kind] = ot.crd_fields(kc, kind)
    return {"topologies": OPSTREE_TOPOLOGIES, "versions": OPSTREE_VERSIONS,
            "crds": installed}


@app.post("/api/preview/opstree")
def preview_opstree(spec: OpstreeSpec):
    return {"yaml": dump(ot.manifests(spec, spec.password or "<generated-at-deploy>"))}


@app.post("/api/deploy/opstree")
def api_deploy_opstree(spec: OpstreeSpec, sid: Optional[str] = Cookie(None)):
    s = _session(sid)
    job = deploy.new_job("opstree")
    deploy.run_in_thread(job, deploy.deploy_opstree, s["kubeconfig"], spec)
    return {"job_id": job.id}


@app.get("/api/versions/enterprise")
def versions_enterprise(sid: Optional[str] = Cookie(None)):
    """Read what this cluster can actually install, from its PackageManifests."""
    s = _session(sid)
    kc = s["kubeconfig"]
    packages = []
    p = ocp.run(kc, ["get", "packagemanifest", "-n", "openshift-marketplace", "-o", "json"],
                check=False, timeout=90)
    if p.returncode == 0:
        for item in json.loads(p.stdout).get("items", []):
            name = item["metadata"]["name"]
            if "redis" not in name.lower():
                continue
            st = item.get("status", {})
            # Only packages that actually provide RedisEnterpriseCluster belong on
            # this form. The Opstree community operator matches "redis" by name but
            # provides Redis/RedisReplication/RedisSentinel/RedisCluster -- picking
            # it here would generate CRs it cannot reconcile.
            kinds = {
                crd.get("kind")
                for ch in st.get("channels", [])
                for crd in (((ch.get("currentCSVDesc") or {})
                             .get("customresourcedefinitions") or {}).get("owned") or [])
            }
            if "RedisEnterpriseCluster" not in kinds:
                continue
            packages.append({
                "name": name,
                "catalog": st.get("catalogSource"),
                "display": st.get("catalogSourceDisplayName"),
                "provider": (st.get("provider") or {}).get("name"),
                "default_channel": st.get("defaultChannel"),
                "channels": [{"name": c.get("name"), "csv": c.get("currentCSV")}
                             for c in st.get("channels", [])],
            })
    return {
        "packages": packages,
        "eviction_policies": REDB_EVICTION,
        "persistence_modes": REDB_PERSISTENCE,
    }


@app.get("/api/discover")
def discover(sid: Optional[str] = Cookie(None)):
    """Everything Redis-shaped that already exists on this cluster."""
    s = _session(sid)
    return discovery.discover(s["kubeconfig"])


@app.get("/api/operators")
def operators_search(q: str = "", catalog: str = "", certified: bool = False,
                     category: str = "", limit: int = 60, refresh: bool = False,
                     sid: Optional[str] = Cookie(None)):
    s = _session(sid)
    return ophub.search(s["kubeconfig"], q=q, catalog=catalog, certified=certified,
                        category=category, limit=limit, force=refresh)


@app.get("/api/operators/installed")
def operators_installed(sid: Optional[str] = Cookie(None)):
    s = _session(sid)
    return {"installed": ophub.installed(s["kubeconfig"])}


@app.get("/api/operators/detail")
def operators_detail(name: str, sid: Optional[str] = Cookie(None)):
    s = _session(sid)
    d = ophub.detail(s["kubeconfig"], name)
    if not d:
        raise HTTPException(404, f"No package '{name}' in the catalog")
    return d


@app.post("/api/operators/install")
def operators_install(spec: OperatorInstallSpec, sid: Optional[str] = Cookie(None)):
    s = _session(sid)
    job = deploy.new_job("operator")
    deploy.run_in_thread(job, deploy.install_operator, s["kubeconfig"], spec)
    return {"job_id": job.id}


@app.post("/api/preview/operator")
def preview_operator(spec: OperatorInstallSpec):
    from .manifests import operator_install
    return {"yaml": dump(operator_install(spec))}


@app.get("/api/leftovers")
def leftovers(sid: Optional[str] = Cookie(None)):
    """Cluster-scoped debris (CRDs, Released PVs) that an uninstall leaves behind."""
    s = _session(sid)
    return discovery.leftovers(s["kubeconfig"])


@app.post("/api/leftovers/cleanup")
def leftovers_cleanup(names: list[str], sid: Optional[str] = Cookie(None)):
    s = _session(sid)
    job = deploy.new_job("cleanup")
    deploy.run_in_thread(job, deploy.cleanup_crds, s["kubeconfig"], names)
    return {"job_id": job.id}


_SYSTEM_NS = ("openshift", "kube-", "default", "redhat-", "dell-", "metallb")


@app.get("/api/tests")
def list_tests(topology: str = "", sid: Optional[str] = Cookie(None)):
    _session(sid)
    return {"tests": redistests.applicable(topology) if topology
            else [{k: v for k, v in t.items() if k != "fn"} for t in redistests.TESTS]}


@app.post("/api/tests/run")
def run_tests(spec: TestRunSpec, sid: Optional[str] = Cookie(None)):
    s = _session(sid)
    chosen = [t for t in redistests.TESTS if t["id"] in spec.tests]
    if any(t["disruptive"] for t in chosen) and not spec.confirm_disruptive:
        raise HTTPException(400, "Disruptive tests selected without confirmation")
    job = deploy.new_job("test")
    deploy.run_in_thread(job, _run_suite, s["kubeconfig"], spec)
    return {"job_id": job.id}


def _run_suite(job, kubeconfig: str, spec: TestRunSpec):
    result = redistests.run_suite(job, kubeconfig, spec.kind, spec.namespace,
                                  spec.name, spec.tests, spec.client_namespace or "",
                                  topology=spec.topology or "",
                                  password=spec.password or "",
                                  client_image=spec.client_image or "")
    job.result.update(result)
    if result.get("failed"):
        raise RuntimeError(f"{result['counts']['fail']} test(s) failed")


@app.post("/api/analyze")
def analyze_keyspace(spec: TestRunSpec, sid: Optional[str] = Cookie(None)):
    s = _session(sid)
    job = deploy.new_job("analyze")
    deploy.run_in_thread(job, _analyze, s["kubeconfig"], spec)
    return {"job_id": job.id}


def _analyze(job, kubeconfig: str, spec: TestRunSpec):
    job.log(f"Analyzing keyspace of {spec.namespace}/{spec.name}")
    job.step(1, 2, "Locating a pod to read from")
    t = redistests.resolve_target(kubeconfig, spec.kind, spec.namespace, spec.name,
                                  log=job.log, topology=spec.topology or "",
                                  password=spec.password or "",
                                  client_image=spec.client_image or "")
    pod = t.primary or (t.pods[0] if t.pods else "")
    if not pod:
        raise RuntimeError("no running pod found")
    job.log(f"  reading from {pod}")
    job.step(2, 2, "Measuring")
    res = keyspace.analyze(kubeconfig, spec.namespace, pod, t.password, log=job.log)
    job.result.update({"kind": "analysis", "analysis": res,
                       "target": {"namespace": spec.namespace, "name": spec.name,
                                  "topology": t.topology}})
    job.log("")
    if res.get("empty"):
        job.log("The database is empty -- nothing to measure.")
        return
    job.log(f"  keys: {res['keys']:,}  ({res['keys_with_ttl']:,} with a TTL)")
    job.log(f"  memory: {res.get('used_memory_human')} of {res.get('maxmemory_human')}")
    if res.get("measured_bytes_per_key"):
        job.log(f"  MEASURED {res['measured_bytes_per_key']} bytes per key "
                "(this is the number to size with)")
    sm = res.get("sample")
    if sm:
        job.log(f"  sample of {sm['n']}: types {sm['types']}, "
                f"avg key {sm['avg_key_length']}B, avg memory {sm['avg_memory_per_key']}B")
        job.log(f"  p50 {sm['p50_memory']}B / p95 {sm['p95_memory']}B / "
                f"max {sm['max_memory_in_sample']}B per key")
    for line in res.get("bigkeys", [])[:8]:
        job.log(f"  {line}")


@app.get("/api/namespaces")
def namespaces(sid: Optional[str] = Cookie(None)):
    """All namespaces, flagged system vs user, with a workload count.

    A NetworkPolicy selects namespaces by LABEL, not by name -- so this also
    reports whether each namespace already carries `name: <itself>`, which is
    what the generated policy matches on. Without that label the policy silently
    matches nothing.
    """
    s = _session(sid)
    kc = s["kubeconfig"]
    out = []
    p = ocp.run(kc, ["get", "ns", "-o", "json"], check=False, timeout=60)
    if p.returncode == 0:
        for item in json.loads(p.stdout).get("items", []):
            md = item["metadata"]
            name = md["name"]
            labels = md.get("labels", {}) or {}
            out.append({
                "name": name,
                "system": any(name.startswith(x) or name == x for x in _SYSTEM_NS),
                "phase": (item.get("status") or {}).get("phase", ""),
                "has_name_label": labels.get("name") == name,
                "display": (md.get("annotations", {}) or {}).get(
                    "openshift.io/display-name", ""),
            })
    out.sort(key=lambda n: (n["system"], n["name"]))
    return {"namespaces": out}


# ---------------------------------------------------------------- preview

@app.post("/api/preview/community")
def preview_community(spec: CommunitySpec):
    return {"yaml": dump(community_manifests(spec, spec.password or "<generated-at-deploy>"))}


@app.post("/api/preview/enterprise")
def preview_enterprise(spec: EnterpriseSpec):
    objs = enterprise_operator(spec) + enterprise_rec(spec)
    if spec.expose_ui:
        objs += enterprise_ui_route(spec)
    if spec.create_db:
        objs += enterprise_redb(spec)
    return {"yaml": dump(objs)}


@app.get("/api/genpassword")
def genpassword():
    return {"password": gen_password()}


# ---------------------------------------------------------------- deploy

@app.post("/api/deploy/community")
def api_deploy_community(spec: CommunitySpec, sid: Optional[str] = Cookie(None)):
    s = _session(sid)
    job = deploy.new_job("community")
    deploy.run_in_thread(job, deploy.deploy_community, s["kubeconfig"], spec)
    return {"job_id": job.id}


@app.post("/api/deploy/enterprise")
def api_deploy_enterprise(spec: EnterpriseSpec, sid: Optional[str] = Cookie(None)):
    s = _session(sid)
    job = deploy.new_job("enterprise")
    deploy.run_in_thread(job, deploy.deploy_enterprise, s["kubeconfig"], spec)
    return {"job_id": job.id}


@app.post("/api/uninstall")
def api_uninstall(spec: UninstallSpec, sid: Optional[str] = Cookie(None)):
    s = _session(sid)
    job = deploy.new_job("uninstall")
    deploy.run_in_thread(job, deploy.uninstall, s["kubeconfig"], spec)
    return {"job_id": job.id}


@app.get("/api/job/{job_id}")
def job_status(job_id: str, offset: int = 0, sid: Optional[str] = Cookie(None)):
    _session(sid)
    job = deploy.JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    return {"status": job.status, "error": job.error,
            "lines": job.lines[offset:], "next_offset": len(job.lines),
            "result": job.result if job.status != "running" else {}}


@app.get("/api/job/{job_id}/stream")
async def job_stream(job_id: str, sid: Optional[str] = Cookie(None)):
    _session(sid)
    job = deploy.JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")

    async def gen():
        sent = 0
        while True:
            while sent < len(job.lines):
                yield f"data: {json.dumps({'line': job.lines[sent]})}\n\n"
                sent += 1
            if job.status != "running":
                payload = {"done": True, "status": job.status,
                           "error": job.error, "result": job.result}
                yield f"data: {json.dumps(payload)}\n\n"
                return
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- status

@app.get("/api/status")
def status(namespace: str, name: str = "", live: bool = True,
           sid: Optional[str] = Cookie(None)):
    """Structured status for one namespace, plus a live INFO from a pod."""
    s = _session(sid)
    kc = s["kubeconfig"]
    out: dict = {"namespace": namespace}

    def js(args, timeout=60):
        p = ocp.run(kc, [*args, "-o", "json"], check=False, timeout=timeout)
        if p.returncode != 0:
            return []
        try:
            return json.loads(p.stdout).get("items", [])
        except Exception:
            return []

    pods = []
    for it in js(["get", "pods", "-n", namespace]):
        md, st, spec = it["metadata"], it.get("status", {}), it["spec"]
        cs = st.get("containerStatuses") or []
        ready = sum(1 for c in cs if c.get("ready"))
        pods.append({
            "name": md["name"],
            "ready": f"{ready}/{len(cs)}",
            "phase": st.get("phase"),
            "restarts": sum(c.get("restartCount", 0) for c in cs),
            "node": spec.get("nodeName"),
            "ip": st.get("podIP"),
            "scc": (md.get("annotations") or {}).get("openshift.io/scc"),
            "qos": st.get("qosClass"),
            "started": md.get("creationTimestamp"),
            "healthy": ready == len(cs) and len(cs) > 0 and st.get("phase") == "Running",
        })
    out["pods"] = pods

    workloads = []
    for kind in ("deployments", "statefulsets"):
        for it in js(["get", kind, "-n", namespace]):
            st = it.get("status", {})
            workloads.append({
                "kind": kind[:-1],
                "name": it["metadata"]["name"],
                "ready": f"{st.get('readyReplicas', 0)}/{st.get('replicas', 0)}",
                "images": [c.get("image") for c in
                           it["spec"]["template"]["spec"].get("containers", [])],
            })
    out["workloads"] = workloads

    out["services"] = [{
        "name": it["metadata"]["name"],
        "type": it["spec"].get("type"),
        "cluster_ip": it["spec"].get("clusterIP"),
        "ports": ", ".join(f"{p.get('port')}"
                           + (f":{p['nodePort']}" if p.get("nodePort") else "")
                           for p in it["spec"].get("ports", [])),
        "selector": ", ".join(f"{k}={v}" for k, v in
                              (it["spec"].get("selector") or {}).items()),
    } for it in js(["get", "svc", "-n", namespace])]

    out["pvcs"] = [{
        "name": it["metadata"]["name"],
        "phase": it.get("status", {}).get("phase"),
        "capacity": (it.get("status", {}).get("capacity") or {}).get("storage"),
        "storage_class": it["spec"].get("storageClassName") or "(default)",
        "volume": it["spec"].get("volumeName"),
    } for it in js(["get", "pvc", "-n", namespace])]

    out["policies"] = [{
        "name": it["metadata"]["name"],
        "pod_selector": ", ".join(f"{k}={v}" for k, v in
                                  (it["spec"].get("podSelector", {})
                                   .get("matchLabels") or {}).items()) or "(all pods)",
        "allows": [ns.get("namespaceSelector", {}).get("matchLabels", {})
                   .get("kubernetes.io/metadata.name")
                   or ("pods in this namespace" if "podSelector" in ns else "?")
                   for rule in it["spec"].get("ingress", [])
                   for ns in rule.get("from", [])],
    } for it in js(["get", "networkpolicy", "-n", namespace])]

    crs = {}
    for kind in ("rec", "redb", "redis", "redisreplication",
                 "redissentinel", "rediscluster"):
        p = ocp.run(kc, ["get", kind, "-n", namespace, "--no-headers"],
                    check=False, timeout=45)
        if p.returncode == 0 and (p.stdout or "").strip():
            crs[kind] = p.stdout.strip()
    out["custom_resources"] = crs

    events = []
    p = ocp.run(kc, ["get", "events", "-n", namespace, "--sort-by=.lastTimestamp",
                     "-o", "json"], check=False, timeout=60)
    try:
        for it in json.loads(p.stdout).get("items", [])[-25:]:
            events.append({
                "type": it.get("type"),
                "reason": it.get("reason"),
                "object": f"{it['involvedObject'].get('kind')}/{it['involvedObject'].get('name')}",
                "message": (it.get("message") or "")[:160],
                "last": it.get("lastTimestamp"),
            })
    except Exception:
        pass
    out["events"] = events

    # a live INFO from the first healthy pod -- cheap, and the most useful line
    out["live"] = {}
    if live and pods:
        target = next((p for p in pods if p["healthy"]), None)
        if target:
            secret = ""
            for sname, key in ((f"{name}-auth", "redis-password"),
                               (f"{name}-auth", "password"),
                               (f"redb-{name}", "password")):
                raw = ocp.run(kc, ["get", "secret", sname, "-n", namespace,
                                   "-o", f"jsonpath={{.data.{key}}}"],
                              check=False, timeout=30).stdout.strip()
                if raw:
                    import base64 as _b
                    secret = _b.b64decode(raw).decode()
                    break
            auth = ["-a", secret, "--no-auth-warning"] if secret else []
            r = ocp.run(kc, ["exec", "-n", namespace, target["name"], "--",
                             "redis-cli", *auth, "INFO"], check=False, timeout=45)
            info = {}
            for line in (r.stdout or "").splitlines():
                if ":" in line and not line.startswith("#"):
                    k, v = line.split(":", 1)
                    info[k.strip()] = v.strip()
            if info:
                out["live"] = {
                    "pod": target["name"],
                    "version": info.get("redis_version"),
                    "mode": info.get("redis_mode"),
                    "role": info.get("role"),
                    "connected_slaves": info.get("connected_slaves"),
                    "uptime_days": info.get("uptime_in_days"),
                    "used_memory": info.get("used_memory_human"),
                    "maxmemory": info.get("maxmemory_human"),
                    "maxmemory_policy": info.get("maxmemory_policy"),
                    "keys": info.get("db0", ""),
                    "connected_clients": info.get("connected_clients"),
                    "evicted_keys": info.get("evicted_keys"),
                    "keyspace_hits": info.get("keyspace_hits"),
                    "keyspace_misses": info.get("keyspace_misses"),
                    "aof_enabled": info.get("aof_enabled"),
                    "rdb_last_bgsave_status": info.get("rdb_last_bgsave_status"),
                }
    return out
