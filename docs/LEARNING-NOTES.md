# OpenShift Learning Notes — worked through with Redis

Notes from a hands-on session on the `homelab` cluster (OpenShift 4.22 / Kubernetes v1.35.6).
Each section is a concept, the command that proved it, what we actually saw, and what to remember.

Order matters — later concepts depend on earlier ones.

---

## 1. Cluster anatomy

```bash
oc get nodes
oc get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints
```

What we saw:

```
3 masters   NoSchedule                       API, etcd, scheduler
3 infra     NoSchedule + NoExecute           routers, registry, monitoring
3 workers   <none>                           ← the ONLY schedulable nodes
```

**Taint effects differ:**

- `NoSchedule` — won't place new pods there
- `NoExecute` — stricter; also **evicts pods already running**

**Takeaway:** count your *schedulable* nodes, not your total nodes. Any workload using required
pod anti-affinity (databases, quorum systems) can never have more replicas than you have
eligible nodes. A 4th replica sits `Pending` forever with `didn't match pod anti-affinity rules`
— which looks like a capacity problem but is a placement problem.

---

## 2. StorageClasses and binding modes

```bash
oc get storageclass
```

```
nfs-synology (default)   nfs.csi.k8s.io            Immediate
thin-csi                 csi.vsphere.vmware.com    WaitForFirstConsumer
isilon*                  csi-isilon.dellemc.com    Immediate
```

Two things to read every time:

| Column | Why it matters |
|---|---|
| `(default)` | A PVC with no `storageClassName` silently gets this one |
| `VOLUMEBINDINGMODE` | `WaitForFirstConsumer` = PVC stays `Pending` until a **pod** uses it |
| `ALLOWVOLUMEEXPANSION` | Whether you can grow it later without migrating data |
| `RECLAIMPOLICY` | `Delete` = removing the PVC destroys the real disk, immediately |

**The trap:** with `WaitForFirstConsumer`, creating a PVC and seeing `Pending` looks broken.
It isn't. The CSI driver is waiting to learn *which node* the pod lands on, so it can attach
the disk there. **You cannot test such a StorageClass with a PVC alone — you must attach a pod.**

**For databases:** prefer **block** storage over NFS. Redis, Postgres etc. rely on `fsync`
semantics and file locking that NFS/CephFS handle differently. The failure mode is not a clean
error at deploy time — it's corruption or a stall weeks later.

---

## 3. The CSI provisioning chain

```bash
oc events
```

The full lifecycle, in order:

```
1. WaitForFirstConsumer ......... PVC idle — "which node will need me?"
2. ExternalProvisioning ......... handed off to the CSI driver
3. Provisioning ................. driver carving a real VMDK / RBD image
4. ProvisioningSucceeded ........ disk exists → PVC goes Bound
5. Scheduled → worker-0 ......... scheduler picked the node
6. SuccessfulAttachVolume ....... the disk is hot-attached to that node
7. AddedInterface 10.128.2.227 .. OVN assigns the pod its IP
8. Pulling / Pulled ............. image fetched
9. Created / Started ............ container runs
```

**Step 6 is physical.** A virtual disk gets attached to a machine. That's why `ReadWriteOnce`
exists, and why a pod with an RWO volume is **pinned to the node holding it**.

**`oc events` beats `oc describe`** for this: it shows every object in the namespace on one
sorted timeline. Make it your default debugging command.

---

## 4. SecurityContextConstraints — the #1 OpenShift gotcha

Plain Kubernetes lets containers run as root. **OpenShift does not.** An SCC decides what a pod
may do — and critically, **which SCC applies depends on *who created the pod*.**

We proved it with the same image, two creation paths:

```bash
# bare Pod, created by system:admin
oc apply -f storage-test.yaml
oc get pod storage-test -o jsonpath='{.metadata.annotations.openshift\.io/scc}{"\n"}'
oc rsh storage-test id

# Deployment — pods created by the ReplicaSet controller as the 'default' ServiceAccount
oc create deployment storage-test-deploy --image=...
oc get pod -l app=storage-test-deploy -o jsonpath='{.items[0].metadata.annotations.openshift\.io/scc}{"\n"}'
oc rsh deployment/storage-test-deploy id
```

