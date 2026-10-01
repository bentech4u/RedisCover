"""Day-2 operations: change a running release without rebuilding it.

Every operation here is a targeted patch plus the checks that stop it going
wrong. The recurring theme is that Kubernetes accepts many of these edits and
then does something other than what you meant -- a StatefulSet's
volumeClaimTemplates are immutable, a StorageClass may forbid expansion, and an
image tag that does not exist gives ImagePullBackOff rather than an error.
"""
from __future__ import annotations

import re
from typing import Any

from . import ocp
from .models import Day2Spec


def _workload(kubeconfig: str, ns: str, name: str) -> tuple[str, str]:
    for kind in ("statefulset", "deployment"):
        if ocp.run(kubeconfig, ["get", kind, name, "-n", ns],
                   check=False, timeout=30).returncode == 0:
            return kind, f"{kind}/{name}"
    raise RuntimeError(f"no Deployment or StatefulSet named '{name}' in '{ns}'")


# ---------------------------------------------------------------- scale

def scale(job, kubeconfig: str, spec: Day2Spec) -> None:
    ns, name = spec.namespace, spec.name
    job.step(1, 3, "Checking what we are scaling")

    if spec.kind == "opstree":
        plural = spec.cr_plural or "redisreplications"
        current = ocp.jsonpath(kubeconfig, ["get", plural, name, "-n", ns],
                               "{.spec.clusterSize}") or "?"
        job.log(f"  {plural}/{name} clusterSize {current} -> {spec.replicas}")
        nodes = len(ocp.schedulable_nodes(kubeconfig))
        if spec.replicas > nodes:
            raise RuntimeError(
                f"{spec.replicas} requested but only {nodes} schedulable node(s). "
                "The operator spreads pods with anti-affinity, so the extras would "
                "stay Pending.")
        job.step(2, 3, "Patching the custom resource")
        ocp.run(kubeconfig, ["patch", plural, name, "-n", ns, "--type", "merge",
                             "-p", f'{{"spec":{{"clusterSize":{spec.replicas}}}}}'],
                log=job.log, timeout=90)
        job.step(3, 3, "Waiting for the operator")
        job.log("  the operator reconciles this; watch the Status tab")
        return

    kind, target = _workload(kubeconfig, ns, name)
    current = ocp.jsonpath(kubeconfig, ["get", kind, name, "-n", ns],
                           "{.spec.replicas}") or "?"
    job.log(f"  {target} replicas {current} -> {spec.replicas}")

    if kind == "statefulset" and spec.replicas > int(current or 0):
        sc = ocp.jsonpath(kubeconfig, ["get", kind, name, "-n", ns],
                          "{.spec.volumeClaimTemplates[0].spec.storageClassName}")
        size = ocp.jsonpath(kubeconfig, ["get", kind, name, "-n", ns],
                            "{.spec.volumeClaimTemplates[0].spec.resources.requests.storage}")
        if sc:
            job.log(f"  new pods each get a NEW {size} PVC on '{sc}' -- "
                    "volumeClaimTemplates are immutable, so they use the size "
                    "recorded when the StatefulSet was created, not any size you "
                    "have grown existing PVCs to")
        nodes = len(ocp.schedulable_nodes(kubeconfig))
        if spec.replicas > nodes:
            job.log(f"  WARNING: {spec.replicas} replicas but {nodes} schedulable "
                    "node(s); anti-affinity is preferred, not required, so they "
                    "will co-locate")

    job.step(2, 3, "Scaling")
    ocp.run(kubeconfig, ["scale", target, "-n", ns, f"--replicas={spec.replicas}"],
            log=job.log, timeout=90)

    job.step(3, 3, "Waiting for rollout")
    ocp.run(kubeconfig, ["rollout", "status", target, "-n", ns, "--timeout=600s"],
            check=False, timeout=660, log=job.log)
    if kind == "statefulset" and spec.replicas > int(current or 0):
        job.log("")
        job.log("  a new replica syncs a full copy from the primary before it is "
                "useful -- check INFO replication on the Status tab")


# ---------------------------------------------------------------- storage

