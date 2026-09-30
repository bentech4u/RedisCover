"""Inspect a cluster endpoint BEFORE sending credentials to it.

Two questions this answers, both of which "skip TLS verification" hides:

  * Is this the cluster I think it is?  The OAuth issuer carries the cluster's
    own apps domain, so it identifies the cluster even when several share a
    similar API hostname.
  * Am I about to trust a certificate I have never seen?  An OpenShift API
    server is normally signed by a cluster-internal CA, so it will not validate
    against the system bundle -- which is exactly why people tick "skip" and
    stop looking. Showing the SHA-256 fingerprint lets it be compared with a
    known-good value instead.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
from urllib.parse import urlparse


def _run(cmd: list[str], stdin: str = "", timeout: int = 15) -> str:
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                           timeout=timeout)
        return (p.stdout or "") + (p.stderr or "")
    except Exception as exc:                                   # noqa: BLE001
        return f"__error__ {exc}"


def _parse_dt(s: str) -> _dt.datetime | None:
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%b  %d %H:%M:%S %Y %Z"):
        try:
            return _dt.datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def inspect(server: str) -> dict:
    u = urlparse(server if "://" in server else f"https://{server}")
    host = u.hostname or ""
    port = u.port or 6443
    out: dict = {"server": server, "host": host, "port": port,
                 "reachable": False, "cert": {}, "cluster": {}, "warnings": []}
    if not host:
        out["error"] = "could not parse a hostname from that URL"
        return out

    target = f"{host}:{port}"
    raw = _run(["openssl", "s_client", "-connect", target, "-servername", host],
               stdin="", timeout=20)
    if "__error__" in raw or "CONNECTED" not in raw:
        out["error"] = (f"cannot reach {target}. "
                        "Check the address, the port and any firewall.")
        return out
    out["reachable"] = True

    pem = ""
    m = re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
                  raw, re.S)
    if m:
        pem = m.group(0)

    verify = re.search(r"Verify return code:\s*(\d+)\s*\((.*?)\)", raw)
    code = int(verify.group(1)) if verify else -1
    out["trusted"] = code == 0
    out["verify_error"] = "" if code == 0 else (verify.group(2) if verify else "unknown")

    if pem:
        det = _run(["openssl", "x509", "-noout", "-subject", "-issuer",
                    "-dates", "-fingerprint", "-sha256", "-ext", "subjectAltName"],
                   stdin=pem)
        cert = {}
        for line in det.splitlines():
            line = line.strip()
            if line.startswith("subject="):
                cert["subject"] = line[8:].strip()
            elif line.startswith("issuer="):
                cert["issuer"] = line[7:].strip()
            elif line.startswith("notBefore="):
                cert["not_before"] = line[10:].strip()
            elif line.startswith("notAfter="):
                cert["not_after"] = line[9:].strip()
            elif "Fingerprint=" in line:
                cert["sha256"] = line.split("=", 1)[1].strip()
            elif line.startswith("DNS:") or line.startswith("IP Address:"):
                cert["sans"] = [x.strip() for x in line.split(",")]
        exp = _parse_dt(cert.get("not_after", ""))
        if exp:
            days = (exp - _dt.datetime.utcnow()).days
            cert["days_until_expiry"] = days
            if days < 0:
                out["warnings"].append(
                    f"The certificate EXPIRED {abs(days)} day(s) ago.")
            elif days < 30:
                out["warnings"].append(
                    f"The certificate expires in {days} day(s) "
                    f"({cert.get('not_after')}). Renew it before it lapses.")
        out["cert"] = cert

    if not out["trusted"]:
        out["warnings"].append(
            "This certificate does not validate against the system CA bundle "
            f"({out['verify_error']}). That is normal for an OpenShift API server, "
            "which is signed by a cluster-internal CA -- but it means you must "
            "compare the fingerprint below with a value you already trust.")

    ver = _run(["curl", "-sk", "--max-time", "10",
                f"https://{target}/version"], timeout=15)
    try:
        v = json.loads(ver)
        out["cluster"]["kubernetes"] = v.get("gitVersion")
        out["cluster"]["build_date"] = v.get("buildDate")
    except Exception:
        pass

    oauth = _run(["curl", "-sk", "--max-time", "10",
                  f"https://{target}/.well-known/oauth-authorization-server"],
                 timeout=15)
    try:
        o = json.loads(oauth)
        issuer = o.get("issuer", "")
        out["cluster"]["oauth_issuer"] = issuer
        m = re.search(r"https://oauth-openshift\.apps\.(.+)$", issuer)
        if m:
            out["cluster"]["cluster_domain"] = m.group(1)
            out["cluster"]["console"] = (
                f"https://console-openshift-console.apps.{m.group(1)}")
    except Exception:
        pass

    if out["cert"].get("sans") and host not in " ".join(out["cert"]["sans"]):
        out["warnings"].append(
            f"'{host}' does not appear in the certificate's subject alternative "
            f"names ({', '.join(out['cert']['sans'])}). You may be connecting "
            "through a proxy, or to the wrong endpoint.")
    return out
