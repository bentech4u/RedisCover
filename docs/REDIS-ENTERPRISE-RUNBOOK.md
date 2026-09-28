# Redis Enterprise on OpenShift — Production Runbook

| | |
|---|---|
| **Purpose** | Deploy a highly-available Redis Enterprise cache for the T24 application |
| **Target** | Production OpenShift cluster (9 bare-metal workers, 512 GB RAM each) |
| **Storage** | External Ceph — **RBD (block) only** |
| **Operator** | Redis Enterprise Operator, certified catalog, `production` channel |
| **Version** | 1.0 — 2026-09-28 |
| **Est. duration** | 60–90 min (excluding licence procurement and approvals) |
| **Downtime** | None — new deployment |

---

## 1. Scope

Deploys:

- Redis Enterprise Operator into a dedicated namespace `redis-prod`
- A 3-node `RedisEnterpriseCluster` (REC) with persistent storage on Ceph RBD
- One `RedisEnterpriseDatabase` (REDB) — a **6 GB cache** with HA replication
- A NetworkPolicy restricting access to namespace `prod-t24`
- Connection credentials delivered to `prod-t24` as a Secret

Does **not** cover: Active-Active geo-replication, backup configuration (can be added later), multi-database self-service.

---

## 2. Design decisions and their rationale

### 2.1 Why the operator is NOT installed in the application namespace

The REC must live in the namespace the operator watches. Installing into `prod-t24` would mean:

| Risk | Consequence |
|---|---|
| Lifecycle coupling | Deleting/rebuilding `prod-t24` destroys the database and its PVCs |
| Privilege leakage | Anyone with `edit` on `prod-t24` could attach pods to the operator's ServiceAccount |
| Secret exposure | Database admin credentials readable by all `prod-t24` editors |
| Quota contention | App-sized ResourceQuota would starve a database |
| No sharing | Other namespaces could not consume the platform |

**Rule: anything that holds state or needs elevated privileges gets its own namespace.**

Namespaces are an administrative boundary, not a network boundary. The app reaches Redis across namespaces via FQDN with no extra configuration.

### 2.2 Topology — translating the request

The original request was *"3 instances, 1 primary and 2 replicas"*. That phrasing comes from OSS Redis / Sentinel. Redis Enterprise replicates differently:

```
OSS Redis / Sentinel          Redis Enterprise
────────────────────          ────────────────────────────────────────
1 primary                     1 MASTER SHARD
2 replicas (3 copies)         1 REPLICA SHARD  — max ONE replica per shard
                              (2 copies of the data)

3 pods running redis          3 REC NODES = infrastructure pods.
                              The database's 2 shards occupy 2 of them;
                              the 3rd provides quorum + failover capacity.
```

**Redis Enterprise cannot provide 2 replicas of a shard.** Three data copies requires Active-Active (three separate clusters).

The delivered design provides **high availability**: master and replica shards on different nodes, automatic failover, quorum across 3 nodes. This is the correct Enterprise answer to an HA requirement.

> **If the requirement is literally 3 data copies, this design does not meet it.** Escalate before proceeding.

### 2.3 Sizing

| Parameter | Value | Rationale |
|---|---|---|
| REC nodes | 3 | Minimum for quorum. Odd number avoids split-brain. |
| CPU per node | 4 | Redis published production minimum. 2 CPU is their *development* tier. |
| Memory per node | 12 Gi | Per site standard. **See warning below.** |
| Disk per node | 80 Gi | ~5× node RAM — Redis guidance for AOF/snapshot headroom. |
| Database size | **6 GB** | 12 Gi − ~3 Gi processes − ~1 Gi buffers − ~2 Gi headroom. |
| Shards | 2 | 1 master + 1 replica (`replication: true`). |

> ⚠️ **12 Gi nodes cap the cache at 6 GB.** 8 GB fits arithmetically (~89 % node utilisation) but leaves no room for a client-buffer spike and sits below Redis's ~15 GB/node production minimum. On a 512 GB host, moving to 16 Gi costs 3 % of one machine and raises the ceiling to 8 GB. **The app team must acknowledge the 6 GB limit or approve 16 Gi nodes.**

Total footprint: **12 CPU, 36 Gi RAM, 240 Gi storage**, plus ~1.25 CPU / 1.5 Gi for the operator and services-rigger.

> Because pod anti-affinity is **required**, this needs **three separate worker nodes each able to spare 4 CPU / 12 Gi** — not 12 CPU spread anywhere.

### 2.4 Cache semantics

| Setting | Value | Consequence |
|---|---|---|
| `evictionPolicy` | `allkeys-lru` | At 6 GB, Redis **silently deletes** least-recently-used keys. Writes never fail. |
| `persistence` | `snapshotEvery1Hour` | Warm restart. Up to 1 h of cache lost on full cluster restart. |
| `replication` | `true` | Survives a node failure without a cold cache. |

