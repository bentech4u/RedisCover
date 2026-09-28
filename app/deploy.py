"""Deployment orchestration.

Each deployment runs as a Job: a background thread appending log lines that the
browser tails over SSE. Jobs never raise into the request path -- failures land
in the log and flip the job to 'failed'.
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from . import ocp
from . import opstree as ot
from .catalog import OPSTREE_GROUP, OPSTREE_PACKAGE, community_version, opstree_topology
from .manifests import (
    community_manifests,
    dump,
    enterprise_license,
    enterprise_namespace,
    enterprise_operator,
    enterprise_rec,
    enterprise_redb,
    enterprise_ui_route,
    gen_password,
    network_policy,
    operator_install,
)
from .models import (CommunitySpec, EnterpriseSpec, OperatorInstallSpec,
                     OpstreeSpec, UninstallSpec)


@dataclass
class Job:
    id: str
    kind: str
    status: str = "running"          # running | succeeded | failed
    lines: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None

    def log(self, msg: str = "") -> None:
        ts = time.strftime("%H:%M:%S")
        for line in str(msg).splitlines() or [""]:
            self.lines.append(f"{ts}  {line}")

    def step(self, n: int, total: int, title: str) -> None:
        self.log("")
        self.log(f"===== [{n}/{total}] {title} " + "=" * max(0, 46 - len(title)))


JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()


def new_job(kind: str) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], kind=kind)
    with _LOCK:
        JOBS[job.id] = job
        # keep the last 50 jobs only
        if len(JOBS) > 50:
            for k in sorted(JOBS, key=lambda j: JOBS[j].started)[:-50]:
                JOBS.pop(k, None)
    return job


def _finish(job: Job, ok: bool, err: str | None = None) -> None:
    job.status = "succeeded" if ok else "failed"
    job.error = err
    job.finished = time.time()
    job.log("")
    job.log("=" * 60)
    job.log("RESULT: " + ("SUCCESS" if ok else f"FAILED - {err}"))


def apply_manifests(job: Job, kubeconfig: str, objs: list[dict], *,
                    dry_run_first: bool = True) -> None:
    """Apply objects, creating any Namespace FIRST.

    `oc apply --dry-run=server` validates against the live API server, so an
    object inside a namespace that does not exist yet fails with NotFound.
    The namespace therefore has to be created for real before the rest can be
    validated. Creating a namespace is idempotent and harmless.
    """
    ns_objs = [o for o in objs if o.get("kind") == "Namespace"]
    rest = [o for o in objs if o.get("kind") != "Namespace"]

    if ns_objs:
        job.log("  creating namespace(s) first so the dry run can validate against them")
        ocp.apply_yaml(kubeconfig, dump(ns_objs), log=job.log)

    if rest and dry_run_first:
        ocp.apply_yaml(kubeconfig, dump(rest), dry_run=True, log=job.log)
        job.log("  dry run accepted by the API server")
    if rest:
        ocp.apply_yaml(kubeconfig, dump(rest), log=job.log)


def run_in_thread(job: Job, fn, *args) -> None:
    def wrapper():
        try:
            fn(job, *args)
            _finish(job, True)
        except Exception as exc:                      # noqa: BLE001
            job.log("")
            job.log("!! " + str(exc))
            job.log(traceback.format_exc())
            _finish(job, False, str(exc))
    threading.Thread(target=wrapper, daemon=True).start()


# ============================================================ community

def deploy_community(job: Job, kubeconfig: str, spec: CommunitySpec) -> None:
    total = 6
    v = community_version(spec.version_id) or {}
    image = spec.custom_image or v.get("image")
    password = spec.password or gen_password()

    job.log(f"Redis (community) -> namespace '{spec.namespace}', release '{spec.name}'")
    job.log(f"Image: {image}")
    job.log(f"Persistence: {spec.persistence}   maxmemory: {spec.maxmemory} ({spec.maxmemory_policy})")

    job.step(1, total, "Pre-flight")
    if spec.persistence != "none":
        if not spec.storage_class:
            # Resolve the default and PIN it into the manifest. Relying on the
            # implicit default means the manifest does not record what it got,
            # and a StatefulSet's volumeClaimTemplates are IMMUTABLE -- landing on
            # the wrong class can only be fixed by deleting and recreating.
            default_sc = ocp.jsonpath(
                kubeconfig, ["get", "sc"],
                "{.items[?(@.metadata.annotations.storageclass\\.kubernetes\\.io/is-default-class==\"true\")].metadata.name}")
            if not default_sc:
                raise RuntimeError(
                    "No StorageClass selected and this cluster has no default -- "
                    "the PVC would stay Pending forever")
            spec.storage_class = default_sc
            job.log(f"  no class selected; resolved the cluster default: {default_sc}")
            job.log("  pinning it into the manifest so the choice is recorded")

        p = ocp.run(kubeconfig, ["get", "sc", spec.storage_class], check=False, log=job.log)
        if p.returncode != 0:
            raise RuntimeError(f"StorageClass '{spec.storage_class}' not found")

        prov = ocp.jsonpath(kubeconfig, ["get", "sc", spec.storage_class], "{.provisioner}")
        kind = ocp.classify_storage(prov)
        job.log(f"  StorageClass {spec.storage_class} -> {prov} ({kind})")
        if kind == "file" and not spec.allow_file_storage:
            raise RuntimeError(
                f"'{spec.storage_class}' is FILE storage ({prov}). Redis relies on "
                "fsync semantics and file locking that NAS/NFS handles differently; "
                "the failure mode is corruption weeks later, not an error today. "
                "Pick a block StorageClass, or tick 'allow file storage' to override.")
        if kind == "unknown":
            job.log("  NOTE: provisioner not recognised -- confirm it presents a block "
                    "device before trusting it with data")

    # a StatefulSet's volumeClaimTemplates cannot be changed after creation
    if spec.topology == "replication":
        existing = ocp.jsonpath(
            kubeconfig, ["get", "statefulset", spec.name, "-n", spec.namespace],
            "{.spec.volumeClaimTemplates[0].spec.storageClassName}")
        exists = ocp.run(kubeconfig, ["get", "statefulset", spec.name, "-n", spec.namespace],
                         check=False, timeout=30).returncode == 0
        if exists and (existing or "<default>") != (spec.storage_class or "<default>"):
            raise RuntimeError(
                f"StatefulSet '{spec.name}' already exists with storageClassName "
                f"'{existing or '(cluster default)'}', and volumeClaimTemplates are "
                f"IMMUTABLE -- re-applying with '{spec.storage_class}' would silently "
                "change nothing. Delete the StatefulSet and its PVCs first "
                "(Uninstall tab, tick 'delete PVCs').")

    job.step(2, total, "Rendering manifests")
    objs = community_manifests(spec, password)
    yaml_text = dump(objs)
    job.result["manifests"] = yaml_text
    job.log(f"  {len(objs)} objects: " + ", ".join(o['kind'] for o in objs))

    job.step(3, total, "Applying (namespace first, then a dry run of the rest)")
    apply_manifests(job, kubeconfig, objs)

    job.step(4, total, "Applied")
    job.log("  all objects accepted")

    job.step(5, total, "Waiting for rollout")
    # standalone is a Deployment; replication is a StatefulSet
    workload = "statefulset" if spec.topology == "replication" else "deployment"
    timeout = 900 if spec.topology == "replication" else 300
    if spec.topology == "replication":
        job.log(f"  StatefulSet: pods start one at a time ({spec.replicas} of them),")
        job.log("  each waiting for its own volume, so this is slower than a Deployment")
    try:
        ocp.run(kubeconfig,
                ["rollout", "status", f"{workload}/{spec.name}", "-n", spec.namespace,
                 f"--timeout={timeout}s"],
                timeout=timeout + 60, log=job.log)
    except Exception:
        job.log("")
        job.log("Rollout did not complete. Recent events:")
        job.log(ocp.recent_events(kubeconfig, spec.namespace, 30))
        raise

    job.step(6, total, "Verifying")
    if spec.topology == "replication":
        # ordinal 0 is the primary; replicas are read-only, so test against it
        pod = f"{spec.name}-0"
    else:
        pod = ocp.jsonpath(kubeconfig,
                           ["get", "pod", "-n", spec.namespace, "-l", f"app={spec.name}"],
                           "{.items[0].metadata.name}")
    scc = ocp.jsonpath(kubeconfig, ["get", "pod", pod, "-n", spec.namespace],
                       "{.metadata.annotations.openshift\\.io/scc}")
    # restricted-v2 injects runAsUser on the CONTAINER, not on the pod
    uid = ocp.jsonpath(kubeconfig, ["get", "pod", pod, "-n", spec.namespace],
                       "{.spec.containers[0].securityContext.runAsUser}") or \
          ocp.jsonpath(kubeconfig, ["get", "pod", pod, "-n", spec.namespace],
                       "{.spec.securityContext.runAsUser}")
    fsgroup = ocp.jsonpath(kubeconfig, ["get", "pod", pod, "-n", spec.namespace],
                           "{.spec.securityContext.fsGroup}")
    qos = ocp.jsonpath(kubeconfig, ["get", "pod", pod, "-n", spec.namespace],
                       "{.status.qosClass}")
    job.log(f"  pod={pod}  scc={scc}  runAsUser={uid}  fsGroup={fsgroup}  qos={qos}")

    if spec.persistence != "none":
        p = ocp.run(kubeconfig,
                    ["get", "pvc", "-n", spec.namespace, "-o",
                     "custom-columns=NAME:.metadata.name,SC:.spec.storageClassName,"
                     "CAP:.status.capacity.storage", "--no-headers"],
                    check=False, timeout=45)
        for line in (p.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                job.log(f"  pvc {parts[0]} -> {parts[1]} ({parts[2] if len(parts) > 2 else '?'})")
                if spec.storage_class and parts[1] != spec.storage_class:
                    raise RuntimeError(
                        f"PVC {parts[0]} bound to '{parts[1]}' but '{spec.storage_class}' "
                        "was requested")
        job.result["storage_class"] = spec.storage_class

    auth = ["-a", password, "--no-auth-warning"] if spec.auth_enabled else []
    p = ocp.run(kubeconfig,
                ["exec", "-n", spec.namespace, pod, "--",
                 "redis-cli", *auth, "PING"],
                check=False, log=job.log)
    if "PONG" not in (p.stdout or ""):
        raise RuntimeError("Redis did not answer PING")
    job.log("  PING -> PONG")

    if spec.topology == "replication":
        info = ocp.run(kubeconfig,
                       ["exec", "-n", spec.namespace, pod, "--",
                        "redis-cli", *auth, "INFO", "replication"],
                       check=False).stdout or ""
        role = next((l.split(":")[1].strip() for l in info.splitlines()
                     if l.startswith("role:")), "?")
        connected = next((l.split(":")[1].strip() for l in info.splitlines()
                          if l.startswith("connected_slaves:")), "0")
        job.log(f"  {pod} role={role}  connected replicas={connected}")
        if role != "master":
            raise RuntimeError(f"{pod} should be the primary but reports role={role}")
        if int(connected or 0) < spec.replicas - 1:
            job.log(f"  WARNING: expected {spec.replicas - 1} replicas, "
                    f"{connected} are connected -- check the other pods' logs")

    if spec.auth_enabled:
        p = ocp.run(kubeconfig,
                    ["exec", "-n", spec.namespace, pod, "--",
                     "redis-cli", "PING"], check=False)
        if "NOAUTH" in (p.stdout or "") + (p.stderr or ""):
            job.log("  unauthenticated PING correctly refused (NOAUTH)")
        else:
            job.log("  WARNING: unauthenticated PING was NOT refused -- check requirepass")

    domain = ocp.cluster_domain(kubeconfig, spec.namespace, pod, log=job.log)
    host = f"{spec.name}.{spec.namespace}.svc.{domain}"
    job.result.update({
        "kind": "community", "namespace": spec.namespace, "name": spec.name,
        "image": image, "host": host, "port": 6379,
        "password": password if spec.auth_enabled else None,
        "scc": scc, "runAsUser": uid, "fsGroup": fsgroup, "qos": qos,
        "cluster_domain": domain,
        "restricted_to": spec.allow_namespaces,
    })
    if spec.topology == "replication":
        job.result["read_host"] = f"{spec.name}-read.{spec.namespace}.svc.{domain}"
        job.result["topology"] = "replication"
        job.result["notes"] = [
            f"Writes -> {host}:6379 (selects {spec.name}-0 only).",
            f"Reads  -> {spec.name}-read.{spec.namespace}.svc.{domain}:6379 (all pods).",
            "Replication is ASYNCHRONOUS: a read-after-write via the read Service "
            "may return a stale value. Read-your-own-writes must use the write Service.",
            "No automatic failover. If the primary pod dies, writes fail until "
            "Kubernetes restarts it (~30s, or minutes if the node itself is lost).",
        ]
    else:
        job.result["topology"] = "standalone"

    job.log("")
    job.log(f"In-cluster address: {host}:6379")
    job.log(f"  '{domain}' is the cluster's INTERNAL DNS domain, unrelated to the")
    job.log("  cluster's external name. It resolves only from pods inside the cluster.")

    if spec.service_type == "NodePort":
        np = ocp.jsonpath(kubeconfig, ["get", "svc", spec.name, "-n", spec.namespace],
                          "{.spec.ports[0].nodePort}")
        ips = ocp.jsonpath(
            kubeconfig, ["get", "nodes", "-l", "node-role.kubernetes.io/worker="],
            '{range .items[*]}{.status.addresses[?(@.type=="InternalIP")].address}{" "}{end}')
        job.result["node_port"] = np
        job.result["node_ips"] = ips.split()
        job.log(f"  external: any node IP on port {np} -- e.g. {(ips.split() or ['<node-ip>'])[0]}:{np}")
        job.log("  that port is open on EVERY node; keep the password and a NetworkPolicy")
    else:
        job.log("")
        job.log("From outside the cluster (ClusterIP is internal-only):")
        job.log(f"  oc port-forward -n {spec.namespace} svc/{spec.name} 16379:6379")
        job.log(f"  redis-cli -h 127.0.0.1 -p 16379 -a '<password>' PING")


# ============================================================ enterprise

def deploy_enterprise(job: Job, kubeconfig: str, spec: EnterpriseSpec) -> None:
    total = 8
    job.log(f"Redis Enterprise -> namespace '{spec.namespace}'")
    job.log(f"Operator: {spec.package} / channel {spec.channel} / approval {spec.approval}")
    job.log(f"Cluster: {spec.nodes} nodes x {spec.cpu} CPU / {spec.memory}, {spec.volume_size} each")
    if not spec.license_text:
        job.log("No licence supplied -- the cluster will run in TRIAL mode (4 shards / 30 days).")

    job.step(1, total, "Pre-flight: capacity and storage")
    _preflight_enterprise(job, kubeconfig, spec)

    job.step(2, total, "Namespace and licence")
    ocp.apply_yaml(kubeconfig, dump(enterprise_namespace(spec)), log=job.log)
    lic = enterprise_license(spec)
    if lic:
        ocp.apply_yaml(kubeconfig, dump(lic), log=job.log)
        job.log("  licence secret created")

    job.step(3, total, "Installing the operator (OperatorGroup + Subscription)")
    op_yaml = dump(enterprise_operator(spec))
    job.result["operator_manifests"] = op_yaml
    ocp.apply_yaml(kubeconfig, op_yaml, log=job.log)

    job.step(4, total, "Approving the InstallPlan")
    _approve_installplan(job, kubeconfig, spec.namespace, spec.package)

    job.step(5, total, "Waiting for the ClusterServiceVersion")
    csv = ocp.wait_for(
        kubeconfig, ["get", "csv", "-n", spec.namespace],
        "{.items[0].status.phase}", "Succeeded",
        timeout=900, label="CSV phase", log=job.log)
    csv_name = ocp.jsonpath(kubeconfig, ["get", "csv", "-n", spec.namespace],
                            "{.items[0].metadata.name}")
    job.result["csv"] = csv_name
    job.log(f"  operator installed: {csv_name} ({csv})")

    job.step(6, total, "Creating the RedisEnterpriseCluster")
    rec_objs = enterprise_rec(spec)
    job.result["rec_manifests"] = dump(rec_objs)
    apply_manifests(job, kubeconfig, rec_objs)
    job.log("  this pulls a ~1.9 GB image per node -- expect 5-15 minutes")
    try:
        ocp.wait_for(kubeconfig,
                     ["get", "rec", spec.rec_name, "-n", spec.namespace],
                     "{.status.state}", "Running",
                     timeout=1800, interval=15, label="REC state", log=job.log)
    except Exception:
        job.log("")
        job.log("Cluster did not reach Running. Recent events:")
        job.log(ocp.recent_events(kubeconfig, spec.namespace, 30))
        raise

    _expose_ui(job, kubeconfig, spec)

    if not spec.create_db:
        job.result.update({"kind": "enterprise", "namespace": spec.namespace,
                           "rec": spec.rec_name, "database": None})
        job.log("Database creation skipped by request.")
        return

    job.step(7, total, "Creating the database")
    db_yaml = dump(enterprise_redb(spec))
    job.result["redb_manifests"] = db_yaml
    try:
        ocp.apply_yaml(kubeconfig, db_yaml, dry_run=True, log=job.log)
    except ocp.OcError as exc:
        raise RuntimeError(
            "The admission webhook rejected the database spec. Enum values differ "
            "between operator versions -- check `oc explain redb.spec.persistence` "
            f"and `oc explain redb.spec.evictionPolicy`. Detail: {exc}") from exc
    ocp.apply_yaml(kubeconfig, db_yaml, log=job.log)
    ocp.wait_for(kubeconfig,
                 ["get", "redb", spec.db_name, "-n", spec.namespace],
                 "{.status.status}", "active",
                 timeout=900, label="database status", log=job.log)

    job.step(8, total, "Collecting connection details")
    secret = f"redb-{spec.db_name}"
    pw = ocp.run(kubeconfig,
                 ["get", "secret", secret, "-n", spec.namespace,
                  "-o", "jsonpath={.data.password}"], check=False).stdout.strip()
    import base64 as _b64
    password = _b64.b64decode(pw).decode() if pw else None
    # the live endpoint is in status.internalEndpoints, not status.databasePort
    port = ocp.jsonpath(kubeconfig,
                        ["get", "redb", spec.db_name, "-n", spec.namespace],
                        "{.status.internalEndpoints[0].port}") or str(spec.db_port)
    internal_host = ocp.jsonpath(kubeconfig,
                                 ["get", "redb", spec.db_name, "-n", spec.namespace],
                                 "{.status.internalEndpoints[0].host}")

    if spec.allow_namespaces:
        pod_labels = {"app": "redis-enterprise"}
        job.log("Applying NetworkPolicy (verify the podSelector matches your rec-* pods)")
        ocp.apply_yaml(kubeconfig, dump([network_policy(
            spec.namespace, pod_labels, int(port), spec.allow_namespaces,
            f"allow-clients-to-{spec.db_name}")]), log=job.log)

    # the operator publishes the real FQDN in status; trust it over anything
    # we could construct, and fall back to the discovered cluster domain
    domain = ocp.cluster_domain(kubeconfig, spec.namespace,
                                f"{spec.rec_name}-0", log=job.log)
    host = internal_host or f"{spec.db_name}.{spec.namespace}.svc.{domain}"
    job.result.update({
        "kind": "enterprise", "namespace": spec.namespace, "rec": spec.rec_name,
        "database": spec.db_name, "host": host, "port": port, "password": password,
        "secret": secret, "internal_host": internal_host,
        "cluster_domain": domain, "name": spec.db_name,
        "notes": [
            "Only database 0 exists -- SELECT 1..15 will fail.",
            f"Port is {port}, not 6379.",
            "Clients MUST retry on connection failure (failover causes a brief reconnect).",
        ],
    })
    job.log(f"  {host}:{port}")


def _preflight_enterprise(job: Job, kubeconfig: str, spec: EnterpriseSpec) -> None:
    p = ocp.run(kubeconfig,
                ["get", "nodes", "-l", "node-role.kubernetes.io/worker=",
                 "-o", "custom-columns=NAME:.metadata.name,CPU:.status.allocatable.cpu,"
                       "MEM:.status.allocatable.memory"],
                check=False, log=job.log)
    schedulable = max(0, len((p.stdout or "").strip().splitlines()) - 1)
    if schedulable < spec.nodes:
        raise RuntimeError(
            f"{spec.nodes} cluster nodes requested but only {schedulable} worker nodes found. "
            "Redis Enterprise uses REQUIRED pod anti-affinity: one pod per node, so extra "
            "replicas stay Pending forever.")
    job.log(f"  {schedulable} worker nodes available for {spec.nodes} REC nodes")

    if spec.storage_class:
        prov = ocp.jsonpath(kubeconfig, ["get", "sc", spec.storage_class], "{.provisioner}")
        kind = ocp.classify_storage(prov)
        job.log(f"  StorageClass {spec.storage_class} -> {prov} ({kind})")
        if kind == "file":
            job.log("  WARNING: this is FILE storage (NAS/NFS). Redis Enterprise expects BLOCK "
                    "(Ceph RBD, vSphere, iSCSI, SAN). File storage handles fsync and locking "
                    "differently and risks corruption.")
        elif kind == "unknown":
            job.log("  NOTE: could not classify this provisioner. Confirm it presents a BLOCK "
                    "device (ReadWriteOnce) before using it for a database.")

    pm = ocp.run(kubeconfig,
                 ["get", "packagemanifest", spec.package, "-n", "openshift-marketplace"],
                 check=False)
    if pm.returncode != 0:
        raise RuntimeError(
            f"PackageManifest '{spec.package}' not found in openshift-marketplace. "
            "Check the operator name on the Versions step.")
    job.log(f"  operator package '{spec.package}' found in the catalog")


def _approve_installplan(job: Job, kubeconfig: str, namespace: str,
                         package: str) -> None:
    deadline = time.time() + 300
    name = ""
    while time.time() < deadline:
        name = ocp.jsonpath(kubeconfig, ["get", "installplan", "-n", namespace],
                            "{.items[0].metadata.name}")
        if name:
            break
        time.sleep(5)
    if not name:
        raise RuntimeError(
            "No InstallPlan appeared within 5 minutes. Check the Subscription status: "
            f"oc describe subscription {package} -n {namespace}")

    approved = ocp.jsonpath(kubeconfig,
                            ["get", "installplan", name, "-n", namespace],
                            "{.spec.approved}")
    if approved == "true":
        job.log(f"  InstallPlan {name} already approved")
        return
    ocp.run(kubeconfig,
            ["patch", "installplan", name, "-n", namespace, "--type", "merge",
             "-p", '{"spec":{"approved":true}}'],
            log=job.log)
    job.log(f"  InstallPlan {name} approved")


# ============================================================ uninstall

def uninstall(job: Job, kubeconfig: str, spec: UninstallSpec) -> None:
    ns = spec.namespace
    job.log(f"Uninstalling {spec.kind} from namespace '{ns}'")
    job.log("")
    job.log("WHAT THIS DELETES:")
    job.log(f"  workload objects      : yes")
    job.log(f"  PersistentVolumeClaims: {'YES -- DATA IS DESTROYED' if spec.delete_pvc else 'no (kept)'}")
    job.log(f"  namespace             : {'YES -- EVERYTHING IN IT' if spec.delete_namespace else 'no (kept)'}")

    if spec.kind == "enterprise":
        job.step(1, 4, "Deleting databases")
        ocp.run(kubeconfig, ["delete", "redb", "--all", "-n", ns, "--ignore-not-found"],
                check=False, timeout=300, log=job.log)
        job.step(2, 4, "Deleting the cluster")
        ocp.run(kubeconfig, ["delete", "rec", "--all", "-n", ns, "--ignore-not-found"],
                check=False, timeout=600, log=job.log)
        job.step(3, 4, "Removing the operator")
        for kind in ("subscription", "csv", "operatorgroup"):
            ocp.run(kubeconfig, ["delete", kind, "--all", "-n", ns, "--ignore-not-found"],
                    check=False, timeout=180, log=job.log)
        step = 4
    else:
        job.step(1, 2, "Deleting workload objects")
        if spec.managed:
            # deployed by this app -> everything carries our label
            for kind in ("deployment", "statefulset", "service", "configmap",
                         "secret", "networkpolicy"):
                ocp.run(kubeconfig,
                        ["delete", kind, "-n", ns, "-l",
                         "app.kubernetes.io/managed-by=redis-deployer", "--ignore-not-found"],
                        check=False, timeout=180, log=job.log)
        else:
            # deployed by someone else -> only touch objects we can name exactly
            job.log("  release was not created by this app: deleting named objects only")
            targets = [
                (spec.workload, spec.name),
                ("service", spec.name),
                ("configmap", f"{spec.name}-config"),
                ("secret", f"{spec.name}-auth"),
            ]
            for kind, name in targets:
                ocp.run(kubeconfig, ["delete", kind, name, "-n", ns, "--ignore-not-found"],
                        check=False, timeout=180, log=job.log)
            job.log("  anything else in this namespace is left alone")
        step = 2

    job.step(step, step, "Storage and namespace")
    if spec.delete_pvc:
        ocp.run(kubeconfig, ["delete", "pvc", "--all", "-n", ns, "--ignore-not-found"],
                check=False, timeout=300, log=job.log)
        job.log("  PVCs deleted -- if the StorageClass reclaimPolicy is Delete, the disks are gone")
    else:
        p = ocp.run(kubeconfig, ["get", "pvc", "-n", ns], check=False, log=job.log)
        job.log("  PVCs kept. Re-deploying into this namespace will reuse them.")

    if spec.delete_namespace:
        ocp.run(kubeconfig, ["delete", "namespace", ns, "--ignore-not-found"],
                check=False, timeout=300, log=job.log)

    job.log("")
    job.log("Check for orphaned volumes:  oc get pv | grep -i released")


# ============================================================ generic operator

def install_operator(job: Job, kubeconfig: str, spec: OperatorInstallSpec) -> None:
    total = 4
    job.log(f"Installing operator '{spec.package}' from {spec.catalog_source}")
    job.log(f"Channel {spec.channel} | mode {spec.install_mode} | namespace {spec.namespace}")
    job.log(f"Approval: {spec.approval}")
    job.log("")
    job.log("This installs the OPERATOR only. It creates no custom resources, so "
            "nothing is running yet -- you still have to create the CRs it provides.")

    job.step(1, total, "Rendering manifests")
    objs = operator_install(spec)
    yaml_text = dump(objs)
    job.result["manifests"] = yaml_text
    job.log("  " + ", ".join(o["kind"] for o in objs))

    job.step(2, total, "Applying")
    apply_manifests(job, kubeconfig, objs)

    job.step(3, total, "Approving the InstallPlan")
    _approve_installplan(job, kubeconfig, spec.namespace, spec.package)

    job.step(4, total, "Waiting for the ClusterServiceVersion")
    csv_name = ""
    deadline = time.time() + 900
    while time.time() < deadline:
        csv_name = ocp.jsonpath(
            kubeconfig, ["get", "subscription", spec.package, "-n", spec.namespace],
            "{.status.installedCSV}") or ocp.jsonpath(
            kubeconfig, ["get", "subscription", spec.package, "-n", spec.namespace],
            "{.status.currentCSV}")
        if csv_name:
            break
        time.sleep(5)
    if not csv_name:
        raise RuntimeError("Subscription never reported a CSV")

    ocp.wait_for(kubeconfig, ["get", "csv", csv_name, "-n", spec.namespace],
                 "{.status.phase}", "Succeeded",
                 timeout=900, label="CSV phase", log=job.log)

    crds = ocp.run(kubeconfig,
                   ["get", "csv", csv_name, "-n", spec.namespace, "-o",
                    "jsonpath={range .spec.customresourcedefinitions.owned[*]}"
                    "{.kind}{\" \"}{.name}{\"\\n\"}{end}"],
                   check=False, log=job.log)

    job.result.update({"kind": "operator", "package": spec.package,
                       "namespace": spec.namespace, "csv": csv_name,
                       "crds": (crds.stdout or "").strip()})
    job.log("")
    job.log(f"Operator ready: {csv_name}")
    job.log("Custom resources it now provides:")
    for line in (crds.stdout or "").splitlines():
        job.log("  " + line)


def cleanup_crds(job: Job, kubeconfig: str, names: list[str]) -> None:
    job.log("Removing leftover CustomResourceDefinitions")
    job.log("")
    job.log("CRDs are CLUSTER-SCOPED. Deleting one removes every object of that")
    job.log("type in EVERY namespace, cluster-wide. Only do this when no Redis")
    job.log("operator remains installed.")
    job.log("")
    for i, name in enumerate(names, 1):
        p = ocp.run(kubeconfig, ["get", name, "-A", "--no-headers"],
                    check=False, timeout=45)
        live = len([l for l in (p.stdout or "").splitlines() if l.strip()])
        if live:
            job.log(f"[{i}/{len(names)}] SKIP {name} -- {live} object(s) still exist")
            continue
        job.step(i, len(names), f"Deleting CRD {name}")
        ocp.run(kubeconfig, ["delete", "crd", name, "--ignore-not-found"],
                check=False, timeout=120, log=job.log)
    job.log("")
    job.log("Remaining redis-related CRDs:")
    p = ocp.run(kubeconfig, ["get", "crd", "-o", "name"], check=False, timeout=60)
    rest = [l for l in (p.stdout or "").splitlines() if "redis" in l.lower()]
    for line in rest or ["  (none)"]:
        job.log("  " + line)


def _expose_ui(job: Job, kubeconfig: str, spec: EnterpriseSpec) -> None:
    """Create the console Route and collect its URL and admin credentials."""
    import base64 as _b64

    secret = ocp.jsonpath(kubeconfig,
                          ["get", "rec", spec.rec_name, "-n", spec.namespace],
                          "{.status.clusterCredentialSecretName}") or spec.rec_name
    user = pw = ""
    raw = ocp.run(kubeconfig, ["get", "secret", secret, "-n", spec.namespace,
                               "-o", "jsonpath={.data.username}"], check=False).stdout.strip()
    if raw:
        user = _b64.b64decode(raw).decode()
    raw = ocp.run(kubeconfig, ["get", "secret", secret, "-n", spec.namespace,
                               "-o", "jsonpath={.data.password}"], check=False).stdout.strip()
    if raw:
        pw = _b64.b64decode(raw).decode()
    job.result.update({"ui_user": user, "ui_password": pw, "ui_secret": secret})

    if not spec.expose_ui:
        job.log("")
        job.log("Management console not exposed. Reach it with a port-forward:")
        job.log(f"  oc port-forward -n {spec.namespace} svc/{spec.rec_name}-ui 8443:8443")
        job.log("  then open https://localhost:8443")
        return

    job.log("")
    job.log("Exposing the management console via a passthrough Route")
    ocp.apply_yaml(kubeconfig, dump(enterprise_ui_route(spec)), log=job.log)
    host = ocp.jsonpath(kubeconfig,
                        ["get", "route", f"{spec.rec_name}-ui", "-n", spec.namespace],
                        "{.spec.host}")
    if host:
        job.result["ui_url"] = f"https://{host}"
        job.log(f"  console: https://{host}")
        job.log("  expect a certificate warning -- passthrough serves the operator's")
        job.log("  self-signed cert, not your cluster wildcard")


# ============================================================ opstree operator

def _wait_pods_ready(job: Job, kubeconfig: str, ns: str, expected: int,
                     timeout: int = 900) -> int:
    """Poll until `expected` pods report every container ready.

    Community operators do not expose a consistent status field, so counting
    ready pods is more reliable than guessing a jsonpath.
    """
    deadline = time.time() + timeout
    last = -1
    while time.time() < deadline:
        p = ocp.run(kubeconfig, ["get", "pods", "-n", ns, "--no-headers"],
                    check=False, timeout=45)
        ready = 0
        for line in (p.stdout or "").splitlines():
            parts = line.split()
            if len(parts) < 3 or parts[2] != "Running":
                continue
            got, want = (parts[1].split("/") + ["0"])[:2]
            if got == want and got != "0":
                ready += 1
        if ready != last:
            job.log(f"    pods ready: {ready}/{expected}")
            last = ready
        if ready >= expected:
            return ready
        time.sleep(10)
    raise TimeoutError(f"only {last} of {expected} pods became ready")


def deploy_opstree(job: Job, kubeconfig: str, spec: OpstreeSpec) -> None:
    topo = opstree_topology(spec.topology) or {}
    total = 6
    password = spec.password or gen_password()

    job.log(f"Redis via the Opstree operator -- topology: {topo.get('label')}")
    job.log(f"  {topo.get('detail')}")
    job.log(f"Namespace {spec.namespace} | name {spec.name} | image {ot.image_for(spec)}")
    if topo.get("client_aware"):
        job.log("")
        job.log(f"!! Clients must be {topo['client_aware']}. A plain redis-cli pointed at")
        job.log("!! a single host will not work correctly against this topology.")

    job.step(1, total, "Pre-flight")
    pods_wanted = {"standalone": 1, "replication": spec.size,
                   "sentinel": spec.size, "cluster": spec.size * 2}[spec.topology]
    p = ocp.run(kubeconfig, ["get", "nodes", "-l", "node-role.kubernetes.io/worker=",
                             "--no-headers"], check=False, log=job.log)
    workers = len([l for l in (p.stdout or "").splitlines() if l.strip()])
    job.log(f"  topology needs ~{pods_wanted} pod(s); {workers} worker nodes available")
    if spec.topology == "cluster" and spec.size < 3:
        raise RuntimeError("Redis Cluster needs at least 3 leader shards")
    if spec.topology == "sentinel" and spec.size % 2 == 0:
        job.log("  WARNING: an even number of sentinels cannot break a tie -- use an odd count")

    job.step(2, total, "Ensuring the operator is installed")
    crd = f"{ {'Redis': 'redis', 'RedisReplication': 'redisreplications', 'RedisSentinel': 'redissentinels', 'RedisCluster': 'redisclusters'}[topo['kind']] }.{OPSTREE_GROUP}"
    have = ocp.run(kubeconfig, ["get", "crd", crd], check=False, timeout=45).returncode == 0
    if have:
        job.log(f"  {crd} already present")
    elif not spec.install_operator:
        raise RuntimeError(f"CRD {crd} not found and operator install was not requested")
    else:
        job.log(f"  {crd} missing -- installing {OPSTREE_PACKAGE}")
        op = OperatorInstallSpec(
            package=OPSTREE_PACKAGE, catalog_source="community-operators",
            channel=spec.operator_channel, install_mode="AllNamespaces",
            namespace=spec.operator_namespace, approval="Manual")
        apply_manifests(job, kubeconfig, operator_install(op))
        _approve_installplan(job, kubeconfig, op.namespace, op.package)
        job.log("  waiting for the CRD to be registered")
        deadline = time.time() + 600
        while time.time() < deadline:
            if ocp.run(kubeconfig, ["get", "crd", crd], check=False,
                       timeout=30).returncode == 0:
                break
            time.sleep(5)
        else:
            raise TimeoutError(f"CRD {crd} never appeared after installing the operator")
        job.log(f"  {crd} registered")

    job.step(3, total, "Checking the CR against the installed CRD schema")
    schema = ot.crd_fields(kubeconfig, topo["kind"])
    job.result["crd_schema"] = schema
    if schema.get("present"):
        job.log(f"  {schema['crd']} served versions: {', '.join(schema['served_versions'])}")
        job.log(f"  spec fields this version accepts: {', '.join(schema['spec_fields'])}")
        objs = ot.manifests(spec, password)
        cr = [o for o in objs if o["kind"] == topo["kind"]][0]
        unknown = [k for k in cr["spec"] if k not in schema["spec_fields"]]
        if unknown:
            job.log(f"  WARNING: fields not in this CRD version: {', '.join(unknown)}")
            job.log("  the dry run below will reject them if they are genuinely invalid")

    job.step(4, total, "Applying")
    objs = ot.manifests(spec, password)
    if spec.allow_namespaces:
        # cluster gossips on port+10000; sentinel listens on 26379
        peer_ports = {"cluster": [16379], "sentinel": [26379]}.get(spec.topology, [])
        objs.append(network_policy(spec.namespace, {"app": spec.name}, 6379,
                                   spec.allow_namespaces,
                                   f"allow-clients-to-{spec.name}",
                                   peer_ports=peer_ports))
        job.log(f"  NetworkPolicy: clients from {', '.join(spec.allow_namespaces)}"
                f" + peer traffic between the pods themselves")
    job.result["manifests"] = dump(objs)
    try:
        apply_manifests(job, kubeconfig, objs)
    except ocp.OcError as exc:
        raise RuntimeError(
            "The API server rejected the custom resource. Community operator field "
            f"names shift between releases -- check `oc explain {topo['kind'].lower()}.spec`. "
            f"Detail: {exc}") from exc

    job.step(5, total, "Waiting for pods")
    try:
        _wait_pods_ready(job, kubeconfig, spec.namespace, pods_wanted)
    except Exception:
        job.log("")
        job.log("Pods did not all become ready. Recent events:")
        job.log(ocp.recent_events(kubeconfig, spec.namespace, 30))
        raise

    job.step(6, total, "Collecting connection details")
    p = ocp.run(kubeconfig, ["get", "svc", "-n", spec.namespace, "--no-headers"],
                check=False, log=job.log)
    services = []
    for line in (p.stdout or "").splitlines():
        parts = line.split()
        if parts:
            services.append({"name": parts[0], "type": parts[1], "ports": parts[4]})

    domain = ocp.cluster_domain(kubeconfig, spec.namespace, log=job.log)
    primary = next((s["name"] for s in services
                    if s["name"].endswith(("-master", "-leader"))), spec.name)

    notes = [f"Topology: {topo['label']}"]
    if topo.get("client_aware"):
        notes.append(f"Clients MUST be {topo['client_aware']}.")
    if spec.topology == "cluster":
        notes.append("Only database 0; no multi-key operations across hash slots.")
    if spec.topology == "replication":
        notes.append("No automatic failover on its own -- deploy a RedisSentinel "
                     "pointed at this RedisReplication to get it.")

    job.result.update({
        "kind": "opstree", "topology": spec.topology, "namespace": spec.namespace,
        "name": spec.name, "host": f"{primary}.{spec.namespace}.svc.{domain}",
        "port": 6379, "password": password if spec.auth_enabled else None,
        "cluster_domain": domain, "services": services, "notes": notes,
        "restricted_to": spec.allow_namespaces,
    })
    job.log("")
    job.log("Services created by the operator:")
    for sv in services:
        job.log(f"  {sv['name']:32s} {sv['type']:12s} {sv['ports']}")
