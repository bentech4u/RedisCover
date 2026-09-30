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