AOF-every-second was rejected: a pure cache does not need write-ahead durability, and AOF over network-attached Ceph is wasted I/O. Hourly snapshots avoid a **cold-start stampede onto T24**, which is the real risk.

### 2.5 Storage — Ceph RBD, not CephFS

```
Ceph RBD   → RADOS Block Device, ReadWriteOnce
             provisioner contains "rbd"        ✅ USE THIS

CephFS     → POSIX shared filesystem, ReadWriteMany
             provisioner contains "cephfs"     ❌ DO NOT USE
```

Redis Enterprise relies on `fsync` semantics and exclusive file locking. On CephFS the failure mode is not a clean error at deploy time — it is corruption or a stalled node weeks later.

### 2.6 Architecture

```
┌─ namespace: redis-prod ───────────────────┐   ┌─ namespace: prod-t24 ────────┐
│                                            │   │                              │
│  redis-enterprise-operator  (controller)   │   │   T24 application pods       │
│  rec-services-rigger        (svc creator)  │   │                              │
│                                            │   │   envFrom:                   │
│  StatefulSet rec                           │   │     secretRef: redis-creds   │
│    rec-0  [master shard]  ─ PVC 80Gi       │◀──┼── REDIS_HOST                 │
│    rec-1  [replica shard] ─ PVC 80Gi       │   │   REDIS_PORT=12000           │
│    rec-2  [spare/quorum]  ─ PVC 80Gi       │   │   REDIS_PASSWORD             │
│                                            │   │                              │
│  REDB t24-cache  → Service :12000          │   │                              │
│                  → Secret redb-t24-cache   │   │                              │
│                                            │   │                              │
│  Owner: Platform / DBA team                │   │   Owner: T24 application team│
└────────────────────────────────────────────┘   └──────────────────────────────┘
          ▲                                                    │
          └────── NetworkPolicy: only prod-t24, port 12000 ────┘
```

---

## 3. Prerequisites — all must be satisfied before starting

| # | Item | How to confirm | Blocker? |
|---|---|---|---|
| 1 | Production Redis Enterprise **licence file** | Obtained from Redis Ltd | **YES** — trial is 4 shards / 30 days |
| 2 | Ceph **RBD** StorageClass name | §4.2 | **YES** |
| 3 | 3 workers each with ≥ 4 CPU / 12 Gi free | §4.1 | **YES** |
| 4 | Cluster can pull `registry.connect.redhat.com` | §4.3 | **YES** |
| 5 | TLS decision from security team | §2.4 / §8.3 | Recommended before handover |
| 6 | App team acknowledges 6 GB cache + LRU eviction | Written sign-off | **YES** |
| 7 | `cluster-admin` on the target cluster | `oc auth can-i '*' '*' --all-namespaces` | YES |
| 8 | Change ticket approved | — | Per site policy |

---

## 4. Variables — set these once

```bash
export NS_REDIS=redis-prod
export NS_APP=prod-t24
export DB_NAME=t24-cache
export DB_PORT=12000
export SC=<FILL-IN-YOUR-CEPH-RBD-STORAGECLASS>
export LICENSE_FILE=/path/to/redis-enterprise-license.txt
```

Verify they are set before continuing:

```bash
echo "NS_REDIS=$NS_REDIS NS_APP=$NS_APP DB=$DB_NAME PORT=$DB_PORT SC=$SC"
```

> Every later command depends on these. If you open a new shell, re-export them **and** `KUBECONFIG`.

---

## PART A — Pre-flight checks

### A.1 Worker capacity

```bash
oc get nodes -l node-role.kubernetes.io/worker='' -o custom-columns=NAME:.metadata.name,CPU:.status.allocatable.cpu,MEM:.status.allocatable.memory
```

```bash
for n in $(oc get nodes -l node-role.kubernetes.io/worker='' -o name | cut -d/ -f2); do
  echo "== $n"; oc describe node $n | grep -A 6 'Allocated resources'
done
```

**Pass criteria:** at least **three** nodes with `allocatable − requests` ≥ **4 CPU and 12 Gi**.

### A.2 Storage class — the critical check

```bash
oc get storageclass -o custom-columns=NAME:.metadata.name,PROVISIONER:.provisioner,EXPAND:.allowVolumeExpansion,BINDING:.volumeBindingMode,RECLAIM:.reclaimPolicy
```

```bash
oc get sc $SC -o jsonpath='provisioner: {.provisioner}{"\n"}expansion: {.allowVolumeExpansion}{"\n"}reclaim:   {.reclaimPolicy}{"\n"}'
```

**Pass criteria:**

- provisioner contains `rbd` (e.g. `openshift-storage.rbd.csi.ceph.com`) — **NOT `cephfs`**
- `allowVolumeExpansion: true`
- `reclaimPolicy` — `Retain` preferred for production; `Delete` means removing the PVC destroys the RBD image immediately