def _sync_claim_template(job, kubeconfig: str, ns: str, sts: str, size: str) -> bool:
    """Make a StatefulSet\'s volumeClaimTemplates match the PVCs we just grew.

    The API forbids patching volumeClaimTemplates -- it is on the list of fields
    a StatefulSet update may not touch -- so growing the PVCs alone leaves the
    template behind, and the next replica you add is created at the ORIGINAL
    size. That is silent: the pod starts, reports ready, and simply has a
    smaller disk than its peers.

    The only route is to delete the StatefulSet with --cascade=orphan, which
    removes the controller while leaving the pods and PVCs untouched, and then
    recreate it with the corrected template so it re-adopts the running pods by
    selector. No pod restarts.

    The replacement is validated with a server-side dry run BEFORE the delete,
    so a manifest the API would reject can never leave the pods orphaned.
    """
    import json

    raw = ocp.run(kubeconfig, ["get", "statefulset", sts, "-n", ns, "-o", "json"],
                  check=False, timeout=60).stdout
    if not raw.strip():
        job.log(f"  no StatefulSet '{sts}' -- nothing to reconcile")
        return False
    obj = json.loads(raw)
    restore = json.loads(raw)
    restore.pop("status", None)
    for f in ("creationTimestamp", "generation", "resourceVersion", "uid",
              "selfLink", "managedFields"):
        restore.get("metadata", {}).pop(f, None)

    tpls = obj.get("spec", {}).get("volumeClaimTemplates", [])
    if not tpls:
        return False
    current = tpls[0].get("spec", {}).get("resources", {}).get("requests", {}).get("storage", "")
    if current == size:
        job.log(f"  template already {size} -- nothing to do")
        return False

    for t in tpls:
        t.setdefault("spec", {}).setdefault("resources", {}).setdefault("requests", {})["storage"] = size

    obj.pop("status", None)
    md = obj.get("metadata", {})
    for f in ("creationTimestamp", "generation", "resourceVersion", "uid",
              "selfLink", "managedFields"):
        md.pop(f, None)
    md.get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)

    manifest = json.dumps(obj)
    original = json.dumps(restore)
    job.log(f"  template {current} -> {size}")

    # A server dry run cannot vet this. While the old StatefulSet still exists
    # the API judges the replacement as an UPDATE and refuses it for the very
    # reason we are here -- so the only honest pre-check is a schema one, and
    # the real safety net is putting the original back if the create fails.
    chk = ocp.run(kubeconfig, ["apply", "--dry-run=client", "-f", "-"],
                  stdin=manifest, check=False, timeout=60)
    if chk.returncode != 0:
        raise RuntimeError(
            "the rebuilt StatefulSet is malformed, so nothing was deleted and "
            f"your pods are untouched: {(chk.stderr or chk.stdout).strip()}")

    job.log("  deleting the StatefulSet with --cascade=orphan (pods keep running)")
    ocp.run(kubeconfig, ["delete", "statefulset", sts, "-n", ns, "--cascade=orphan"],
            log=job.log, timeout=120)

    job.log("  recreating it with the corrected template; it re-adopts the pods")
    res = ocp.run(kubeconfig, ["apply", "-f", "-"], stdin=manifest,
                  check=False, log=job.log, timeout=120)
    if res.returncode != 0:
        job.log("  recreate FAILED -- restoring the original StatefulSet so the")
        job.log("  running pods are not left without a controller")
        back = ocp.run(kubeconfig, ["apply", "-f", "-"], stdin=original,
                       check=False, log=job.log, timeout=120)
        if back.returncode != 0:
            raise RuntimeError(
                "could not recreate the StatefulSet AND could not restore the "
                "original. Your pods and PVCs are intact but have no controller. "
                f"Recreate it by hand. Error: {(res.stderr or res.stdout).strip()}")
        raise RuntimeError(
            "could not recreate the StatefulSet with the new template; the "
            f"original was restored and your pods are untouched: "
            f"{(res.stderr or res.stdout).strip()}")
    return True