| | Bare Pod (you created it) | Deployment (controller created it) |
|---|---|---|
| SCC | `anyuid` | `restricted-v2` |
| runAsUser | none → **root** | `1000790000` |
| fsGroup | none | `1000790000` |
| seccompProfile | none | `RuntimeDefault` |
| `id` | `uid=0(root)` | `uid=1000790000 groups=0,1000790000` |
| prompt | `sh-5.1#` | `sh-5.1$` |

**Why `anyuid` and not `privileged`?** When a user is eligible for several SCCs, admission sorts
them by **priority** and picks the winner. `anyuid` has `priority: 10`; `privileged` has none (0).

```bash
oc get scc -o custom-columns=NAME:.metadata.name,PRIO:.priority,RUNASUSER:.runAsUser.type,FSGROUP:.fsGroup.type
```

```
anyuid          RunAsAny           RunAsAny     image picks the UID; root allowed
restricted-v2   MustRunAsRange     MustRunAs    ← THE DEFAULT for workloads
nonroot-v2      MustRunAsNonRoot   RunAsAny     image picks, but root refused
privileged      RunAsAny           RunAsAny     everything allowed
```

> **⚠ The lesson that costs people hours:** testing storage or an image with a bare Pod as
> `cluster-admin` proves **nothing**. You get privileges your application will never have.
> Always test with a Deployment.

---

## 5. Where the random UID comes from

```bash
oc get namespace redis-demo -o jsonpath='{.metadata.annotations}' | tr ',' '\n'
```

```
openshift.io/sa.scc.uid-range:            "1000790000/10000"   ← start / how many
openshift.io/sa.scc.supplemental-groups:  "1000790000/10000"
openshift.io/sa.scc.mcs:                  "s0:c28,c17"         ← SELinux category pair
```

Every project gets its own private block of 10,000 UIDs, plus its own SELinux MCS label. A
process escaping project A cannot impersonate a process in project B — enforced by the kernel,
not just by RBAC.

`restricted-v2` picks the **first** UID in the range: `1000790000`.

---

## 6. fsGroup — how a non-root pod writes to a volume

```
id      → uid=1000790000  gid=0  groups=0,1000790000
                                          ▲ fsGroup, injected into supplementary groups

ls -ld  → drwxrwsr-x. root 1000790000 /data
             ▲ ▲▲▲          ▲
             │ group rwx    group owner == your fsGroup
             └ owner is root — you are NOT the owner
```

You get write access **only** through the group bit. The chain is:

```
restricted-v2 has fsGroup: MustRunAs
   → admission injects fsGroup into the pod
      → the CSI driver chowns the volume's group to it
         → the group bit makes it writable
```

The `s` in `drwxrwsr-x` is **setgid** — new files inherit that group automatically, which is
what keeps Redis's `dump.rdb` writable after creation.

**Proof it works:** the Redis log line

```
* Creating AOF base file appendonly.aof.1.base.rdb on server start
```

A UID that doesn't exist in `/etc/passwd`, no root anywhere, successfully writing to a vSphere
block device. **No `chmod 777`, no `runAsUser: 0`, no privileged SCC.** That's what "correct on
OpenShift" looks like.

---

## 7. SCC vs Pod Security Admission — two independent layers

Applying a root-running pod produced:

```
Warning: would violate PodSecurity "restricted:latest":
  allowPrivilegeEscalation != false
  unrestricted capabilities (must drop ["ALL"])
  runAsNonRoot != true
  seccompProfile not set to "RuntimeDefault"
```

| | **SCC** (OpenShift) | **PSA** (upstream Kubernetes) |
|---|---|---|
| Can it change your pod? | **Yes** — injects UID, fsGroup, SELinux | **No** — never mutates, only judges |
| What happened here | `anyuid` allowed root | warned that root violates `restricted` |
| Outcome | pod admitted | pod admitted anyway (**warn** mode) |

On a hardened cluster PSA runs in **enforce** mode and the pod is rejected outright.

**That warning is a free specification.** Those four fields are exactly what a well-behaved pod
sets:

```yaml
securityContext:              # pod level
  runAsNonRoot: true
  seccompProfile:
    type: RuntimeDefault
# container level
  allowPrivilegeEscalation: false
  capabilities:
    drop: ["ALL"]
```

Get those right and the workload runs on *any* Kubernetes, hardened or not.

---