### A.3 Registry reachability

```bash
oc create namespace $NS_REDIS --dry-run=client -o yaml | oc apply -f -
```

```bash
oc -n $NS_REDIS run regtest --image=registry.connect.redhat.com/redislabs/redis-enterprise-operator:latest --restart=Never --command -- true
```

```bash
oc -n $NS_REDIS get pod regtest -o jsonpath='{.status.phase}{"\n"}'; oc -n $NS_REDIS delete pod regtest --ignore-not-found
```

**Pass criteria:** no `ErrImagePull` / `ImagePullBackOff`.

### A.4 Storage smoke test — prove the Ceph class before you build on it

Uses a **Deployment**, not a bare Pod, so it runs under `restricted-v2` with a random UID — exactly like Redis Enterprise will. A bare Pod created by a cluster-admin gets `anyuid` and proves nothing.

```bash
cat > 00-sc-test.yaml <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: sc-test
  namespace: $NS_REDIS
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: $SC
  resources:
    requests:
      storage: 10Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: sc-test
  namespace: $NS_REDIS
spec:
  replicas: 1
  selector:
    matchLabels: {app: sc-test}
  template:
    metadata:
      labels: {app: sc-test}
    spec:
      containers:
        - name: t
          image: registry.access.redhat.com/ubi9/ubi-minimal:latest
          command: ["sleep","3600"]
          volumeMounts:
            - {name: d, mountPath: /mnt/data}
      volumes:
        - name: d
          persistentVolumeClaim: {claimName: sc-test}
EOF
```

```bash
oc apply -f 00-sc-test.yaml
```

```bash
oc -n $NS_REDIS get pvc,pod -l app=sc-test
```

```bash
oc -n $NS_REDIS rsh deployment/sc-test sh -c 'id; ls -ld /mnt/data; df -hT /mnt/data; dd if=/dev/zero of=/mnt/data/t bs=1M count=512 oflag=direct; rm -f /mnt/data/t'
```

**Pass criteria:**

- `id` shows a high random UID (e.g. `1000xxxxxx`), **not** `uid=0`
- `df -hT` shows `ext4` or `xfs` — **not** `ceph` or `fuse`
- `dd` completes with acceptable throughput
- the non-root UID could write

Persistence check — destroy the pod, keep the disk:

```bash
oc -n $NS_REDIS exec deployment/sc-test -- sh -c 'echo proof > /mnt/data/proof.txt'
```

```bash
oc -n $NS_REDIS delete pod -l app=sc-test
```

```bash
sleep 20; oc -n $NS_REDIS exec deployment/sc-test -- cat /mnt/data/proof.txt
```

Clean up:

```bash
oc delete -f 00-sc-test.yaml
```

### A.5 Zone / rack labels (optional — decides rack awareness)

```bash
oc get nodes -l node-role.kubernetes.io/worker='' -o custom-columns=NAME:.metadata.name,ZONE:'.metadata.labels.topology\.kubernetes\.io/zone'
```

- **Different zone values across nodes** → enable `rackAwarenessNodeLabel` in §7. Master and replica shards will be placed in different failure domains.
- **All `<none>` or all identical** → skip it. A label with one value protects against nothing.

To label by physical rack (one-off):

```bash
oc label node <node-name> topology.kubernetes.io/zone=rack1 --overwrite
```

> A `baremetal=true` label on every node is **not** a failure domain. Rack awareness needs a label whose *value differs* per fault domain.

### A.6 Is pod-network traffic already encrypted?

Informs the TLS decision in §8.3.

```bash
oc get network.operator cluster -o jsonpath='{.spec.defaultNetwork.ovnKubernetesConfig.ipsecConfig}{"\n"}'
```

If IPsec is enabled, pod-to-pod traffic is encrypted at the network layer, which satisfies many policies without database TLS.

---

## PART B — Namespace and licence

```bash
oc create namespace $NS_REDIS 2>/dev/null || echo "namespace exists"
```

```bash
oc label namespace $NS_REDIS \
  pod-security.kubernetes.io/enforce=baseline \
  pod-security.kubernetes.io/audit=restricted \
  pod-security.kubernetes.io/warn=restricted --overwrite
```

Create the licence secret:

```bash
oc create secret generic rec-license -n $NS_REDIS --from-file=license=$LICENSE_FILE
```

```bash
oc get secret rec-license -n $NS_REDIS
```

Confirm the field name your operator version expects:

```bash
oc explain rec.spec --recursive 2>/dev/null | grep -i licen
```

Create the priority class (cluster-scoped, safe to re-run):

```bash
oc create priorityclass redis-prod-critical --value=1000000 \
  --description="Redis Enterprise production databases" 2>/dev/null || echo "exists"
```

---

## PART C — Install the operator

### C.1 Confirm the package name in your catalog

