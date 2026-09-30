from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    server: str
    username: str
    password: str
    insecure: bool = True


class CommunitySpec(BaseModel):
    namespace: str = Field(default="redis", pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    name: str = Field(default="redis", pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

    version_id: str = "redis-7.4"
    custom_image: Optional[str] = None

    # standalone = Deployment; replication = StatefulSet, <name>-0 is the primary
    topology: Literal["standalone", "replication"] = "standalone"
    replicas: int = 3

    # auth
    password: Optional[str] = None          # blank -> generated
    auth_enabled: bool = True

    # storage
    persistence: Literal["aof", "rdb", "both", "none"] = "aof"
    appendfsync: Literal["always", "everysec", "no"] = "everysec"
    storage_class: Optional[str] = None     # None -> resolve the cluster default
    storage_size: str = "5Gi"
    allow_file_storage: bool = False        # explicit override for NFS/CephFS

    # memory / cpu
    maxmemory: str = "256mb"
    maxmemory_policy: str = "allkeys-lru"
    cpu_request: str = "100m"
    cpu_limit: str = "500m"
    memory_request: str = "128Mi"
    memory_limit: str = "512Mi"

    # version-gated extras
    maxmemory_clients: Optional[str] = None   # Redis 7.4+
    io_threads: Optional[int] = None          # Redis 6+

    # networking
    service_type: Literal["ClusterIP", "NodePort"] = "ClusterIP"
    node_port: Optional[int] = None
    allow_namespaces: list[str] = []          # empty -> no NetworkPolicy


class EnterpriseSpec(BaseModel):
    namespace: str = Field(default="redis-enterprise", pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

    # operator
    package: str = "redis-enterprise-operator-cert"
    catalog_source: str = "certified-operators"
    channel: str = "production"
    starting_csv: Optional[str] = None
    approval: Literal["Manual", "Automatic"] = "Manual"

    # licence (paste the file contents; optional -> trial mode)
    license_text: Optional[str] = None

    # cluster
    rec_name: str = "rec"
    nodes: int = 3
    cpu: str = "4"
    memory: str = "16Gi"
    storage_class: Optional[str] = None
    volume_size: str = "80Gi"
    rack_aware_label: Optional[str] = None
    priority_class: Optional[str] = None

    # database
    create_db: bool = True
    db_name: str = "redis-db"
    db_memory: str = "2GB"
    db_port: int = 12000
    shard_count: int = 1
    replication: bool = True
    db_eviction: str = "allkeys-lru"
    db_persistence: str = "snapshotEvery1Hour"
    tls_mode: Optional[str] = None

    # management console: HTTPS, so a passthrough Route works (unlike Redis itself)
    expose_ui: bool = False

    allow_namespaces: list[str] = []


class UninstallSpec(BaseModel):
    kind: Literal["community", "opstree", "enterprise"]
    cr_plural: Optional[str] = None     # opstree: which CR to delete
    namespace: str
    name: str = "redis"
    workload: Literal["deployment", "statefulset"] = "deployment"
    managed: bool = True          # False -> delete by name, not by our label
    delete_pvc: bool = False
    delete_namespace: bool = False


class OperatorInstallSpec(BaseModel):
    package: str
    catalog_source: str
    channel: str
    starting_csv: Optional[str] = None
    install_mode: Literal["AllNamespaces", "OwnNamespace", "SingleNamespace"] = "AllNamespaces"
    namespace: str = "openshift-operators"
    target_namespace: Optional[str] = None
    approval: Literal["Manual", "Automatic"] = "Manual"


class OpstreeSpec(BaseModel):
    namespace: str = Field(default="redis-ot", pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    name: str = Field(default="redis", pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

    topology: Literal["standalone", "replication", "sentinel", "cluster"] = "standalone"
    version_id: str = "v8.2"
    custom_image: Optional[str] = None

    size: int = 3                       # replicas / sentinels / leader shards
    password: Optional[str] = None
    auth_enabled: bool = True

    # sentinel only -- which RedisReplication to watch
    replication_name: Optional[str] = None
    master_group: str = "myMaster"
    quorum: int = 2
    # write `sentinel auth-pass` so Sentinel can authenticate to the primary.
    # Without it Sentinel marks the primary s_down and can never fail over.
    sentinel_auth_pass: bool = True

    storage_class: Optional[str] = None
    storage_size: str = "5Gi"
    persistence: bool = True

    # cache capacity: RAM. Delivered through the operator's additionalRedisConfig.
    maxmemory: str = "256mb"
    maxmemory_policy: str = "allkeys-lru"
    extra_config: Optional[str] = None

    cpu_request: str = "100m"
    cpu_limit: str = "500m"
    memory_request: str = "128Mi"
    memory_limit: str = "512Mi"

    install_operator: bool = True       # install it first if it is not there
    # the v0.15.1 bundle is missing RBAC for two of its own controllers
    fix_operator_rbac: bool = True
    operator_namespace: str = "openshift-operators"
    operator_channel: str = "stable"

    allow_namespaces: list[str] = []


class TestRunSpec(BaseModel):
    kind: Literal["community", "opstree", "enterprise"]
    namespace: str
    name: str
    # Opstree releases are identified by CR kind as well as name
    topology: Optional[str] = None
    password: Optional[str] = None      # override when the Secret cannot be read
    client_image: Optional[str] = None  # for clusters that cannot reach docker.io
    tests: list[str] = []
    client_namespace: Optional[str] = None
    confirm_disruptive: bool = False