## 8. Imperative vs declarative

| Style | Command | Use for |
|---|---|---|
| Imperative | `oc create deployment ...` | learning, throwaway tests |
| Declarative | write YAML → `oc apply -f` | anything you keep |

Always dry-run against the API before applying for real:

```bash
oc apply -f redis.yaml --dry-run=server
```

The same principle bit us again with the Redis Enterprise console: a database created in the UI
has **no** custom resource, is invisible to `oc get redb` and to Git, and does not come back
when the cluster is rebuilt. **Use the UI to look; use YAML to create.**

---

## 9. Deployment — the fields that actually matter

### `strategy: Recreate`

```
Default is RollingUpdate: start the NEW pod, then kill the old one.
But an RWO PVC attaches to ONE node at a time.
  → new pod waits for the volume
  → old pod won't release it until the new one is Ready
  → DEADLOCK, stuck in ContainerCreating forever

Recreate: kill the old pod FIRST, then start the new one.
  → brief downtime, but it works.
```

**Every single-replica stateful workload on RWO storage needs this.**

### Selectors

`spec.selector.matchLabels` must match `spec.template.metadata.labels`. Mismatch = a Deployment
that creates pods forever and never recognises them.

### Secrets into the process

```yaml
env:
  - name: REDIS_PASSWORD
    valueFrom:
      secretKeyRef: {name: redis-auth, key: redis-password}
command: ["redis-server", "/etc/redis/redis.conf", "--requirepass", "$(REDIS_PASSWORD)"]
```

Note `$(VAR)` — **parentheses, not braces**. `${VAR}` does not expand and your password becomes
the literal string.

### The three probes

| Probe | Question | On failure |
|---|---|---|
| `startupProbe` | "finished booting?" | **suspends** the other two — grace for slow AOF loads |
| `readinessProbe` | "can you serve *now*?" | removed from the **Service**, not restarted |
| `livenessProbe` | "are you wedged?" | **killed and restarted** |

**Make liveness dumber than readiness.** Readiness ran a real `AUTH` + `PING`; liveness was only
a TCP check. An aggressive liveness probe will restart-loop a healthy database during a slow
`BGSAVE`.

The Redis Enterprise cluster showed this beautifully during bootstrap:

```
Warning  Unhealthy  Readiness probe failed: node id file does not exist - pod is not yet bootstrapped
```

Alarming, entirely normal — readiness holding the pod out of the Service while it joins.

### requests vs limits, and QoS

```
requests  what the SCHEDULER reserves — guaranteed floor
limits    the hard ceiling

CPU over limit  → throttled (slow)
MEM over limit  → OOMKilled (dead)
```

Which is why `redis.conf` sets `maxmemory 256mb` well under a `512Mi` container limit: Redis
evicts keys **before** the kernel kills the container.

```bash
oc get pod <name> -o jsonpath='{.status.qosClass}{"\n"}'
```

| QoS class | Condition | Evicted |
|---|---|---|
| `Guaranteed` | requests == limits for every resource | **last** |
| `Burstable` | requests set, but != limits | second |
| `BestEffort` | nothing set | **first** |

**Production databases should be `Guaranteed`.** We deliberately used `Burstable` in the lab to
squeeze onto a full worker — a real trade, worth making consciously rather than discovering
during an incident.

---

## 10. Services — a rule, not a server

```bash
oc get svc redis -o wide
oc get endpointslice -l kubernetes.io/service-name=redis -o wide
oc get pod -l app=redis -o wide
```

`172.30.89.180` is a **fake IP**. Nothing listens on it. No NIC has it.

```
Client says "connect to 172.30.89.180:6379"
        ▼
OVN-Kubernetes rewrites the destination in the kernel to a real pod IP
from the EndpointSlice
        ▼
packet arrives at the pod
```

Because it's a rule, a Service never goes down, never restarts, and keeps its IP for life while
pods are destroyed and recreated with new IPs.

**Labels are the glue.** The EndpointSlice is maintained by matching `service.spec.selector`
against pod labels. Break the labels and the endpoint list goes empty instantly.

**Headless Services** (`CLUSTER-IP: None`) skip the virtual IP entirely — DNS returns the **pod**
IPs. That's how Prometheus scrapes each node individually (`rec-prom`), and how clients discover
individual shards.