```bash
oc get packagemanifests -n openshift-marketplace -o custom-columns=NAME:.metadata.name,CATALOG:.status.catalogSource,CHANNEL:.status.defaultChannel | grep -i redis
```

Note the exact `NAME` and `CATALOG`. On OpenShift 4.22 this is typically `redis-enterprise-operator-cert` in `certified-operators`.

```bash
oc get packagemanifest redis-enterprise-operator-cert -n openshift-marketplace -o jsonpath='{range .status.channels[*]}{.name}{"\t"}{.currentCSV}{"\n"}{end}'
```

Record the `currentCSV` — you will pin it below.

### C.2 OperatorGroup + Subscription

```bash
cat > 01-operator.yaml <<EOF
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: redis-enterprise-og
  namespace: $NS_REDIS
spec:
  targetNamespaces:
    - $NS_REDIS
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: redis-enterprise-operator-cert
  namespace: $NS_REDIS
spec:
  channel: production
  name: redis-enterprise-operator-cert
  source: certified-operators
  sourceNamespace: openshift-marketplace
  installPlanApproval: Manual
  startingCSV: <PASTE-currentCSV-FROM-C.1>
EOF
```

```bash
oc apply -f 01-operator.yaml
```

> **`installPlanApproval: Manual` and a pinned `startingCSV` are deliberate.** Automatic approval lets a vendor release roll-restart your database cluster unannounced. The console default is Automatic — do not use it for stateful workloads.

### C.3 Approve the install plan

```bash
oc get installplan -n $NS_REDIS
```

```bash
oc patch installplan $(oc get installplan -n $NS_REDIS -o jsonpath='{.items[0].metadata.name}') \
  -n $NS_REDIS --type merge -p '{"spec":{"approved":true}}'
```

```bash
oc get csv -n $NS_REDIS -w
```

**Pass criteria:** `PHASE: Succeeded`. Ctrl+C, then:

```bash
oc get pods -n $NS_REDIS
```

Expect one `redis-enterprise-operator-*` pod, `2/2 Running` (controller + admission webhook).

### C.4 Record what the operator installed (for the change record)

```bash
oc get crd | grep -i redis
```

```bash
oc get scc | grep -i redis
```

```bash
oc get clusterrole,clusterrolebinding | grep -i redis | head -20
```

```bash
oc get validatingwebhookconfiguration | grep -i redis
```

---

## PART D — Create the Redis Enterprise Cluster

```bash
cat > 02-rec.yaml <<EOF
apiVersion: app.redislabs.com/v1
kind: RedisEnterpriseCluster
metadata:
  name: rec
  namespace: $NS_REDIS
spec:
  nodes: 3

  licenseSecretName: rec-license
  priorityClassName: redis-prod-critical

  # ── SIZING ────────────────────────────────────────────────
  # 4 CPU  : Redis published production minimum
  # 12Gi   : site standard. Caps the database at 6GB (see §2.3).
  #          For an 8GB database, raise to 16Gi.
  # requests == limits  →  QoS class GUARANTEED  →  evicted LAST
  redisEnterpriseNodeResources:
    requests:
      cpu: "4"
      memory: 12Gi
    limits:
      cpu: "4"
      memory: 12Gi

  bootstrapperResources:
    requests: {cpu: 100m, memory: 256Mi}
    limits:   {cpu: 200m, memory: 512Mi}

  redisEnterpriseServicesRiggerResources:
    requests: {cpu: 100m, memory: 256Mi}
    limits:   {cpu: 500m, memory: 512Mi}

  # ── STORAGE ───────────────────────────────────────────────
  # MUST be Ceph RBD (block). NOT CephFS.
  persistentSpec:
    enabled: true
    storageClassName: $SC
    volumeSize: 80Gi

  uiServiceType: ClusterIP

  # Uncomment ONLY if A.5 showed DIFFERENT zone values per node:
  # rackAwarenessNodeLabel: topology.kubernetes.io/zone
EOF
```

Validate field names against your operator version, then dry-run:

```bash
oc explain rec.spec 2>/dev/null | grep -iE 'priority|rack|nodeSelector|toleration|license|persistent'
```

```bash
oc apply -f 02-rec.yaml --dry-run=server
```

```bash
oc apply -f 02-rec.yaml
```

```bash
oc get rec,pod,pvc -n $NS_REDIS -w
```

**Expect 5–15 minutes.** The node image is ~1.9 GB per node. Pods start in **parallel** (`podManagementPolicy: Parallel`) and each joins the cluster independently.

During bootstrap you will see repeated:

```
Warning  Unhealthy  Readiness probe failed: node id file does not exist - pod is not yet bootstrapped
```

**This is normal.** It is a *readiness* probe, so the pod is held out of the Service, not restarted. It clears once the node joins.

**Pass criteria:**

```bash
oc get rec rec -n $NS_REDIS
```

```
NODES 3   STATE Running   SPEC STATUS Valid   LICENSE STATE Valid
```

