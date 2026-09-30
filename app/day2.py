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

def grow_storage(job, kubeconfig: str, spec: Day2Spec) -> None:
    ns = spec.namespace
    job.step(1, 4, "Checking the StorageClass allows expansion")
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

    job.step(2, 4, "Checking the new size is larger")
    def to_gi(v: str) -> float:
        m = re.match(r"([\d.]+)\s*([GMT]i?)", v or "")
        if not m:
            return 0.0
        n, u = float(m.group(1)), m.group(2)[0]
        return n * {"M": 1 / 1024, "G": 1, "T": 1024}[u]
    new = to_gi(spec.storage_size)
    for pvc in pvcs:
        cur = to_gi(pvc["size"])
        job.log(f"  {pvc['name']}: {pvc['size']} -> {spec.storage_size}")
        if new <= cur:
            raise RuntimeError(
                f"{pvc['name']} is already {pvc['size']}. Kubernetes cannot SHRINK a "
                "volume -- only grow it.")

    job.step(3, 4, "Patching each PersistentVolumeClaim")
    for pvc in pvcs:
        ocp.run(kubeconfig, ["patch", "pvc", pvc["name"], "-n", ns, "--type", "merge",
                             "-p", '{"spec":{"resources":{"requests":{"storage":"'
                                   + spec.storage_size + '"}}}}'],
                log=job.log, timeout=90)

    job.step(4, 4, "Result")
    ocp.run(kubeconfig, ["get", "pvc", "-n", ns], check=False, log=job.log, timeout=60)
    job.log("")
    job.log("  Expansion is asynchronous. Some CSI drivers resize the filesystem")
    job.log("  online; others need the pod restarted. Watch the PVC conditions:")
    job.log(f"    oc describe pvc {pvcs[0]['name']} -n {ns}")
    job.log("")
    job.log("  IMPORTANT for a StatefulSet: volumeClaimTemplates are IMMUTABLE, so")
    job.log("  this grows the PVCs that exist today. Any replica added later gets")
    job.log("  the ORIGINAL size. Update the manifest and recreate to fix that.")
    job.result["note"] = "volumeClaimTemplates unchanged; new replicas use the old size"


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

def _bytes(v: str) -> float:
    """Parse both Redis units (256mb) and Kubernetes units (512Mi)."""
    m = re.match(r"^\s*([\d.]+)\s*([kKmMgG]i?[bB]?)?\s*$", v or "")
    if not m:
        return 0.0
    n = float(m.group(1))
    u = (m.group(2) or "").lower()
    if not u:
        return n
    if u.startswith("k"):
        return n * (1024 if "i" in u else 1000)
    if u.startswith("m"):
        return n * (1048576 if "i" in u else 1000000)
    if u.startswith("g"):
        return n * (1073741824 if "i" in u else 1000000000)
    return n


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

    job.log(f"  maxmemory (RAM, the cache)  : {_human(_bytes(cur_mm))}"
            f"  ->  {new_mm or '(unchanged)'}")
    job.log(f"  container memory limit      : {cur_lim or '?'}"
            f"  ->  {new_lim or '(unchanged)'}")
    job.log("  disk (PVC) is NOT involved -- it only holds the AOF/RDB files")

    target_mm = _bytes(new_mm) if new_mm else _bytes(cur_mm)
    target_lim = _bytes(new_lim) if new_lim else _bytes(cur_lim)

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

    limit_changes = bool(new_lim) and _bytes(new_lim) != _bytes(cur_lim)
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
    if limit_changes:
        if spec.kind == "opstree":
            import json as _j
            patch = _j.dumps({"spec": {"kubernetesConfig": {"resources": {
                "limits": {"memory": new_lim},
                "requests": {"memory": new_lim}}}}})
            ocp.run(kubeconfig, ["patch", plural, name, "-n", ns, "--type", "merge",
                                 "-p", patch], log=job.log, timeout=90)
            job.log("  the operator rolls the pods")
        else:
            ocp.run(kubeconfig, ["set", "resources", f"{kind}/{name}", "-n", ns,
                                 f"--limits=memory={new_lim}",
                                 f"--requests=memory={new_lim}"],
                    log=job.log, timeout=90)
            ocp.run(kubeconfig, ["rollout", "status", f"{kind}/{name}", "-n", ns,
                                 "--timeout=900s"], check=False, timeout=960, log=job.log)
        job.log("")
        job.log("  requests were set equal to limits -- QoS class Guaranteed, evicted last")
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
        if _bytes(new_mm) < _bytes(cur_mm):
            job.log("  NOTE: you LOWERED maxmemory. Redis will evict immediately to fit,")
            job.log("  under the current policy, until it is back under the new ceiling.")

    job.result.update({"maxmemory": new_mm or cur_mm, "memory_limit": new_lim or cur_lim,
                       "restarted": limit_changes})