---

## 11. DNS

```bash
cat /etc/resolv.conf
getent hosts redis
```

```
search redis-demo.svc.cluster.local svc.cluster.local cluster.local
nameserver 172.30.0.10        ← CoreDNS, itself a Service
options ndots:5
```

| Form | Works from |
|---|---|
| `redis` | same namespace only (via search path) |
| `redis.redis-demo` | any namespace |
| `redis.redis-demo.svc.cluster.local` | **always** — use this in app config |

**Namespaces are an administrative boundary, not a network boundary.** Cross-namespace access
needs no configuration at all — just the FQDN. (Restricting it is NetworkPolicy's job.)

---

## 12. Routes are Layer 7 — they cannot carry Redis

The most instructive failure of the session. We created the Route anyway and watched both
directions break:

```bash
oc expose service/redis --name=redis-route
curl -sv "http://$RHOST/"                      # → 502 Bad Gateway
redis-cli -h "$RHOST" -p 80 ... PING           # → I/O error
```

```
curl  ──GET / HTTP/1.1──▶  HAProxy ──▶ Redis:6379
                                       Redis parses it as an INLINE command,
                                       replies "$-1" (RESP nil)
      HAProxy expects "HTTP/1.1 200 OK", gets "$-1" → 502

redis-cli ──*1\r\n$4\r\nPING\r\n──▶ HAProxy
                                    not an HTTP request line, no Host header
                                    → 400 / connection closed → "I/O error"
```

**Why:** HAProxy routes by reading the HTTP `Host:` header or the TLS **SNI** field. Redis speaks
RESP — raw binary TCP, no Host header, no SNI. There is nothing to route on. This is arithmetic,
not a configuration gap. Same for Postgres, MySQL, MongoDB, Kafka.

A `passthrough` TLS Route doesn't help either — it routes by SNI, which `redis-cli --tls`
doesn't provide.

**But the management UI worked over a passthrough Route**, because it's HTTPS and the browser
sends SNI. Same Route type, opposite outcome. **The protocol decides, not the configuration.**

### What does work for TCP

| Method | Port | Good for | Don't |
|---|---|---|---|
| **ClusterIP** | 6379 | apps inside the cluster | — usually the right answer |
| **port-forward** | any local | debugging, admin tasks | run applications through it |
| **NodePort** | 30000–32767 | labs, on-prem behind a firewall | expose without password + NetworkPolicy |
| **LoadBalancer** | any | production external access | assume it exists — bare metal needs MetalLB |
| **Route** | 80/443 | HTTP apps only | ❌ never for a database |

**NodePort surprise:** the port opens on **every** node, not just the one running the pod. Hit any
node and OVN forwards across the cluster network. Convenient, and a real exposure.

---

## 13. Session affinity — stateful web apps behind replicas

The Redis Enterprise console kept bouncing back to the sign-in page. Not a password problem:

```
POST /login  → rec-0   (session created on rec-0)
GET  /       → rec-1   ("who are you?") → back to sign-in
```

```bash
oc annotate route rec-ui haproxy.router.openshift.io/balance=source --overwrite
```

`balance=source` hashes the client IP so a client always reaches the same backend.

**Any server-side-session app behind multiple replicas needs this** — or it needs to externalise
sessions (to Redis, ironically). It presents as a broken login; it's a load-balancing problem.

---

## 14. What is precious and what is disposable

| Command | Pod | PVC | Data |
|---|---|---|---|
| `oc delete pod ...` | recreated | kept | **safe** |
| `oc scale --replicas=0` | gone | kept | **safe** |
| `oc delete deployment ...` | gone | kept | **safe** |
| `oc delete pvc ...` | — | gone | **DESTROYED** |
| `oc delete project ...` | gone | gone | **DESTROYED** |

```
DISPOSABLE                       PRECIOUS
─────────────────────────────────────────────────────
Pod          recreated in secs   PVC     holds your data
ReplicaSet   managed for you     PV      the actual disk
Deployment   it's just YAML      Secret  a password users depend on
Service      it's just a rule
Route        it's just a rule
```

The `kubernetes.io/pvc-protection` finalizer only blocks deletion **while a pod is using it**.
Scale to zero and the PVC deletes instantly. **It is not a safety net.**

```bash
oc get all                                          # LIES — omits pvc, cm, secret, route, netpol
oc get all,pvc,configmap,secret,route,networkpolicy # the honest version
oc get pv | grep -E 'redis|NAME'                    # always check for orphans afterwards
```

---

## 15. Operators and OLM

An Operator is domain expertise packaged as software: **CRDs** that teach the cluster new object
types, plus a **controller** that reconciles them.

```
CatalogSource            a POD serving a catalogue index over gRPC
    ▼
PackageManifest          read-only view of one operator: channels, versions, installModes
    ▼
OperatorGroup            YOU create. Which namespaces the operator may watch.
    ▼
Subscription             YOU create. "install package X, channel Y, approval Z"
    ▼
InstallPlan              OLM generates. Automatic → applied; Manual → waits for you.
    ▼
ClusterServiceVersion    the installed operator. Pending → Installing → Succeeded
    ▼
    creates: CRDs, controller Deployment, ServiceAccount + RBAC, SCCs, webhooks
```

**You author two objects — `OperatorGroup` and `Subscription`.** Everything else is generated.
When an install goes wrong, walk that chain downward.

```bash
oc get subscription,installplan,csv -n <ns>
```

The CSV phase sequence we observed:

```
RequirementsUnknown → RequirementsNotMet → AllRequirementsMet
  → InstallSucceeded → InstallWaiting → InstallSucceeded
```

### Always inspect what an operator brought

```bash
oc get crd | grep -i <name>
oc get scc | grep -i <name>
oc get clusterrole,clusterrolebinding | grep -i <name>
oc get validatingwebhookconfiguration | grep -i <name>
```

Redis Enterprise installed 10 CRDs and **four ClusterRoles per CRD** (`-admin`, `-edit`, `-view`,
`-crdview`). Those are **aggregated** into the built-in `admin`/`edit`/`view` roles — meaning
anyone who already holds `edit` on a namespace silently gains the ability to manage those custom
resources. Worth knowing before you hand out `edit`.

Notably **absent**: any binding to `cluster-admin`. All real permissions were namespaced Roles.
That's a well-behaved operator. Many are not — check in 30 seconds rather than assuming.

### Manual approval for stateful operators

```bash
oc patch subscription <name> -n <ns> --type merge -p '{"spec":{"installPlanApproval":"Manual"}}'
```

The web console defaults to **Automatic**, which lets a vendor release roll-restart your database
without warning. Pin `startingCSV` too.

---

## 16. StatefulSet vs Deployment

| | Deployment | StatefulSet |
|---|---|---|
| Pod names | random (`redis-545f7689c7-sgmhx`) | ordinal (`rec-0`, `rec-1`, `rec-2`) |
| Identity | interchangeable | stable, with stable DNS |
| Storage | one PVC you create and reference | `volumeClaimTemplate` → **one PVC per pod** |
| Start order | parallel | ordered by default |

`podManagementPolicy` controls that last row:

```bash
oc get statefulset rec -o jsonpath='{.spec.podManagementPolicy}{"\n"}'   # → Parallel
```

Redis Enterprise uses `Parallel` — its nodes negotiate cluster membership among themselves, so
they don't need serialised startup the way a primary/replica chain does.

**Per-pod PVCs are the point.** That's why a Deployment can't scale a stateful app: three
replicas would all mount the same volume.

---

## 17. Platform vs database (REC vs REDB)

```
WHAT WE BUILT BY HAND
  one redis-server process = the server AND the database, inseparable.
  Deployment + Service gave both at once. Port 6379. Done.

REDIS ENTERPRISE
  RedisEnterpriseCluster (REC)  = the PLATFORM. 3 nodes, control plane,
      Envoy proxy layer, metrics, failover machinery. NOT a database.
  RedisEnterpriseDatabase (REDB) = an actual database. Own port, password,
      memory limit, shard count, eviction policy, persistence setting.
      One REC hosts MANY REDBs.
```

Like installing PostgreSQL then running `CREATE DATABASE`. `SHARDS 0/4` on a fresh REC means
there is no Redis running yet.

The scale difference is worth internalising:

| | Hand-built | Redis Enterprise |
|---|---|---|
| image size | ~15 MB (alpine) | 1.88 GB |
| processes in container | 1 (`redis-server`) | ~12 under supervisord |
| pods | 1 | 5 |
| PVCs | 1 × 5Gi | 3 × 20Gi |
| image pull | 3 s | 2 m 45 s |
| HA | none | master + replica, automatic failover |

Those 12 processes (`node_mgr`, `ccs`, `envoy`, `envoy_control_plane`, `crdb_coordinator`,
`stats_archiver`, `redis_exporter`, aggregators…) are why it's a *platform*. You're not running
Redis — you're running a Redis management system that happens to run Redis.

**The operator even has its own operator:** `rec-services-rigger`, whose entire job is creating a
Service for each database you define.

---

## 18. The debugging toolkit

```bash
oc events                                          # everything, one timeline — START HERE
oc get events -n <ns> --sort-by=.lastTimestamp | tail -40
oc describe pod <name> | tail -30                  # the Events: section at the bottom
oc logs deployment/<name> -c <container> --tail=50
oc get pod <name> --show-labels
oc get pod <name> -o jsonpath='{.metadata.annotations.openshift\.io/scc}{"\n"}'
oc get pod <name> -o jsonpath='{.status.qosClass}{"\n"}'
oc api-resources | grep -i <partial-name>          # when you forget a short name
oc explain <kind>.spec.<field>                     # ← verify field names for YOUR version
```

**`oc explain` is the one people skip.** CRD field names and enum values change between operator
versions. Verify against the cluster rather than copying from a blog:

```bash
oc explain redb.spec.persistence
oc explain redb.spec.evictionPolicy
oc get crd redisenterpriseclusters.app.redislabs.com -o jsonpath='{range .spec.versions[*]}{.name}{"\n"}{end}'
```

`oc get` shows **status**. `oc describe` and `oc events` show **why**.

---

## 19. Test it, don't assume it

Habits worth keeping, each of which caught something real here:

- **Prove the StorageClass before building on it** — with a *Deployment*, so it runs under
  `restricted-v2` with a random UID like your real workload will.
- **Verify a security control by watching it refuse you.** `redis-cli PING` without a password
  must return `NOAUTH`. A password you never tested might not be set.
- **Test a NetworkPolicy in both directions.** It must succeed from the allowed namespace and
  *fail* from everywhere else. One-directional testing is not a control.
- **Confirm a selector matches something** before trusting a policy or Service:
  `oc get pod <name> --show-labels`.
- **Kill the pod** (`--grace-period=0 --force`) and check the data came back. That's the
  difference between "I deployed a database" and "I can run a database."
- **Read the numbers as evidence.** `DBSIZE = 1` when it should have been 10000 revealed a
  skipped step more reliably than memory did.
- **Do the capacity arithmetic before applying.** `allocatable − requests`, per node, on three
  separate nodes — not total CPU spread anywhere.

---

## 20. Quick command reference

```bash
# context
oc whoami; oc project; oc version
export KUBECONFIG=/root/kubeconfig-homelab

# discovery
oc get nodes -o wide
oc get storageclass
oc api-resources | grep -i <thing>
oc explain <kind>.spec.<field>

# the honest "what exists here"
oc get all,pvc,configmap,secret,route,networkpolicy

# security introspection
oc get scc -o custom-columns=NAME:.metadata.name,PRIO:.priority,RUNASUSER:.runAsUser.type,FSGROUP:.fsGroup.type
oc get namespace <ns> -o jsonpath='{.metadata.annotations}' | tr ',' '\n'
oc get pod <p> -o jsonpath='{.metadata.annotations.openshift\.io/scc}{"\n"}'

# workload control
oc apply -f x.yaml --dry-run=server
oc scale deployment/<d> --replicas=0        # stop without destroying
oc rollout restart deployment/<d>           # pick up a changed Secret/ConfigMap
oc delete pod -l app=<x> --grace-period=0 --force   # simulate a crash

# access
oc port-forward deployment/<d> 16379:6379 &
oc rsh deployment/<d>
oc exec deployment/<d> -- <cmd>

# operators
oc get packagemanifests -n openshift-marketplace | grep -i <name>
oc get subscription,installplan,csv -n <ns>
oc patch installplan <ip> -n <ns> --type merge -p '{"spec":{"approved":true}}'
```

---

## Related

- `REDIS-ENTERPRISE-RUNBOOK.md` — the production deployment procedure built on these concepts