```bash
oc get pod,pvc -n $NS_REDIS
```

Expect `rec-0/1/2` at `2/2 Running`, a `rec-services-rigger-*` pod, and **three** PVCs of 80 Gi each.

Confirm the pods spread across three nodes:

```bash
oc get pod -n $NS_REDIS -o wide | grep '^rec-'
```

Confirm Guaranteed QoS:

```bash
oc get pod rec-0 -n $NS_REDIS -o jsonpath='{.status.qosClass}{"\n"}'
```

---

## PART E — Create the database

### E.1 Verify enum values for your operator version

These differ across releases; a wrong value is rejected at admission.

```bash
oc explain redb.spec.persistence
```

```bash
oc explain redb.spec.evictionPolicy
```

```bash
oc explain redb.spec.tlsMode
```

### E.2 Apply

```bash
cat > 03-redb.yaml <<EOF
apiVersion: app.redislabs.com/v1alpha1
kind: RedisEnterpriseDatabase
metadata:
  name: $DB_NAME
  namespace: $NS_REDIS
spec:
  redisEnterpriseCluster:
    name: rec

  memorySize: 6GB              # 12Gi nodes → 6GB safe ceiling (§2.3)
  shardCount: 1
  replication: true            # master + replica on different nodes

  databasePort: $DB_PORT       # PINNED. Without this the port is random
                               # and CHANGES if the database is recreated.

  # ── CACHE SETTINGS ────────────────────────────────────────
  evictionPolicy: allkeys-lru  # full → evict LRU keys. Writes never fail.
  persistence: snapshotEvery1Hour

  # Add after the security team decides (§8.3):
  # tlsMode: enabled
EOF
```

```bash
oc apply -f 03-redb.yaml --dry-run=server
```

```bash
oc apply -f 03-redb.yaml
```

```bash
oc get redb -n $NS_REDIS -w
```

**Pass criteria:** `STATUS: active`, `SHARDS: 2`, `PORT: 12000`.

```bash
oc get svc,secret -n $NS_REDIS | grep $DB_NAME
```

Expect a `t24-cache` Service, a `t24-cache-headless` Service, and a `redb-t24-cache` Secret (created by the operator).

```bash
oc get rec rec -n $NS_REDIS
```

`SHARDS` should now read `2/<licensed-total>`.

---

## PART F — Verification and failover test

### F.1 Connectivity

```bash
export REDBPW=$(oc get secret redb-$DB_NAME -n $NS_REDIS -o jsonpath='{.data.password}' | base64 -d)
```

```bash
echo "password length = ${#REDBPW}"
```

```bash
oc -n $NS_REDIS run rediscli --image=docker.io/redis:7.4-alpine --restart=Never --command -- sleep 3600
```

```bash
oc -n $NS_REDIS exec rediscli -- redis-cli -h $DB_NAME -p $DB_PORT -a "$REDBPW" --no-auth-warning PING
```

Confirm authentication is actually enforced:

```bash
oc -n $NS_REDIS exec rediscli -- redis-cli -h $DB_NAME -p $DB_PORT PING
```

**Expect `NOAUTH Authentication required.`** A password you never tested might not be set.

```bash
oc -n $NS_REDIS exec rediscli -- redis-cli -h $DB_NAME -p $DB_PORT -a "$REDBPW" --no-auth-warning INFO memory | grep -E 'used_memory_human|maxmemory_human|maxmemory_policy'
```

### F.2 Failover test — mandatory before handover

This is the test that justifies the licence cost. Do not skip it.

```bash
oc -n $NS_REDIS exec rediscli -- redis-cli -h $DB_NAME -p $DB_PORT -a "$REDBPW" --no-auth-warning DEBUG POPULATE 100000
```

```bash
oc -n $NS_REDIS exec rediscli -- redis-cli -h $DB_NAME -p $DB_PORT -a "$REDBPW" --no-auth-warning DBSIZE
```

Record which node holds the master shard, then kill it with no grace period:

```bash
oc get pod -n $NS_REDIS -o wide | grep '^rec-'
```

```bash
oc -n $NS_REDIS delete pod rec-0 --grace-period=0 --force
```

Immediately re-test — measure how long reads fail:

```bash
oc -n $NS_REDIS exec rediscli -- redis-cli -h $DB_NAME -p $DB_PORT -a "$REDBPW" --no-auth-warning DBSIZE
```

```bash
oc get pod,rec,redb -n $NS_REDIS
```

**Pass criteria:** `DBSIZE` returns 100000, the REDB returns to `active`, and `rec-0` rejoins. **Record the observed interruption window** — the app team needs it to configure client retry/timeouts.

```bash
oc -n $NS_REDIS exec rediscli -- redis-cli -h $DB_NAME -p $DB_PORT -a "$REDBPW" --no-auth-warning FLUSHALL
```

```bash
oc -n $NS_REDIS delete pod rediscli
```

