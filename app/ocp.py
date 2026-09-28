"""Thin, auditable wrapper around the `oc` CLI.

Every cluster interaction in this app goes through here. Nothing else shells out.
Passwords are never logged: _redact() strips them before anything is echoed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from typing import Callable, Optional, Sequence

OC = shutil.which("oc") or "/usr/bin/oc"
SESSION_DIR = "/tmp/rediscover-sessions"

Logger = Optional[Callable[[str], None]]


class OcError(RuntimeError):
    def __init__(self, args: Sequence[str], rc: int, out: str, err: str):
        self.rc = rc
        self.out = out
        self.err = err
        detail = (err or out or "").strip()
        super().__init__(f"oc {' '.join(_redact(args))} failed (rc={rc}): {detail}")


def _redact(args: Sequence[str]) -> list[str]:
    """Never echo a password into a log line."""
    out: list[str] = []
    skip = False
    for a in args:
        if skip:
            out.append("********")
            skip = False
            continue
        if a in ("-p", "--password", "--token"):
            out.append(a)
            skip = True
            continue
        if a.startswith("--password=") or a.startswith("--token="):
            out.append(a.split("=", 1)[0] + "=********")
            continue
        out.append(a)
    return out


def _env(kubeconfig: str) -> dict:
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig
    return env


def run(
    kubeconfig: str,
    args: Sequence[str],
    *,
    stdin: Optional[str] = None,
    timeout: int = 300,
    check: bool = True,
    log: Logger = None,
) -> subprocess.CompletedProcess:
    if log:
        log("$ oc " + " ".join(_redact(args)))
    p = subprocess.run(
        [OC, *args],
        input=stdin,
        env=_env(kubeconfig),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if log:
        for line in (p.stdout or "").splitlines():
            log("  " + line)
        for line in (p.stderr or "").splitlines():
            log("  " + line)
    if check and p.returncode != 0:
        raise OcError(args, p.returncode, p.stdout, p.stderr)
    return p


# ---------------------------------------------------------------- auth

def detect_server() -> Optional[str]:
    """Best-effort: read the API URL from whatever kubeconfig the host already has."""
    for kc in (os.environ.get("KUBECONFIG"), os.path.expanduser("~/.kube/config")):
        if not kc or not os.path.exists(kc):
            continue
        try:
            p = subprocess.run(
                [OC, "whoami", "--show-server"],
                env={**os.environ, "KUBECONFIG": kc},
                capture_output=True,
                text=True,
                timeout=15,
            )
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
        except Exception:
            pass
    return None


def login(server: str, username: str, password: str, insecure: bool = True) -> str:
    """Log in and return the path to a private, per-session kubeconfig (mode 0600).

    The password is used once, here, and never stored.
    """
    os.makedirs(SESSION_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(SESSION_DIR, f"kubeconfig-{uuid.uuid4().hex}")
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.close(fd)

    args = ["login", server, "-u", username, "-p", password]
    if insecure:
        args.append("--insecure-skip-tls-verify=true")

    p = subprocess.run(
        [OC, *args], env=_env(path), capture_output=True, text=True, timeout=60
    )
    if p.returncode != 0:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise OcError(args, p.returncode, p.stdout, p.stderr)
    return path


def logout(kubeconfig: str) -> None:
    try:
        subprocess.run([OC, "logout"], env=_env(kubeconfig), capture_output=True, timeout=30)
    except Exception:
        pass
    try:
        os.unlink(kubeconfig)
    except OSError:
        pass


def whoami(kubeconfig: str) -> str:
    return run(kubeconfig, ["whoami"], timeout=30).stdout.strip()


def is_cluster_admin(kubeconfig: str) -> bool:
    p = run(
        kubeconfig,
        ["auth", "can-i", "create", "subscriptions.operators.coreos.com", "-A"],
        check=False,
        timeout=30,
    )
    return p.stdout.strip() == "yes"


# ---------------------------------------------------------------- helpers

def apply_yaml(kubeconfig: str, yaml_text: str, *, dry_run: bool = False, log: Logger = None):
    args = ["apply", "-f", "-"]
    if dry_run:
        args.append("--dry-run=server")
    return run(kubeconfig, args, stdin=yaml_text, log=log, timeout=180)


def delete_yaml(kubeconfig: str, yaml_text: str, log: Logger = None):
    return run(
        kubeconfig,
        ["delete", "-f", "-", "--ignore-not-found"],
        stdin=yaml_text,
        log=log,
        check=False,
        timeout=300,
    )


def jsonpath(kubeconfig: str, args: Sequence[str], expr: str, *, check: bool = False) -> str:
    p = run(kubeconfig, [*args, "-o", f"jsonpath={expr}"], check=check, timeout=60)
    return (p.stdout or "").strip()


def wait_for(
    kubeconfig: str,
    args: Sequence[str],
    expr: str,
    expected: str,
    *,
    timeout: int = 900,
    interval: int = 5,
    label: str = "resource",
    log: Logger = None,
) -> str:
    """Poll a jsonpath until it equals `expected`. Returns the value, raises on timeout."""
    deadline = time.time() + timeout
    last = ""
    if log:
        log(f"... waiting for {label} to become '{expected}' (timeout {timeout}s)")
    while time.time() < deadline:
        val = jsonpath(kubeconfig, args, expr)
        if val != last:
            last = val
            if log:
                log(f"    {label}: {val or '<empty>'}")
        if val == expected:
            return val
        time.sleep(interval)
    raise TimeoutError(f"{label} never reached '{expected}' (last value: '{last or '<empty>'}')")


def recent_events(kubeconfig: str, namespace: str, tail: int = 25) -> str:
    p = run(
        kubeconfig,
        ["get", "events", "-n", namespace, "--sort-by=.lastTimestamp"],
        check=False,
        timeout=60,
    )
    lines = (p.stdout or "").splitlines()
    return "\n".join(lines[-tail:])


# ---------------------------------------------------------------- storage classification

# Guessing "is this block storage?" from the provisioner string is only safe
# against a known list. Dell Isilon/PowerScale, for example, is scale-out NAS
# (NFS) but its provisioner name contains neither "nfs" nor "file".
_FILE_PROVISIONERS = (
    "nfs", "cephfs", "azurefile", "efs.csi", "filestore", "glusterfs",
    "isilon", "powerscale", "ontap-nas", "trident-nas", "quobyte",
    "juicefs", "weka", "vast", "flashblade", "smb.csi",
)
_BLOCK_PROVISIONERS = (
    "rbd", "vsphere", "vmware", "ebs.csi", "disk.csi.azure", "pd.csi",
    "cinder", "iscsi", "lvm", "topolvm", "local", "powerstore", "powermax",
    "unity", "xtremio", "purestorage", "pure-block", "hpe", "ontap-san",
    "trident-san", "hostpath", "openebs", "linstor", "portworx",
)


def classify_storage(provisioner: str) -> str:
    """Return 'block', 'file' or 'unknown'. File first: a NAS name may also
    contain a vendor token that appears in the block list."""
    p = (provisioner or "").lower()
    if any(tok in p for tok in _FILE_PROVISIONERS):
        return "file"
    if any(tok in p for tok in _BLOCK_PROVISIONERS):
        return "block"
    return "unknown"


# ---------------------------------------------------------------- cluster DNS domain

_DOMAIN_CACHE: dict[str, str] = {}


def cluster_domain(kubeconfig: str, namespace: str = "", pod: str = "",
                   log: Logger = None) -> str:
    """The cluster's internal DNS domain -- almost always 'cluster.local',
    but it IS configurable at install time and some clusters change it.

    Two sources, because neither works for everyone:
      1. dns.operator/default .status.clusterDomain -- cheap, but cluster-scoped,
         so a plain developer account cannot read it.
      2. the search path in a running pod's /etc/resolv.conf -- authoritative and
         readable by anyone who can exec into their own pod.
    Falls back to 'cluster.local' rather than failing the deployment.
    """
    cached = _DOMAIN_CACHE.get(kubeconfig)
    if cached:
        return cached

    domain = jsonpath(kubeconfig, ["get", "dns.operator/default"],
                      "{.status.clusterDomain}")

    if not domain and pod and namespace:
        p = run(kubeconfig, ["exec", "-n", namespace, pod, "--",
                             "cat", "/etc/resolv.conf"], check=False, timeout=45)
        for line in (p.stdout or "").splitlines():
            if line.startswith("search"):
                # search <ns>.svc.<domain> svc.<domain> <domain> ...
                for tok in line.split()[1:]:
                    if tok.startswith("svc."):
                        domain = tok[4:]
                        break
            if domain:
                break

    domain = domain or "cluster.local"
    if log:
        job_note = "" if domain == "cluster.local" else "  (non-default!)"
        log(f"  cluster DNS domain: {domain}{job_note}")
    _DOMAIN_CACHE[kubeconfig] = domain
    return domain


def schedulable_nodes(kubeconfig: str) -> list[dict]:
    """Nodes a normal workload can actually land on.

    The `node-role.kubernetes.io/worker` label is not enough: OpenShift infra
    nodes usually carry that label AND a NoSchedule/NoExecute taint, so counting
    by label alone over-reports capacity. A pre-flight that says "6 nodes
    available" when 3 are tainted lets a deployment through that then sits
    Pending forever.
    """
    import json as _json
    p = run(kubeconfig, ["get", "nodes", "-o", "json"], check=False, timeout=90)
    if p.returncode != 0:
        return []
    out = []
    for item in _json.loads(p.stdout).get("items", []):
        taints = item["spec"].get("taints") or []
        blocking = [t for t in taints
                    if t.get("effect") in ("NoSchedule", "NoExecute")]
        if blocking:
            continue
        labels = item["metadata"].get("labels", {}) or {}
        alloc = item["status"].get("allocatable", {})
        out.append({
            "name": item["metadata"]["name"],
            "roles": [k.split("/", 1)[1] for k in labels
                      if k.startswith("node-role.kubernetes.io/")],
            "cpu": alloc.get("cpu"),
            "memory": alloc.get("memory"),
        })
    return out