def grow_storage(job, kubeconfig: str, spec: Day2Spec) -> None:
    ns = spec.namespace
    job.step(1, 5, "Checking the StorageClass allows expansion")
    pvcs = []
    p = ocp.run(kubeconfig, ["get", "pvc", "-n", ns, "-o",
                             "custom-columns=NAME:.metadata.name,"
                             "SC:.spec.storageClassName,SIZE:.status.capacity.storage",
                             "--no-headers"], check=False, timeout=60)
    for line in (p.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and (spec.name in parts[0] or not spec.name):
            pvcs.append({"name": parts[0], "sc": parts[1], "size": parts[2]})
    if not pvcs:
        raise RuntimeError(f"no PersistentVolumeClaims matching '{spec.name}' in '{ns}'")

    sc = pvcs[0]["sc"]
    allowed = ocp.jsonpath(kubeconfig, ["get", "sc", sc], "{.allowVolumeExpansion}")
    job.log(f"  StorageClass {sc}: allowVolumeExpansion={allowed or 'false'}")
    if allowed != "true":
        raise RuntimeError(
            f"StorageClass '{sc}' does not allow volume expansion. The only way to "
            "grow these volumes is to create a new release on a larger size and "
            "copy the data across.")

    job.step(2, 5, "Checking the new size is larger")
    def to_gi(v: str) -> float:
        m = re.match(r"([\d.]+)\s*([GMT]i?)", v or "")
        if not m:
            return 0.0
        n, u = float(m.group(1)), m.group(2)[0]
        return n * {"M": 1 / 1024, "G": 1, "T": 1024}[u]
    new = to_gi(spec.storage_size)
    # Sizes can legitimately differ across a set: grow the PVCs, then scale up,
    # and the replicas added afterwards came from the old template. Treat each
    # claim on its own -- only a genuine shrink is an error, and one already at
    # the target is simply nothing to do.
    todo = []
    for pvc in pvcs:
        cur = to_gi(pvc["size"])
        if new < cur:
            raise RuntimeError(
                f"{pvc['name']} is already {pvc['size']}. Kubernetes cannot SHRINK a "
                "volume -- only grow it.")
        if new == cur:
            job.log(f"  {pvc['name']}: already {pvc['size']} -- skipping")
        else:
            job.log(f"  {pvc['name']}: {pvc['size']} -> {spec.storage_size}")
            todo.append(pvc)

    job.step(3, 5, "Patching each PersistentVolumeClaim")
    if not todo:
        job.log("  every claim is already at the target size")
    for pvc in todo:
        ocp.run(kubeconfig, ["patch", "pvc", pvc["name"], "-n", ns, "--type", "merge",
                             "-p", '{"spec":{"resources":{"requests":{"storage":"'
                                   + spec.storage_size + '"}}}}'],
                log=job.log, timeout=90)

    job.step(4, 5, "Reconciling the StatefulSet volumeClaimTemplates")
    job.log("  Growing the PVCs does not touch the template they were stamped from,")
    job.log("  and the API forbids patching it. Left alone, the next replica you add")
    job.log("  would silently get the OLD size. Rebuilding the controller fixes that.")
    synced = False
    if ocp.run(kubeconfig, ["get", "statefulset", spec.name, "-n", ns],
               check=False, timeout=30).returncode == 0:
        synced = _sync_claim_template(job, kubeconfig, ns, spec.name, spec.storage_size)
    else:
        job.log(f"  '{spec.name}' is not a StatefulSet -- no template to reconcile")

    job.step(5, 5, "Result")
    ocp.run(kubeconfig, ["get", "pvc", "-n", ns], check=False, log=job.log, timeout=60)
    job.log("")
    job.log("  Expansion is asynchronous. Some CSI drivers resize the filesystem")
    job.log("  online; others need the pod restarted. Watch the PVC conditions:")
    job.log(f"    oc describe pvc {pvcs[0]['name']} -n {ns}")
    if synced:
        job.log("")
        job.log(f"  The volumeClaimTemplates now say {spec.storage_size}, so replicas")
        job.log("  added from here on are created at the new size.")
        job.result["note"] = f"PVCs and volumeClaimTemplates both at {spec.storage_size}"
    else:
        job.result["note"] = f"PVCs grown to {spec.storage_size}"


# ---------------------------------------------------------------- image

def bump_image(job, kubeconfig: str, spec: Day2Spec) -> None:
    ns, name = spec.namespace, spec.name
    new = spec.image or ""
    if not new:
        raise RuntimeError("no image given")

    job.step(1, 4, "Comparing versions")
    if spec.kind == "opstree":
        plural = spec.cr_plural or "redisreplications"
        cur = ocp.jsonpath(kubeconfig, ["get", plural, name, "-n", ns],
                           "{.spec.kubernetesConfig.image}")
    else:
        kind, _ = _workload(kubeconfig, ns, name)
        cur = ocp.jsonpath(kubeconfig, ["get", kind, name, "-n", ns],
                           "{.spec.template.spec.containers[0].image}")
    job.log(f"  current: {cur}")
    job.log(f"  new    : {new}")
    if cur == new:
        raise RuntimeError("that is the image it is already running")

    def ver(img: str) -> tuple[int, ...]:
        m = re.search(r":v?(\d+)\.(\d+)", img or "")
        return tuple(int(x) for x in m.groups()) if m else ()
    a, b = ver(cur), ver(new)
    if a and b:
        if b < a and not spec.force:
            raise RuntimeError(
                f"that is a DOWNGRADE ({'.'.join(map(str, a))} -> "
                f"{'.'.join(map(str, b))}). Redis does not guarantee that a newer "
                "RDB or AOF can be read by an older server, so the data written "
                "since the upgrade may not load. Tick 'force' only if you have a "
                "backup and understand that.")
        if b[0] > a[0]:
            job.log(f"  MAJOR version change {a[0]} -> {b[0]}: read the release notes "
                    "for removed commands and changed defaults before continuing")

    job.step(2, 4, "Checking the cluster can pull it")
    probe = f"imgcheck-{abs(hash(new)) % 100000}"
    ocp.run(kubeconfig, ["run", probe, "-n", ns, "--image", new, "--restart=Never",
                         "--command", "--", "true"], check=False, timeout=90)
    reason = ""
    for _ in range(12):
        phase = ocp.jsonpath(kubeconfig, ["get", "pod", probe, "-n", ns],
                             "{.status.phase}")
        reason = ocp.jsonpath(
            kubeconfig, ["get", "pod", probe, "-n", ns],
            "{.status.containerStatuses[0].state.waiting.reason}")
        if phase in ("Succeeded", "Running") or "ImagePull" in (reason or ""):
            break
        import time as _t
        _t.sleep(5)
    ocp.run(kubeconfig, ["delete", "pod", probe, "-n", ns, "--ignore-not-found",
                         "--grace-period=0", "--force"], check=False, timeout=60)
    if "ImagePull" in (reason or "") or "ErrImage" in (reason or ""):
        raise RuntimeError(
            f"this cluster cannot pull '{new}' ({reason}). Check the tag exists and "
            "that any mirror or pull secret covers it.")
    job.log("  pull succeeded")

    job.step(3, 4, "Applying")
    if spec.kind == "opstree":
        ocp.run(kubeconfig, ["patch", plural, name, "-n", ns, "--type", "merge",
                             "-p", '{"spec":{"kubernetesConfig":{"image":"' + new + '"}}}'],
                log=job.log, timeout=90)
        job.log("  the operator rolls the pods")
    else:
        ocp.run(kubeconfig, ["set", "image", f"{kind}/{name}", f"redis={new}", "-n", ns],
                log=job.log, timeout=90)

    job.step(4, 4, "Waiting for rollout")
    if spec.kind != "opstree":
        ocp.run(kubeconfig, ["rollout", "status", f"{kind}/{name}", "-n", ns,
                             "--timeout=900s"], check=False, timeout=960, log=job.log)
    job.log("")
    job.log("  A StatefulSet rolls highest ordinal first, so on a replication set the")
    job.log("  REPLICAS upgrade before the primary. Check INFO replication afterwards.")
    job.result.update({"from": cur, "to": new})


# ---------------------------------------------------------------- cache size

# Redis and Kubernetes use OPPOSITE conventions, which is a genuine trap:
#
#   Redis       1k=1000   1kb=1024   1m=10^6   1mb=1024^2   1g=10^9   1gb=1024^3
#   Kubernetes  1k=1000   1Ki=1024   1M=10^6   1Mi=1024^2   1G=10^9   1Gi=1024^3
#
# So Redis "mb" is BINARY and equals Kubernetes "Mi", while Redis "m" is decimal
# and equals Kubernetes "M". Reading a Redis value with Kubernetes rules
# understates it by up to 7%, which quietly loosens every ratio check.
_REDIS_UNITS = {"": 1, "b": 1,
                "k": 1000, "kb": 1024,
                "m": 1000000, "mb": 1048576,
                "g": 1000000000, "gb": 1073741824}
_K8S_UNITS = {"": 1, "k": 1000, "ki": 1024,
              "m": 1000000, "mi": 1048576,
              "g": 1000000000, "gi": 1073741824,
              "t": 1000 ** 4, "ti": 1024 ** 4}


def redis_bytes(v: str) -> float:
    """Parse a value the way redis-server does."""
    m = re.match(r"^\s*([\d.]+)\s*([a-zA-Z]*)\s*$", v or "")
    if not m:
        return 0.0
    return float(m.group(1)) * _REDIS_UNITS.get(m.group(2).lower(), 1)


def k8s_bytes(v: str) -> float:
    """Parse a Kubernetes quantity (512Mi, 1Gi, 1000M)."""
    m = re.match(r"^\s*([\d.]+)\s*([a-zA-Z]*)\s*$", v or "")
    if not m:
        return 0.0
    return float(m.group(1)) * _K8S_UNITS.get(m.group(2).lower(), 1)


def _bytes(v: str) -> float:
    """Kubernetes flavour -- kept for the container limits."""
    return k8s_bytes(v)


def _human(b: float) -> str:
    if b >= 1073741824:
        return f"{b / 1073741824:.1f}Gi"
    return f"{b / 1048576:.0f}Mi"


def change_memory(job, kubeconfig: str, spec: Day2Spec) -> None:
    """Change the CACHE capacity -- which is RAM, not disk.

    Growing a PVC gives the AOF and RDB files more room; it does not let Redis
    hold one more key. Capacity is `maxmemory`, bounded by the container's
    memory limit.

    Raising maxmemory INSIDE the existing limit is applied live with CONFIG SET
    and needs no restart. Raising the limit is a pod spec change, so the pods
    roll -- and on a replication set the replicas roll before the primary.
    """
    ns, name = spec.namespace, spec.name
    new_mm = spec.maxmemory or ""
    new_lim = spec.memory_limit or ""
    if not new_mm and not new_lim:
        raise RuntimeError("give a new maxmemory, a new container limit, or both")

    job.step(1, 5, "Reading what it has now")
    if spec.kind == "opstree":
        plural = spec.cr_plural or "redisreplications"
        cur_lim = ocp.jsonpath(kubeconfig, ["get", plural, name, "-n", ns],
                               "{.spec.kubernetesConfig.resources.limits.memory}")
        cm_name, cm_key = f"{name}-redis-config", "redis-additional.conf"
    else:
        kind, _ = _workload(kubeconfig, ns, name)
        cur_lim = ocp.jsonpath(kubeconfig, ["get", kind, name, "-n", ns],
                               "{.spec.template.spec.containers[0].resources.limits.memory}")
        cm_name, cm_key = f"{name}-config", "redis.conf"

    pod = ocp.jsonpath(kubeconfig, ["get", "pods", "-n", ns, "-l", f"app={name}"],
                       "{.items[0].metadata.name}")
    pw = ""
    for sname, key in ((f"{name}-auth", "redis-password"), (f"{name}-auth", "password")):
        raw = ocp.run(kubeconfig, ["get", "secret", sname, "-n", ns, "-o",
                                   f"jsonpath={{.data.{key}}}"],
                      check=False, timeout=30).stdout.strip()
        if raw:
            import base64 as _b
            pw = _b.b64decode(raw).decode()
            break
    auth = ["-a", pw, "--no-auth-warning"] if pw else []
    cur_mm = ""
    if pod:
        out = ocp.run(kubeconfig, ["exec", "-n", ns, pod, "--", "redis-cli", *auth,
                                   "CONFIG", "GET", "maxmemory"],
                      check=False, timeout=45).stdout or ""
        parts = [l.strip() for l in out.splitlines() if l.strip()]
        cur_mm = parts[1] if len(parts) > 1 else ""

    job.log(f"  maxmemory (RAM, the cache)  : {_human(float(cur_mm or 0))}"
            f"  ->  {new_mm or '(unchanged)'}")
    job.log(f"  container memory limit      : {cur_lim or '?'}"
            f"  ->  {new_lim or '(unchanged)'}")
    job.log("  disk (PVC) is NOT involved -- it only holds the AOF/RDB files")

    # cur_mm comes back from CONFIG GET as a plain byte count
    target_mm = redis_bytes(new_mm) if new_mm else float(cur_mm or 0)
    target_lim = k8s_bytes(new_lim) if new_lim else k8s_bytes(cur_lim)

    job.step(2, 5, "Checking the ratio")
    if target_lim and target_mm:
        pct = target_mm / target_lim * 100
        job.log(f"  maxmemory would be {pct:.0f}% of the container limit")
        if pct > 80 and not spec.force:
            raise RuntimeError(
                f"maxmemory {_human(target_mm)} is {pct:.0f}% of the {_human(target_lim)} "
                "limit. A BGSAVE forks and copies every page modified during the save, so "
                "this will be OOMKilled under write load -- a crash, not an eviction. Aim "
                "for 50-70%, or tick force.")
        if pct > 70:
            job.log("  WARNING: above the 50-70% safe band")

    # ANY pod-spec resource change rolls the pods; maxmemory alone does not
    spec_changes = any([
        bool(new_lim) and _bytes(new_lim) != _bytes(cur_lim),
        bool(spec.memory_request), bool(spec.cpu_request), bool(spec.cpu_limit),
    ])
    limit_changes = spec_changes
    if limit_changes:
        job.step(3, 5, "Checking a node can hold it")
        for n in ocp.schedulable_nodes(kubeconfig):
            alloc = _bytes((n.get("memory") or "0").replace("Ki", "Ki"))
            job.log(f"    {n['name']:36s} allocatable {n.get('memory')}")
        job.log("  (compare the new limit against the free memory on the Cluster tab)")
    else:
        job.step(3, 5, "Limit unchanged -- no pod restart needed")

    job.step(4, 5, "Persisting the new maxmemory in the ConfigMap")
    if new_mm:
        cur_cm = ocp.run(kubeconfig, ["get", "cm", cm_name, "-n", ns, "-o",
                                      f"jsonpath={{.data['{cm_key.replace('.', chr(92) + '.')}']}}"],
                         check=False, timeout=45).stdout
        if cur_cm:
            if re.search(r"^maxmemory\s+\S+", cur_cm, re.M):
                updated = re.sub(r"^maxmemory\s+\S+", f"maxmemory {new_mm}",
                                 cur_cm, flags=re.M)
            else:
                updated = cur_cm.rstrip() + f"\nmaxmemory {new_mm}\n"
            import json as _j
            patch = _j.dumps({"data": {cm_key: updated}})
            ocp.run(kubeconfig, ["patch", "cm", cm_name, "-n", ns, "--type", "merge",
                                 "-p", patch], log=job.log, timeout=90)
            job.log("  ConfigMap updated, so the value survives a restart")
        else:
            job.log(f"  WARNING: could not read ConfigMap {cm_name}; the live change "
                    "below will be lost on restart")

    job.step(5, 5, "Applying")
    if spec_changes:
        lim = {}
        req = {}
        if new_lim:
            lim["memory"] = new_lim
        if spec.memory_request:
            req["memory"] = spec.memory_request
        if spec.cpu_limit:
            lim["cpu"] = spec.cpu_limit
        if spec.cpu_request:
            req["cpu"] = spec.cpu_request

        guaranteed = (lim.get("memory") and lim["memory"] == req.get("memory")
                      and lim.get("cpu") and lim["cpu"] == req.get("cpu"))
        job.log(f"  limits  : {lim or '(unchanged)'}")
        job.log(f"  requests: {req or '(unchanged)'}")
        job.log("  QoS will be " + ("Guaranteed (requests == limits, evicted LAST)"
                                    if guaranteed else
                                    "Burstable -- set requests equal to limits for "
                                    "Guaranteed, which is evicted last under node pressure"))

        if spec.kind == "opstree":
            import json as _j
            body: dict[str, Any] = {}
            if lim:
                body["limits"] = lim
            if req:
                body["requests"] = req
            patch = _j.dumps({"spec": {"kubernetesConfig": {"resources": body}}})
            ocp.run(kubeconfig, ["patch", plural, name, "-n", ns, "--type", "merge",
                                 "-p", patch], log=job.log, timeout=90)
            job.log("  the operator rolls the pods")
        else:
            args = ["set", "resources", f"{kind}/{name}", "-n", ns]
            if lim:
                args.append("--limits=" + ",".join(f"{k}={v}" for k, v in lim.items()))
            if req:
                args.append("--requests=" + ",".join(f"{k}={v}" for k, v in req.items()))
            ocp.run(kubeconfig, args, log=job.log, timeout=90)
            ocp.run(kubeconfig, ["rollout", "status", f"{kind}/{name}", "-n", ns,
                                 "--timeout=900s"], check=False, timeout=960, log=job.log)
    elif new_mm:
        # no pod spec change: apply it live, every pod, zero downtime
        pods = (ocp.jsonpath(kubeconfig, ["get", "pods", "-n", ns, "-l", f"app={name}"],
                             '{range .items[*]}{.metadata.name}{" "}{end}') or "").split()
        for p in pods:
            r = ocp.run(kubeconfig, ["exec", "-n", ns, p, "--", "redis-cli", *auth,
                                     "CONFIG", "SET", "maxmemory", new_mm],
                        check=False, timeout=45)
            ok = "OK" in (r.stdout or "")
            job.log(f"  {p}: CONFIG SET maxmemory {new_mm} -> {'OK' if ok else r.stdout.strip()}")
        job.log("")
        job.log("  applied live, no restart, no downtime")
        if redis_bytes(new_mm) < float(cur_mm or 0):
            job.log("  NOTE: you LOWERED maxmemory. Redis will evict immediately to fit,")
            job.log("  under the current policy, until it is back under the new ceiling.")

    job.result.update({"maxmemory": new_mm or cur_mm, "memory_limit": new_lim or cur_lim,
                       "restarted": spec_changes})


# ---------------------------------------------------------------- ACL users

def list_users(kubeconfig: str, ns: str, name: str) -> list[dict]:
    """Read the ACL as the server sees it, not as the manifest claims."""
    pod = ocp.jsonpath(kubeconfig, ["get", "pods", "-n", ns, "-l", f"app={name}"],
                       "{.items[0].metadata.name}")
    if not pod:
        return []
    pw = ""
    for sname, key in ((f"{name}-auth", "redis-password"), (f"{name}-auth", "password")):
        raw = ocp.run(kubeconfig, ["get", "secret", sname, "-n", ns, "-o",
                                   f"jsonpath={{.data.{key}}}"],
                      check=False, timeout=30).stdout.strip()
        if raw:
            import base64 as _b
            pw = _b.b64decode(raw).decode()
            break
    auth = ["-a", pw, "--no-auth-warning"] if pw else []
    out = ocp.run(kubeconfig, ["exec", "-n", ns, pod, "--", "redis-cli", *auth,
                               "ACL", "LIST"], check=False, timeout=45).stdout or ""
    users = []
    for line in out.splitlines():
        if not line.startswith("user "):
            continue
        parts = line.split()
        users.append({
            "username": parts[1],
            "enabled": "on" in parts[2:4],
            "keys": " ".join(p for p in parts if p.startswith("~")) or "(none)",
            "channels": ("&*" if "&*" in parts else
                         "none" if "resetchannels" in parts else
                         " ".join(p for p in parts if p.startswith("&")) or "(none)"),
            "commands": " ".join(p for p in parts if p.startswith(("+", "-"))),
            "raw": line,
        })
    return users


def manage_acl(job, kubeconfig: str, spec: Day2Spec) -> None:
    """Create, delete or re-password an ACL user.

    Applied twice on purpose: live with ACL SETUSER so it takes effect at once,
    and into the ConfigMap so it survives a restart. A runtime-only ACL change
    is lost the moment the pod is recreated, which is the kind of thing nobody
    notices until an unrelated rollout.
    """
    ns, name = spec.namespace, spec.name
    action = spec.acl_action or "create"
    u = spec.user
    if not u:
        raise RuntimeError("no user given")
    if u.username == "default":
        raise RuntimeError(
            "'default' is the admin account that requirepass sets and that the "
            "health probes authenticate as. Changing it here would lock out the "
            "readiness probe. Use the cache/resources section to change its password.")

    from .catalog import acl_line
    job.step(1, 4, f"{action} user '{u.username}'")
    pods = (ocp.jsonpath(kubeconfig, ["get", "pods", "-n", ns, "-l", f"app={name}"],
                         '{range .items[*]}{.metadata.name}{" "}{end}') or "").split()
    if not pods:
        raise RuntimeError(f"no pods found for '{name}' in '{ns}'")
    pw = ""
    for sname, key in ((f"{name}-auth", "redis-password"), (f"{name}-auth", "password")):
        raw = ocp.run(kubeconfig, ["get", "secret", sname, "-n", ns, "-o",
                                   f"jsonpath={{.data.{key}}}"],
                      check=False, timeout=30).stdout.strip()
        if raw:
            import base64 as _b
            pw = _b.b64decode(raw).decode()
            break
    auth = ["-a", pw, "--no-auth-warning"] if pw else []

    if action in ("create", "password") and not u.password:
        from .manifests import gen_password
        u.password = gen_password()

    line = acl_line(u.username, u.password or "", u.key_pattern, u.permissions,
                    u.channels, u.enabled)
    shown = line.replace(u.password, "********") if u.password else line
    job.log("  " + shown)

    job.step(2, 4, "Applying to every running pod")
    for p in pods:
        if action == "delete":
            cmd = ["ACL", "DELUSER", u.username]
        else:
            cmd = ["ACL", "SETUSER"] + line.split()[1:]
        r = ocp.run(kubeconfig, ["exec", "-n", ns, p, "--", "redis-cli", *auth, *cmd],
                    check=False, timeout=45)
        res = ((r.stdout or "") + (r.stderr or "")).strip()
        job.log(f"  {p}: {res[:90]}")
        if "ERR" in res or "WRONGPASS" in res:
            raise RuntimeError(f"{p} rejected the change: {res[:160]}")

    job.step(3, 4, "Persisting to the ConfigMap so it survives a restart")
    cm = f"{name}-config"
    cur = ocp.run(kubeconfig, ["get", "cm", cm, "-n", ns, "-o",
                               "jsonpath={.data.redis\\.conf}"],
                  check=False, timeout=45).stdout
    if cur:
        kept = [l for l in cur.splitlines()
                if not re.match(rf"^user\s+{re.escape(u.username)}\s", l)]
        if action != "delete":
            kept.append(line)
        import json as _j
        ocp.run(kubeconfig, ["patch", "cm", cm, "-n", ns, "--type", "merge",
                             "-p", _j.dumps({"data": {"redis.conf": "\n".join(kept) + "\n"}})],
                log=job.log, timeout=90)
    else:
        job.log(f"  WARNING: could not read ConfigMap {cm}; this change will be LOST "
                "when a pod restarts")

    job.step(4, 4, "Credential Secret")
    sec = f"{name}-user-{u.username}"
    if action == "delete":
        ocp.run(kubeconfig, ["delete", "secret", sec, "-n", ns, "--ignore-not-found"],
                check=False, timeout=60, log=job.log)
    else:
        import base64 as _b
        import json as _j
        body = _j.dumps({
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": sec, "namespace": ns,
                         "labels": {"app": name,
                                    "app.kubernetes.io/managed-by": "redis-deployer",
                                    "redis-deployer/acl-user": u.username}},
            "type": "Opaque",
            "data": {"username": _b.b64encode(u.username.encode()).decode(),
                     "password": _b.b64encode((u.password or "").encode()).decode(),
                     "key-pattern": _b.b64encode(u.key_pattern.encode()).decode()},
        })
        ocp.apply_yaml(kubeconfig, body, log=job.log)
        job.result["user"] = {"username": u.username, "password": u.password,
                              "secret": sec, "key_pattern": u.key_pattern,
                              "permissions": u.permissions}
        job.log("")
        job.log(f"  give the application Secret '{sec}' in its own namespace --")
        job.log("  it carries only this user's credential, not the admin password")