### F.3 Management UI (optional)

```bash
oc create route passthrough rec-ui --service=rec-ui --port=8443 -n $NS_REDIS
```

```bash
oc annotate route rec-ui -n $NS_REDIS haproxy.router.openshift.io/balance=source --overwrite
```

> The `balance=source` annotation is **required**. The console keeps server-side session state across 3 pods; without sticky sessions, login silently fails and bounces back to the sign-in page.

```bash
oc get route rec-ui -n $NS_REDIS -o jsonpath='https://{.spec.host}{"\n"}'
```

```bash
oc get secret rec -n $NS_REDIS -o jsonpath='{.data.username}' | base64 -d; echo
```

```bash
oc get secret rec -n $NS_REDIS -o jsonpath='{.data.password}' | base64 -d; echo
```

> **Use the UI for viewing only.** Databases created in the console have no `RedisEnterpriseDatabase` resource, are invisible to `oc get redb` and to Git, and do not survive a cluster rebuild. Always create databases via the CRD.

---

## PART G — Network restriction

### G.1 Label the consumer namespace

> The generated policy selects on `kubernetes.io/metadata.name`, which
> Kubernetes sets automatically on every namespace since 1.21 — verify rather
> than assume, and note that a `namespaceSelector` matching **nothing** denies
> all ingress rather than allowing it:
>
> ```bash
> oc get ns $NS_APP -o jsonpath='{.metadata.labels}'
> ```

### G.2 Find the real pod labels first

```bash
oc get pod rec-0 -n $NS_REDIS --show-labels
```

> A NetworkPolicy whose `podSelector` matches nothing silently protects nothing. Confirm the label before applying.

### G.3 Apply

```bash
cat > 04-networkpolicy.yaml <<EOF
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-app-to-redis
  namespace: $NS_REDIS
spec:
  podSelector:
    matchLabels:
      app: redis-enterprise        # ← REPLACE with a label confirmed in G.2
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: $NS_APP
      ports:
        - protocol: TCP
          port: $DB_PORT
EOF
```

```bash
oc apply -f 04-networkpolicy.yaml
```

### G.4 Test both directions

Must **succeed** from `prod-t24`:

```bash
oc -n $NS_APP run np-ok --image=docker.io/redis:7.4-alpine --restart=Never --command -- sleep 300
```

```bash
oc -n $NS_APP exec np-ok -- redis-cli -h $DB_NAME.$NS_REDIS.svc.cluster.local -p $DB_PORT -a "$REDBPW" --no-auth-warning PING
```

Must **fail** from anywhere else:

```bash
oc create namespace np-test 2>/dev/null; oc -n np-test run np-bad --image=docker.io/redis:7.4-alpine --restart=Never --command -- sleep 300
```

```bash
oc -n np-test exec np-bad -- timeout 10 redis-cli -h $DB_NAME.$NS_REDIS.svc.cluster.local -p $DB_PORT -a "$REDBPW" --no-auth-warning PING
```

**Expect a timeout / connection failure.** A policy tested in only one direction is not a control.

```bash
oc -n $NS_APP delete pod np-ok; oc delete namespace np-test
```

---

## PART H — Handover

Create the credentials in the **application's** namespace so the app team never needs read access to `redis-prod`:

```bash
oc -n $NS_APP create secret generic redis-creds \
  --from-literal=REDIS_HOST=$DB_NAME.$NS_REDIS.svc.cluster.local \
  --from-literal=REDIS_PORT=$DB_PORT \
  --from-literal=REDIS_PASSWORD="$REDBPW"
```

```bash
oc -n $NS_APP get secret redis-creds
```

Their Deployment consumes it — no hardcoded host, no password in Git:

```yaml
spec:
  containers:
    - name: app
      image: <their-image>
      envFrom:
        - secretRef:
            name: redis-creds
```

Verification they can run themselves:

```bash
oc -n $NS_APP exec deployment/<their-deployment> -- sh -c \
  'redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" -a "$REDIS_PASSWORD" --no-auth-warning PING'
```

---

## 5. Handover package — send this to the application team

```
REDIS CACHE — CONNECTION DETAILS
================================

CONNECTION
  Host       : t24-cache.redis-prod.svc.cluster.local
  Port       : 12000                         (NOT 6379)
  Auth       : Secret "redis-creds" in your namespace prod-t24
               keys: REDIS_HOST, REDIS_PORT, REDIS_PASSWORD
  Username   : none (password-only AUTH)
  TLS        : <disabled | enabled — confirm with platform team>
  Reachable  : from namespace prod-t24 ONLY (NetworkPolicy enforced)

CAPACITY AND BEHAVIOUR — READ CAREFULLY
  Cache size : 6 GB
  Eviction   : allkeys-lru
               At 6 GB, Redis SILENTLY DELETES the least-recently-used
               keys to make room. Writes always succeed.
               >> YOUR APPLICATION MUST TREAT EVERY READ AS A POSSIBLE
               >> MISS AND FALL BACK TO THE SOURCE OF TRUTH.
               >> DO NOT STORE ANYTHING HERE THAT ONLY EXISTS HERE.
  Persistence: hourly snapshot. Up to 1 hour of cache lost on a full
               cluster restart. Acceptable for a cache.
  HA         : master + replica shard on separate nodes, automatic
               failover. Expect a brief reconnect during failover.
               >> YOUR CLIENT MUST RETRY ON CONNECTION FAILURE.
               Observed failover window in testing: <FILL IN FROM F.2>

DIFFERENCES FROM STANDARD REDIS
  * Only database 0 exists. SELECT 1..15 WILL FAIL.
    If your code calls SELECT, it must be changed.
  * Port is 12000, not 6379.
  * Connections are proxied through Envoy inside the cluster.
  * Cluster-mode multi-key operations behave as single-shard
    (shardCount = 1), so standard multi-key commands work.

CLIENT REQUIREMENTS
  * Connection pooling: yes
  * Automatic reconnect and retry on failover: REQUIRED
  * Explicit connect/read timeouts: REQUIRED (do not rely on defaults)
  * Set TTLs on cache entries. Do not rely on eviction alone.

ESCALATION
  Platform / DBA team owns namespace redis-prod.
  Application team owns prod-t24 and the client configuration.
```

---

## 6. Day-2 operations

### 6.1 Health checks

```bash
oc get rec,redb,pod,pvc -n $NS_REDIS
```

```bash
oc get rec rec -n $NS_REDIS -o jsonpath='{.status}{"\n"}' | tr ',' '\n'
```

```bash
oc get events -n $NS_REDIS --sort-by=.lastTimestamp | tail -30
```

### 6.2 Monitoring

The REC exposes Prometheus metrics on the headless service `rec-prom:8070`. With OpenShift user-workload monitoring enabled, add a `ServiceMonitor` in `redis-prod`.

Key alerts to configure:

| Metric | Alert when |
|---|---|
| Database memory usage | > 80 % of `memorySize` |
| Evicted keys rate | sustained increase (cache too small) |
| Node memory | > 85 % |
| PVC usage | > 75 % |
| Shard count vs licence | approaching limit |
| REC state | != `Running` |
| Licence expiry | < 30 days |

### 6.3 Adding backup later (non-disruptive)

Backup is a REDB field. Editing it is an in-place config update — no downtime, no recreate.

```bash
oc explain redb.spec.backup
```

Then add a `backup:` block to `03-redb.yaml` and re-apply. For a pure cache this is usually unnecessary — repopulating from the source of truth is preferable to restoring a stale cache.

### 6.4 Growing the cache

**Within current node memory** — edit `memorySize` in `03-redb.yaml` and re-apply. Online operation.

**Beyond 6 GB** — requires larger REC nodes:

1. Raise `redisEnterpriseNodeResources.memory` to `16Gi` in `02-rec.yaml`
2. `oc apply -f 02-rec.yaml`
3. The operator performs a **rolling restart** of REC nodes — schedule a window
4. Then raise `memorySize` in `03-redb.yaml`

### 6.5 Expanding storage

Only if the StorageClass has `allowVolumeExpansion: true`:

```bash
oc get pvc -n $NS_REDIS
```

```bash
oc patch pvc <pvc-name> -n $NS_REDIS -p '{"spec":{"resources":{"requests":{"storage":"120Gi"}}}}'
```

Also update `volumeSize` in `02-rec.yaml` so the manifest matches reality.

### 6.6 Operator upgrades

Approval is **Manual**, so OLM stages upgrades and waits.

```bash
oc get installplan -n $NS_REDIS
```

Before approving: read the Redis release notes, confirm the target version supports your OpenShift version, and **schedule a window** — an operator upgrade can roll-restart the REC.

```bash
oc patch installplan <name> -n $NS_REDIS --type merge -p '{"spec":{"approved":true}}'
```

```bash
oc get csv -n $NS_REDIS -w
```

### 6.7 Password rotation

```bash
oc get secret redb-$DB_NAME -n $NS_REDIS -o jsonpath='{.data.password}' | base64 -d
```

After any rotation, update `redis-creds` in `prod-t24` and restart the application pods:

```bash
oc -n $NS_APP delete secret redis-creds
```

```bash
oc -n $NS_APP create secret generic redis-creds \
  --from-literal=REDIS_HOST=$DB_NAME.$NS_REDIS.svc.cluster.local \
  --from-literal=REDIS_PORT=$DB_PORT \
  --from-literal=REDIS_PASSWORD="<new-password>"
```

```bash
oc -n $NS_APP rollout restart deployment/<their-deployment>
```

---

## 7. Rollback and decommission

### What deletes what

| Command | Pods | PVCs | Data |
|---|---|---|---|
| `oc delete pod rec-0` | recreated | kept | **safe** |
| `oc delete redb t24-cache` | — | kept | **database destroyed** |
| `oc delete rec rec` | gone | **kept** (verify) | recoverable |
| `oc delete pvc ...` | — | gone | **DESTROYED** if reclaimPolicy=Delete |
| `oc delete namespace redis-prod` | gone | gone | **EVERYTHING DESTROYED** |

### Rollback during deployment

```bash
oc delete -f 03-redb.yaml
```

```bash
oc delete -f 02-rec.yaml
```

```bash
oc get pvc -n $NS_REDIS
```

```bash
oc delete pvc -n $NS_REDIS --all
```

```bash
oc delete -f 01-operator.yaml
```

```bash
oc get csv -n $NS_REDIS
```

```bash
oc delete csv --all -n $NS_REDIS
```

```bash
oc delete namespace $NS_REDIS
```

Confirm no orphaned volumes remain:

```bash
oc get pv | grep -E 'redis|NAME'
```

> CRDs are cluster-scoped and survive namespace deletion. Remove them only if no other Redis Enterprise installation exists on the cluster.

---

## 8. Troubleshooting

| Symptom | Likely cause | Command |
|---|---|---|
| REC pods `Pending` | Insufficient CPU/memory on 3 distinct nodes | `oc describe pod rec-0 -n $NS_REDIS \| tail -25` |
| `didn't match pod anti-affinity` | Fewer than 3 eligible worker nodes | `oc get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints` |
| PVC stuck `Pending` | Wrong StorageClass, or CSI driver down | `oc describe pvc <name> -n $NS_REDIS \| tail -20` |
| `ErrImagePull` | Cluster pull secret lacks `registry.connect.redhat.com` | `oc get events -n $NS_REDIS \| grep -i pull` |
| Readiness probe: `node id file does not exist` | **Normal during bootstrap** — wait | `oc logs rec-0 -n $NS_REDIS -c bootstrapper` |
| REDB stuck `pending` | Insufficient shards on licence, or REC not `Running` | `oc get rec rec -n $NS_REDIS`; `oc describe redb $DB_NAME -n $NS_REDIS` |
| UI login bounces back to sign-in | Missing sticky sessions on the Route | `oc annotate route rec-ui -n $NS_REDIS haproxy.router.openshift.io/balance=source --overwrite` |
| App cannot connect | NetworkPolicy podSelector matches nothing, or wrong FQDN | `oc get pod rec-0 -n $NS_REDIS --show-labels` |
| `NOAUTH` from app | Secret not mounted / stale password | `oc -n $NS_APP get secret redis-creds -o jsonpath='{.data}'` |
| Writes rejected | `evictionPolicy` is `noeviction` — should be `allkeys-lru` for a cache | `oc get redb $DB_NAME -n $NS_REDIS -o jsonpath='{.spec.evictionPolicy}'` |
| Operator not reconciling | Controller crash or RBAC issue | `oc logs deployment/redis-enterprise-operator -n $NS_REDIS -c redis-enterprise-operator --tail=50` |

**Universal first step:**

```bash
oc get events -n $NS_REDIS --sort-by=.lastTimestamp | tail -40
```

---

## 9. Open items requiring sign-off

| # | Item | Owner | Status |
|---|---|---|---|
| 1 | Ceph **RBD** StorageClass name confirmed (not CephFS) | Platform | ☐ |
| 2 | Production licence file obtained | Platform / Procurement | ☐ |
| 3 | TLS on the database connection — required or not | Security | ☐ |
| 4 | Rack/zone labels — enable rack awareness or not | Platform | ☐ |
| 5 | **App team acknowledges 6 GB cache limit** (or approves 16 Gi nodes) | T24 App Team | ☐ |
| 6 | **App team acknowledges LRU eviction** — every read may miss | T24 App Team | ☐ |
| 7 | App client configured with retry + timeouts | T24 App Team | ☐ |
| 8 | App confirmed not to use `SELECT` (only DB 0 exists) | T24 App Team | ☐ |
| 9 | Failover window from §F.2 recorded and communicated | Platform | ☐ |
| 10 | Monitoring and alerts configured (§6.2) | Platform | ☐ |

> Items **5, 6 and 8** are the ones that cause production incidents. Get them in writing.

---

## 10. Manifest inventory

| File | Creates | Part |
|---|---|---|
| `00-sc-test.yaml` | Storage validation (temporary) | A.4 |
| `01-operator.yaml` | OperatorGroup + Subscription | C.2 |
| `02-rec.yaml` | RedisEnterpriseCluster | D |
| `03-redb.yaml` | RedisEnterpriseDatabase | E.2 |
| `04-networkpolicy.yaml` | NetworkPolicy | G.3 |

Commit `01` through `04` to Git. They are the source of truth — **anything created through the web console is invisible to them.**
