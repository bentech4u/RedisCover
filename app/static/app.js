const $ = (id) => document.getElementById(id);
// A failed call used to reject with nothing listening, so a dead session made
// every button look broken. Surface it instead.
function showError(msg, kind = 'e') {
  let el = document.getElementById('globalErr');
  if (!el) {
    el = document.createElement('div');
    el.id = 'globalErr';
    el.style.cssText = 'position:fixed;top:64px;right:20px;max-width:440px;z-index:200';
    document.body.appendChild(el);
  }
  el.className = 'alert ' + kind;
  el.innerHTML = `<b style="float:right;cursor:pointer;margin-left:10px">&times;</b>${msg}`;
  el.querySelector('b').onclick = () => el.remove();
  clearTimeout(el._t);
  el._t = setTimeout(() => el.remove(), 15000);
}

const api = async (path, opts = {}) => {
  let r;
  try {
    r = await fetch(path, {
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
  } catch (netErr) {
    showError(`Cannot reach the server — is it still running? (${netErr.message})`);
    throw netErr;
  }
  const txt = await r.text();
  let body; try { body = JSON.parse(txt); } catch { body = { detail: txt }; }

  if (r.status === 401) {
    // sessions are in memory, so restarting the app logs everyone out
    showError('Session expired — the server was restarted. Log in again.', 'w');
    document.getElementById('panelMain').classList.add('hide');
    document.getElementById('panelLogin').classList.remove('hide');
    document.getElementById('whoami').classList.add('hide');
    document.getElementById('btnLogout').classList.add('hide');
    throw new Error('Not logged in');
  }
  if (!r.ok) {
    showError(typeof body.detail === 'string' ? body.detail : r.statusText);
    throw new Error(body.detail || r.statusText);
  }
  return body;
};

// last resort: nothing should fail silently
window.addEventListener('unhandledrejection', (e) => {
  const m = (e.reason && e.reason.message) || String(e.reason || '');
  if (m && m !== 'Not logged in') showError(m);
});

let STATE = { kind: null, preflight: null, community: null, enterprise: null };

/* ------------------------------------------------ login */

(async () => {
  try {
    const d = await api('/api/detect-server');
    if (d.server) $('server').value = d.server;
  } catch {}
  try {
    const me = await api('/api/whoami');
    afterLogin(me);
  } catch {}
})();

$('btnSkipInspect').onclick = () => {
  $('credBlock').classList.remove('hide');
  $('skipInspectRow').classList.add('hide');
  $('insecureHint').innerHTML =
    '<span class="warn">You did not inspect the endpoint — you are trusting whatever certificate it presents.</span>';
};

$('btnInspect').onclick = async () => {
  const btn = $('btnInspect');
  const panel = $('inspectPanel');
  btn.disabled = true; btn.textContent = 'Checking…';
  panel.classList.remove('hide');
  panel.innerHTML = '<div class="alert i"><span class="spin"></span> contacting the endpoint…</div>';
  try {
    const d = await api('/api/inspect', {
      method: 'POST', body: JSON.stringify({ server: $('server').value.trim() }),
    });
    renderInspect(d);
  } catch (e) {
    panel.innerHTML = `<div class="alert e">${e.message}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = 'Inspect';
  }
};

function renderInspect(d) {
  const panel = $('inspectPanel');
  if (d.error) {
    panel.innerHTML = `<div class="alert e">${d.error}</div>`;
    return;
  }
  const c = d.cert || {}, cl = d.cluster || {};
  const rows = [];
  if (cl.cluster_domain) rows.push(['Cluster', cl.cluster_domain]);
  if (cl.kubernetes) rows.push(['Kubernetes', cl.kubernetes]);
  if (cl.console) rows.push(['Console', `<a href="${cl.console}" target="_blank" rel="noopener">${cl.console}</a>`]);
  if (cl.oauth_issuer) rows.push(['OAuth issuer', cl.oauth_issuer]);
  rows.push(['Endpoint', `${d.host}:${d.port}`]);
  if (c.subject) rows.push(['Certificate subject', c.subject]);
  if (c.issuer) rows.push(['Issued by', c.issuer]);
  if (c.sans) rows.push(['Valid for', c.sans.join(', ')]);
  if (c.not_after) rows.push(['Expires', `${c.not_after}` +
    (c.days_until_expiry != null ? `  (${c.days_until_expiry} days)` : '')]);
  rows.push(['System CA trust', d.trusted
    ? '<span class="ok">trusted</span>'
    : `<span class="warn">not trusted — ${d.verify_error}</span>`]);

  const warn = (d.warnings || []).map(w => `<div class="alert w">${w}</div>`).join('');

  panel.innerHTML = `
    <div class="alert ${d.trusted ? 'i' : 'w'}"><strong>
      ${cl.cluster_domain ? 'This is cluster <span class="mono">' + cl.cluster_domain + '</span>'
        : 'Endpoint reachable'}</strong>
      — check it is the one you intend before sending credentials.</div>
    <div class="kv">${rows.map(([k, v]) => `<div>${k}</div><div class="mono">${v}</div>`).join('')}</div>
    ${c.sha256 ? `<h3 style="font-size:13px;margin:18px 0 6px">SHA-256 fingerprint</h3>
      <pre style="max-height:none;font-size:11.5px">${c.sha256}</pre>
      <div class="hint">Compare this with a value you already trust. On a machine that
        has the cluster's kubeconfig:
        <span class="mono">openssl s_client -connect ${d.host}:${d.port} &lt;/dev/null 2>/dev/null
        | openssl x509 -noout -fingerprint -sha256</span></div>` : ''}
    ${warn}
    <div class="row">
      <label class="chk"><input type="checkbox" id="certOk">
        ${d.trusted ? 'This is the cluster I intend to connect to'
                    : 'I have verified this fingerprint and this is the cluster I intend to connect to'}</label>
    </div>
    <div class="row"><button id="btnProceed" disabled>Continue to sign in</button></div>`;

  $('certOk').onchange = () => { $('btnProceed').disabled = !$('certOk').checked; };
  $('btnProceed').onclick = () => {
    $('credBlock').classList.remove('hide');
    $('skipInspectRow').classList.add('hide');
    // a cluster-internal CA is normal; keep skip-verify on but say why
    $('insecure').checked = !d.trusted;
    $('insecureHint').innerHTML = d.trusted
      ? '<span class="ok">Certificate validates against the system CA bundle, so verification stays on.</span>'
      : '<span class="dim">Left on: this certificate is signed by a cluster-internal CA, which the system bundle cannot validate. You confirmed the fingerprint above.</span>';
    $('username').focus();
    $('credBlock').scrollIntoView({ behavior: 'smooth' });
  };
}

$('btnLogin').onclick = async () => {
  const btn = $('btnLogin');
  btn.disabled = true; btn.textContent = 'Connecting...';
  $('loginErr').classList.add('hide');
  try {
    const me = await api('/api/login', {
      method: 'POST',
      body: JSON.stringify({
        server: $('server').value.trim(),
        username: $('username').value.trim(),
        password: $('password').value,
        insecure: $('insecure').checked,
      }),
    });
    $('password').value = '';
    afterLogin(me);
  } catch (e) {
    $('loginErr').textContent = e.message;
    $('loginErr').classList.remove('hide');
  } finally {
    btn.disabled = false; btn.textContent = 'Connect';
  }
};

$('password').addEventListener('keydown', e => { if (e.key === 'Enter') $('btnLogin').click(); });

function afterLogin(me) {
  $('panelLogin').classList.add('hide');
  $('panelMain').classList.remove('hide');
  const b = $('whoami');
  b.textContent = `${me.user} @ ${me.server.replace(/^https?:\/\//, '')}`;
  b.classList.remove('hide'); b.classList.add('ok');
  $('btnLogout').classList.remove('hide');
  loadPreflight();
  loadCommunityVersions();
  loadNamespaces();
}

$('btnLogout').onclick = async () => {
  await api('/api/logout', { method: 'POST' });
  location.reload();
};

/* ------------------------------------------------ tabs */

document.querySelectorAll('.tab').forEach(t => {
  t.onclick = () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    ['deploy', 'cluster', 'sizing', 'day2', 'operators', 'tests', 'console', 'status', 'uninstall'].forEach(n => {
      $('tab' + n[0].toUpperCase() + n.slice(1)).classList.toggle('hide', n !== t.dataset.tab);
    });
  };
});

/* ------------------------------------------------ preflight */

async function loadPreflight() {
  const d = await api('/api/preflight');
  STATE.preflight = d;

  $('clusterSub').innerHTML =
    `OpenShift ${d.openshift_version || '?'} &mdash; ${d.nodes.length} nodes, ` +
    `${d.schedulable_workers} schedulable, ${d.storage_classes.length} storage classes<br>` +
    `<span class="mono" style="font-size:11.5px">internal DNS: ${d.cluster_domain || '?'}` +
    `${d.ingress_domain ? '  &middot;  ingress: *.' + d.ingress_domain : ''}</span>`;

  $('nodeTable').innerHTML =
    '<tr><th>Name</th><th>Roles</th><th>CPU</th><th>Memory</th><th>Zone</th><th>Schedulable</th></tr>' +
    d.nodes.map(n => `<tr><td class="mono">${n.name}</td><td>${n.roles.join(', ')}</td>
      <td class="mono">${n.cpu}</td><td class="mono">${n.memory}</td>
      <td class="mono">${n.zone || '-'}</td>
      <td class="${n.schedulable ? 'ok' : 'dim'}">${n.schedulable ? 'yes' : 'tainted'}</td></tr>`).join('');

  $('scTable').innerHTML =
    '<tr><th>Name</th><th>Provisioner</th><th>Type</th><th>Binding</th><th>Expand</th><th>Reclaim</th></tr>' +
    d.storage_classes.map(s => `<tr>
      <td class="mono">${s.name}${s.default ? ' <span class="dim">(default)</span>' : ''}</td>
      <td class="mono" style="font-size:11px">${s.provisioner}</td>
      <td class="${s.kind === 'block' ? 'ok' : (s.kind === 'file' ? 'err' : 'warn')}">${s.kind}</td>
      <td>${s.binding}</td><td>${s.expansion ? 'yes' : 'no'}</td><td>${s.reclaim}</td></tr>`).join('');

  const warns = [];
  if (d.schedulable_workers < 3)
    warns.push(`Only ${d.schedulable_workers} schedulable nodes. Redis Enterprise needs 3 - its pod anti-affinity is required, so extra replicas stay Pending.`);
  if (!d.storage_classes.some(s => s.default))
    warns.push('No default StorageClass. A PVC without an explicit class will stay Pending forever.');
  if (!d.storage_classes.some(s => s.kind === 'block'))
    warns.push('No block StorageClass detected. Databases on NAS/NFS risk corruption - fsync and locking behave differently.');
  const fileDefault = d.storage_classes.find(s => s.default && s.kind !== 'block');
  if (fileDefault)
    warns.push(`The default StorageClass '${fileDefault.name}' is ${fileDefault.kind} storage. A PVC that does not name a class lands there - pick a block class explicitly for any database.`);
  $('clusterWarn').innerHTML = warns.map(w => `<div class="alert w">${w}</div>`).join('');

  // storage class dropdowns
  const opts = '<option value="">Cluster default</option>' +
    d.storage_classes.map(s =>
      `<option value="${s.name}" data-kind="${s.kind}">${s.name}${s.default ? ' (default)' : ''} - ${s.kind}</option>`).join('');
  $('cStorageClass').innerHTML = opts;
  $('eStorageClass').innerHTML = opts;
  [['cStorageClass', 'cScHint'], ['eStorageClass', 'eScHint']].forEach(([sel, hint]) => {
    $(sel).onchange = () => {
      const o = $(sel).selectedOptions[0];
      const k = o.dataset.kind;
      $(hint).innerHTML =
        !o.value ? (() => {
          const def = (STATE.preflight?.storage_classes || []).find(x => x.default);
          return def && def.kind !== 'block'
            ? `<span class="err">Cluster default is <b>${def.name}</b> (${def.kind} storage) - not suitable for a database. Pick a block class.</span>`
            : 'Uses the cluster default; it will be pinned into the manifest.';
        })() :
        k === 'file' ? '<span class="err">NAS/file storage. Not suitable for a database - fsync and locking differ.</span>' :
        k === 'block' ? '<span class="ok">Block storage - correct for a database.</span>' :
        k === 'unknown' ? '<span class="warn">Could not classify this provisioner. Confirm it presents a block device before using it.</span>' : '';
    };
  });

  $('eNodesHint').textContent = `${d.schedulable_workers} schedulable nodes available.`;
  $('eRackHint').textContent = d.zones_distinct > 1
    ? `${d.zones_distinct} distinct zones found - worth enabling.`
    : 'No distinct zone values found; enabling this protects against nothing.';
}

$('refreshCluster').onclick = loadPreflight;

/* ------------------------------------------------ choose kind */

document.querySelectorAll('.opt').forEach(o => {
  o.onclick = async () => {
    document.querySelectorAll('.opt').forEach(x => x.classList.remove('sel'));
    o.classList.add('sel');
    STATE.kind = o.dataset.kind;
    $('formCommunity').classList.toggle('hide', STATE.kind !== 'community');
    $('formOpstree').classList.toggle('hide', STATE.kind !== 'opstree');
    $('formEnterprise').classList.toggle('hide', STATE.kind !== 'enterprise');
    $('panelPreview').classList.add('hide');
    if (STATE.kind === 'enterprise' && !STATE.enterprise) await loadEnterpriseVersions();
    if (STATE.kind === 'opstree' && !STATE.opstree) await loadOpstree();
  };
});

/* ------------------------------------------------ community */

async function loadCommunityVersions() {
  const d = await api('/api/versions/community');
  STATE.community = d;
  $('cVersion').innerHTML = d.versions.map(v =>
    `<option value="${v.id}">${v.label}</option>`).join('');
  $('cEviction').innerHTML = d.eviction_policies.map(e =>
    `<option value="${e.value}"${e.value === 'allkeys-lru' ? ' selected' : ''}>${e.label}</option>`).join('');
  $('cPersistence').innerHTML = d.persistence_modes.map(p =>
    `<option value="${p.value}">${p.label}</option>`).join('');
  ['cMaxmemory', 'cMaxmemoryUnit', 'cMemLim'].forEach(id => { const e=$(id); if(e){e.addEventListener('input',()=>checkMem('c')); e.addEventListener('change',()=>checkMem('c'));} });
  checkMem('c');
  $('cVersion').onchange = applyVersionFeatures;
  $('cTopology').onchange = applyCTopology;
  applyCTopology();
  $('cPersistence').onchange = applyPersistence;
  $('cServiceType').onchange = () =>
    $('wrapNodePort').classList.toggle('hide', $('cServiceType').value !== 'NodePort');
  applyVersionFeatures();
  applyPersistence();
}

function applyVersionFeatures() {
  const v = STATE.community.versions.find(x => x.id === $('cVersion').value);
  if (!v) return;
  $('cVersionNote').innerHTML = `<span class="mono">${v.image}</span><br>${v.notes}`;
  document.querySelectorAll('.feat').forEach(el => {
    el.classList.toggle('hide', !v.features.includes(el.dataset.feat));
  });
  setMaxmem('cMaxmemory', redisBytes(v.defaults.maxmemory));
  $('cMemLim').value = v.defaults.memory_limit;
  $('cCpuLim').value = v.defaults.cpu_limit;
  if (!v.features.includes('multipart_aof')) {
    $('cVersionNote').innerHTML +=
      '<br><span class="warn">Single-file AOF: rewrites need roughly 2x the dataset in free disk.</span>';
  }
}

function applyPersistence() {
  const p = $('cPersistence').value;
  $('wrapFsync').classList.toggle('hide', !(p === 'aof' || p === 'both'));
  const none = p === 'none';
  $('wrapStorage').classList.toggle('hide', none);
  $('wrapSize').classList.toggle('hide', none);
}

$('cGenPw').onclick = async (e) => {
  e.preventDefault();
  $('cPassword').value = (await api('/api/genpassword')).password;
};

function communitySpec() {
  const s = {
    namespace: $('cNamespace').value.trim(),
    name: $('cName').value.trim(),
    version_id: $('cVersion').value,
    topology: $('cTopology').value,
    replicas: parseInt($('cReplicas').value) || 3,
    custom_image: $('cCustomImage').value.trim() || null,
    password: $('cPassword').value || null,
    auth_enabled: true,
    persistence: $('cPersistence').value,
    appendfsync: $('cFsync').value,
    storage_class: $('cStorageClass').value || null,
    allow_file_storage: $('cAllowFile').checked,
    storage_size: $('cStorageSize').value.trim(),
    maxmemory: maxmemValue('cMaxmemory'),
    maxmemory_policy: $('cEviction').value,
    cpu_request: $('cCpuReq').value.trim(),
    cpu_limit: $('cCpuLim').value.trim(),
    memory_request: $('cMemReq').value.trim(),
    memory_limit: $('cMemLim').value.trim(),
    maxmemory_clients: $('cMaxClients').value.trim() || null,
    io_threads: parseInt($('cIoThreads').value) || null,
    service_type: $('cServiceType').value,
    node_port: parseInt($('cNodePort').value) || null,
    allow_namespaces: nsPicked('cAllowNs'),
    users: CUSERS.filter(u => u.username.trim()),
    mirror: mirrorSettings(),
  };
  return s;
}

$('cPreview').onclick = async () => showPreview(
  await api('/api/preview/community', { method: 'POST', body: JSON.stringify(communitySpec()) }));

$('cDeploy').onclick = async () => {
  const j = await api('/api/deploy/community', { method: 'POST', body: JSON.stringify(communitySpec()) });
  streamJob(j.job_id, () => { RELEASES = []; });
};

/* ------------------------------------------------ enterprise */

async function loadEnterpriseVersions() {
  const d = await api('/api/versions/enterprise');
  STATE.enterprise = d;
  if (!d.packages.length) {
    $('entWarn').innerHTML =
      '<div class="alert e">No Redis operator found in this cluster\'s catalog sources. Check that OperatorHub is reachable and the certified-operators CatalogSource is healthy.</div>';
    return;
  }
  $('entWarn').innerHTML = '';
  $('ePackage').innerHTML = d.packages.map(p =>
    `<option value="${p.name}">${p.name} - ${p.catalog}</option>`).join('');
  $('eDbEviction').innerHTML = d.eviction_policies.map(e =>
    `<option value="${e}"${e === 'allkeys-lru' ? ' selected' : ''}>${e}</option>`).join('');
  $('eDbPersistence').innerHTML = d.persistence_modes.map(p =>
    `<option value="${p}"${p === 'snapshotEvery1Hour' ? ' selected' : ''}>${p}</option>`).join('');
  $('ePackage').onchange = fillChannels;
  $('eChannel').onchange = fillCsv;
  fillChannels();
}

function currentPackage() {
  return STATE.enterprise.packages.find(p => p.name === $('ePackage').value);
}

function fillChannels() {
  const p = currentPackage();
  $('eChannel').innerHTML = p.channels.map(c =>
    `<option value="${c.name}"${c.name === p.default_channel ? ' selected' : ''}>${c.name}</option>`).join('');
  fillCsv();
}

function fillCsv() {
  const p = currentPackage();
  const c = p.channels.find(x => x.name === $('eChannel').value);
  $('eCsv').value = c ? c.csv : '';
}

$('eCreateDb').onchange = () => $('dbFields').classList.toggle('hide', !$('eCreateDb').checked);

function parseGi(s) { const m = /^(\d+(?:\.\d+)?)\s*Gi?$/i.exec((s || '').trim()); return m ? parseFloat(m[1]) : null; }
function parseGB(s) { const m = /^(\d+(?:\.\d+)?)\s*GB$/i.exec((s || '').trim()); return m ? parseFloat(m[1]) : null; }

function checkEnterpriseMemory() {
  const node = parseGi($('eMemory').value), db = parseGB($('eDbMemory').value);
  if (!node) return;
  const usable = Math.max(0, node - 4);
  $('eMemHint').innerHTML = `Usable dataset on a ${node}Gi node: about <strong>${usable}GB</strong> after Redis Enterprise overhead.`;
  if (db && db > usable) {
    $('eDbMemHint').innerHTML = `<span class="err">${db}GB will not fit on ${node}Gi nodes. Use ${usable}GB or smaller, or raise node memory.</span>`;
  } else if (db) {
    $('eDbMemHint').innerHTML = `Fits with room to spare.`;
  }
}
['eMemory', 'eDbMemory'].forEach(id => $(id).addEventListener('input', checkEnterpriseMemory));

function enterpriseSpec() {
  return {
    namespace: $('eNamespace').value.trim(),
    package: $('ePackage').value,
    catalog_source: (currentPackage() || {}).catalog || 'certified-operators',
    channel: $('eChannel').value,
    starting_csv: $('eCsv').value || null,
    approval: $('eApproval').value,
    license_text: $('eLicense').value.trim() || null,
    rec_name: $('eRecName').value.trim(),
    nodes: parseInt($('eNodes').value),
    cpu: $('eCpu').value.trim(),
    memory: $('eMemory').value.trim(),
    storage_class: $('eStorageClass').value || null,
    volume_size: $('eVolumeSize').value.trim(),
    rack_aware_label: $('eRack').value || null,
    priority_class: null,
    create_db: $('eCreateDb').checked,
    db_name: $('eDbName').value.trim(),
    db_memory: $('eDbMemory').value.trim(),
    db_port: parseInt($('eDbPort').value),
    shard_count: parseInt($('eShards').value),
    replication: $('eReplication').value === 'true',
    db_eviction: $('eDbEviction').value,
    db_persistence: $('eDbPersistence').value,
    tls_mode: $('eTls').value || null,
    expose_ui: $('eExposeUi').checked,
    allow_namespaces: nsPicked('eAllowNs'),
  };
}

$('ePreview').onclick = async () => showPreview(
  await api('/api/preview/enterprise', { method: 'POST', body: JSON.stringify(enterpriseSpec()) }));

$('eDeploy').onclick = async () => {
  const s = enterpriseSpec();
  if (!s.license_text && !confirm('No licence supplied.\n\nThe cluster will run in TRIAL mode: 4 shards, 30 days.\n\nContinue?')) return;
  const j = await api('/api/deploy/enterprise', { method: 'POST', body: JSON.stringify(s) });
  streamJob(j.job_id, () => { RELEASES = []; });
};

/* ------------------------------------------------ preview + job */

function showPreview(d) {
  $('previewYaml').textContent = d.yaml;
  $('panelPreview').classList.remove('hide');
  $('panelPreview').scrollIntoView({ behavior: 'smooth' });
}
$('closePreview').onclick = () => $('panelPreview').classList.add('hide');
$('copyYaml').onclick = () => navigator.clipboard.writeText($('previewYaml').textContent);

function streamJob(id, onDone) {
  $('panelPreview').classList.add('hide');
  $('panelResult').classList.add('hide');
  $('panelJob').classList.remove('hide');
  $('jobSpin').classList.remove('hide');
  $('jobSub').textContent = 'Running - the log streams live.';
  const log = $('log');
  log.textContent = '';
  $('panelJob').scrollIntoView({ behavior: 'smooth' });

  const es = new EventSource(`/api/job/${id}/stream`);
  es.onmessage = (ev) => {
    const d = JSON.parse(ev.data);
    if (d.line !== undefined) {
      log.textContent += d.line + '\n';
      log.scrollTop = log.scrollHeight;
      return;
    }
    if (d.done) {
      es.close();
      $('jobSpin').classList.add('hide');
      const ok = d.status === 'succeeded';
      $('jobSub').innerHTML = ok
        ? '<span class="ok">Completed successfully.</span>'
        : `<span class="err">Failed: ${d.error}</span>`;
      if (ok) renderResult(d.result);
      // refresh dependent views only once the work has actually finished --
      // a timer fired at submit time reads the cluster before anything is gone
      if (typeof onDone === 'function') onDone(d.status, d.result);
    }
  };
  es.onerror = () => { es.close(); $('jobSpin').classList.add('hide'); };
}

function renderResult(r) {
  if (!r || !r.host) return;

  const rows = [
    ['In-cluster host', r.host],
    ['Port', r.port],
    ['Password', r.password || '(no auth)'],
  ];
  if (r.secret) rows.push(['Secret', `${r.secret} (namespace ${r.namespace})`]);
  if (r.scc) rows.push(['SCC', r.scc]);
  if (r.runAsUser) rows.push(['runAsUser / fsGroup', `${r.runAsUser} / ${r.fsGroup || '-'}`]);
  if (r.qos) rows.push(['QoS class', r.qos]);
  if (r.ui_url) rows.push(['Console', `<a href="${r.ui_url}" target="_blank" rel="noopener">${r.ui_url}</a>`]);
  if (r.ui_user) rows.push(['Console login', `${r.ui_user} / ${r.ui_password || '(secret ' + r.ui_secret + ')'}`]);

  let html = '<div class="kv">' +
    rows.map(([k, v]) => `<div>${k}</div><div class="mono">${v}</div>`).join('') + '</div>';

  const domain = r.cluster_domain || 'cluster.local';
  html += `<div class="alert i" style="margin-top:16px">
    <strong>That host resolves only from inside the cluster.</strong>
    <span class="mono">${domain}</span> is the cluster's internal DNS domain, served by CoreDNS.
    It is unrelated to the cluster's external name (the <span class="mono">api.</span> and
    <span class="mono">apps.</span> addresses) and is the same on almost every Kubernetes cluster.
    Use this host in your application's config; it will not resolve from your laptop
    or the installer node.</div>`;

  html += `<h3 style="font-size:13px;margin:20px 0 8px">From inside the cluster &mdash; what the app uses</h3>
    <pre style="max-height:none">oc -n &lt;their-namespace&gt; create secret generic redis-creds \\
  --from-literal=REDIS_HOST=${r.host} \\
  --from-literal=REDIS_PORT=${r.port} \\
  --from-literal=REDIS_PASSWORD='${r.password || ''}'</pre>
    <div class="hint">Then <span class="mono">envFrom: [{secretRef: {name: redis-creds}}]</span> in their Deployment.</div>`;

  html += `<h3 style="font-size:13px;margin:20px 0 8px">From outside the cluster</h3>`;
  if (r.node_port) {
    const ip = (r.node_ips && r.node_ips[0]) || '<node-ip>';
    html += `<pre style="max-height:none">redis-cli -h ${ip} -p ${r.node_port} -a '${r.password || ''}' --no-auth-warning PING</pre>
      <div class="alert w">NodePort <span class="mono">${r.node_port}</span> is open on
      <strong>every</strong> node${r.node_ips ? ' (' + r.node_ips.join(', ') + ')' : ''}, not just the one running the pod.
      The password and the NetworkPolicy are your only protection.</div>`;
  } else {
    html += `<pre style="max-height:none">oc port-forward -n ${r.namespace} svc/${r.name || r.database} 16379:${r.port}
redis-cli -h 127.0.0.1 -p 16379 -a '${r.password || ''}' --no-auth-warning PING</pre>
      <div class="hint">This is a ClusterIP Service, so a tunnel through the API server is the
      only way in. Fine for debugging; never point an application at it.</div>`;
  }

  if (r.restricted_to && r.restricted_to.length) {
    html += `<div class="alert w" style="margin-top:14px">A NetworkPolicy restricts access to
      <span class="mono">${r.restricted_to.join(', ')}</span>. Pods in any other namespace &mdash;
      including a debug pod you start elsewhere &mdash; will time out. Test from an allowed
      namespace, not from a scratch one.</div>`;
  }

  if (r.notes && r.notes.length) {
    html += '<div class="alert w" style="margin-top:14px"><strong>Tell the app team:</strong><ul style="margin:6px 0 0;padding-left:18px">' +
      r.notes.map(n => `<li>${n}</li>`).join('') + '</ul></div>';
  }

  $('resultBody').innerHTML = html;
  $('panelResult').classList.remove('hide');
}

/* ------------------------------------------------ status */

let SCUR = null;

document.querySelector('.tab[data-tab="status"]').addEventListener('click', scanForStatus);
$('sRefresh').onclick = scanForStatus;
$('sManualToggle').onclick = () => $('sManual').classList.toggle('hide');
$('sCheck').onclick = () => {
  const ns = $('sNamespace').value.trim();
  if (!ns) return;
  loadStatus({ namespace: ns, name: '', kind: 'manual' });
};
$('sReload').onclick = () => SCUR && loadStatus(SCUR);

async function scanForStatus() {
  $('sScanState').innerHTML = '<span class="spin"></span> scanning...';
  try {
    const d = await api('/api/discover');
    RELEASES = d.releases;
    $('nsList').innerHTML = (d.namespaces || []).map(n => `<option value="${n}">`).join('');
    $('sScanState').textContent =
      `${RELEASES.length} release(s) · scanned ${new Date().toLocaleTimeString()}`;
    $('sTable').innerHTML = RELEASES.length
      ? '<tr><th></th><th>Type</th><th>Namespace / name</th><th>Version</th><th>Status</th><th>Pods</th></tr>' +
        RELEASES.map((r, i) => `<tr>
          <td><input type="radio" name="srel" value="${i}" style="width:auto"></td>
          <td>${r.kind}${r.cr_kind ? ' <span class="dim">/ ' + r.cr_kind + '</span>'
            : (r.topology && r.topology !== 'standalone' ? ' <span class="dim">/ ' + r.topology + '</span>' : '')}</td>
          <td class="mono">${r.namespace}/<strong>${r.name}</strong></td>
          <td class="mono" style="font-size:11px">${r.version || '-'}</td>
          <td class="${r.deleting ? 'err' : 'ok'}">${r.deleting ? 'Terminating' : r.status}</td>
          <td>${r.pods}</td>
        </tr>`).join('')
      : '<tr><td class="dim">Nothing deployed. Use "Check another namespace" to look anyway.</td></tr>';
    document.querySelectorAll('input[name=srel]').forEach(rb => {
      rb.onchange = () => loadStatus(RELEASES[parseInt(rb.value)]);
    });
  } catch (e) { $('sScanState').innerHTML = `<span class="err">${e.message}</span>`; }
}

async function loadStatus(r) {
  SCUR = r;
  $('sTitle').textContent = r.name ? `${r.namespace}/${r.name}` : r.namespace;
  $('sDetailCard').classList.remove('hide');
  // only a named release can be handed over; a bare namespace has nothing to describe
  $('sHandoverCard').classList.toggle('hide', !r.name);
  $('hOut').classList.add('hide');
  $('hWarn').innerHTML = '';
  HDOC = '';
  $('sSpin').classList.remove('hide');
  $('sBody').innerHTML = '';
  $('sLive').innerHTML = '';
  try {
    const d = await api(`/api/status?namespace=${encodeURIComponent(r.namespace)}`
      + `&name=${encodeURIComponent(r.name || '')}`);
    renderStatus(d);
  } catch (e) {
    $('sBody').innerHTML = `<div class="alert e">${e.message}</div>`;
  } finally {
    $('sSpin').classList.add('hide');
  }
}

function tbl(title, head, rows) {
  if (!rows.length) return '';
  return `<h3 style="font-size:13px;margin:20px 0 8px">${title}</h3>
    <table><tr>${head.map(h => `<th>${h}</th>`).join('')}</tr>${rows.join('')}</table>`;
}

function renderStatus(d) {
  const L = d.live || {};
  if (L.pod) {
    const hit = parseInt(L.keyspace_hits || 0), miss = parseInt(L.keyspace_misses || 0);
    const ratio = (hit + miss) ? Math.round(hit / (hit + miss) * 100) : null;
    // redis_mode is standalone | sentinel | cluster -- it reports the PROTOCOL,
    // not whether replication is configured. A fully replicated primary still
    // says "standalone", which reads as a contradiction next to "2 replicas
    // attached". Derive the real topology instead and only surface redis_mode
    // when it actually carries information.
    const slaves = parseInt(L.connected_slaves || 0);
    let topo, topoNote = '';
    if (L.mode === 'cluster') {
      topo = 'Redis Cluster — data sharded by hash slot';
    } else if (L.mode === 'sentinel') {
      topo = 'Sentinel — monitoring, holds no data';
    } else if (L.role === 'master' && slaves > 0) {
      topo = `Replication — 1 primary + ${slaves} replica(s)`;
      topoNote = `Redis reports <span class="mono">redis_mode: standalone</span> here, which only
        means it is not running the Cluster or Sentinel protocol. Replication is a feature of a
        standalone server, so that is expected.`;
    } else if (L.role === 'slave') {
      topo = 'Replica — read-only, follows a primary';
    } else {
      topo = 'Standalone — a single server, no replicas attached';
      if (L.role === 'master') {
        topoNote = 'No replicas are connected. If you deployed a replication topology, '
                 + 'that is a fault — check the replication test.';
      }
    }

    const kv = [
      ['Redis version', L.version],
      ['Topology', topo],
      ['Role', L.role + (slaves > 0 ? ` — ${slaves} replica(s) attached` : '')],
      ['Memory', `${L.used_memory} used of ${L.maxmemory} (${L.maxmemory_policy})`],
      ['Keys', L.keys || '(empty)'],
      ['Clients', L.connected_clients],
      ['Evicted keys', L.evicted_keys + (parseInt(L.evicted_keys || 0) > 0
        ? ' — the cache is full and dropping data' : '')],
      ['Hit ratio', ratio === null ? 'no reads yet' : `${ratio}%  (${hit} hits / ${miss} misses)`],
      ['Persistence', `AOF ${L.aof_enabled === '1' ? 'on' : 'off'}, last RDB save ${L.rdb_last_bgsave_status}`],
    ];
    $('sLive').innerHTML =
      `<div class="alert i" style="margin-bottom:4px">Live <span class="mono">INFO</span> from
        <span class="mono">${L.pod}</span></div>
       <div class="kv">${kv.map(([k, v]) => `<div>${k}</div><div class="mono">${v}</div>`).join('')}</div>` +
      (topoNote ? `<div class="hint" style="margin-top:10px">${topoNote}</div>` : '');
  } else if (d.pods.length) {
    $('sLive').innerHTML = '<div class="alert w">Could not read live INFO — no password found, or no pod ready.</div>';
  }

  let h = '';
  h += tbl('Workloads', ['Kind', 'Name', 'Ready', 'Image'], d.workloads.map(w =>
    `<tr><td>${w.kind}</td><td class="mono">${w.name}</td>
     <td class="${w.ready.split('/')[0] === w.ready.split('/')[1] ? 'ok' : 'warn'}">${w.ready}</td>
     <td class="mono" style="font-size:11px">${w.images.join(', ')}</td></tr>`));

  h += tbl('Pods', ['Name', 'Ready', 'Phase', 'Restarts', 'Node', 'IP', 'SCC', 'QoS'],
    d.pods.map(p => `<tr>
      <td class="mono">${p.name}</td>
      <td class="${p.healthy ? 'ok' : 'warn'}">${p.ready}</td>
      <td class="${p.phase === 'Running' ? 'ok' : 'warn'}">${p.phase}</td>
      <td class="${p.restarts > 0 ? 'warn' : 'dim'}">${p.restarts}</td>
      <td class="mono" style="font-size:11px">${p.node || '-'}</td>
      <td class="mono" style="font-size:11px">${p.ip || '-'}</td>
      <td style="font-size:11px">${p.scc || '-'}</td>
      <td class="${p.qos === 'Guaranteed' ? 'ok' : 'dim'}">${p.qos || '-'}</td></tr>`));

  h += tbl('Services', ['Name', 'Type', 'Cluster IP', 'Ports', 'Selects'],
    d.services.map(s => `<tr>
      <td class="mono">${s.name}</td><td>${s.type}</td>
      <td class="mono" style="font-size:11px">${s.cluster_ip === 'None' ? 'None (headless)' : s.cluster_ip}</td>
      <td class="mono">${s.ports}</td>
      <td class="dim" style="font-size:11px">${s.selector || '-'}</td></tr>`));

  h += tbl('Storage', ['PVC', 'Status', 'Capacity', 'StorageClass'],
    d.pvcs.map(p => `<tr>
      <td class="mono">${p.name}</td>
      <td class="${p.phase === 'Bound' ? 'ok' : 'err'}">${p.phase}</td>
      <td>${p.capacity || '-'}</td>
      <td class="mono ${/nfs|cephfs|isilon/i.test(p.storage_class) ? 'err' : ''}">${p.storage_class}
        ${/nfs|cephfs|isilon/i.test(p.storage_class) ? ' ⚠ file storage' : ''}</td></tr>`));

  h += tbl('NetworkPolicies', ['Name', 'Applies to', 'Allows ingress from'],
    d.policies.map(p => `<tr>
      <td class="mono">${p.name}</td><td class="mono">${p.pod_selector}</td>
      <td class="mono">${p.allows.join(', ') || '<span class="err">nothing — all ingress denied</span>'}</td></tr>`));

  const crs = Object.entries(d.custom_resources || {});
  if (crs.length) {
    h += '<h3 style="font-size:13px;margin:20px 0 8px">Custom resources</h3>';
    h += crs.map(([k, v]) => `<pre style="max-height:150px;margin-bottom:8px">${k}\n${v}</pre>`).join('');
  }

  const warn = d.events.filter(e => e.type === 'Warning');
  h += tbl(`Recent events${warn.length ? ` (${warn.length} warning${warn.length > 1 ? 's' : ''})` : ''}`,
    ['Type', 'Reason', 'Object', 'Message'],
    d.events.slice().reverse().map(e => `<tr>
      <td class="${e.type === 'Warning' ? 'warn' : 'dim'}">${e.type}</td>
      <td>${e.reason}</td>
      <td class="mono" style="font-size:11px">${e.object}</td>
      <td style="font-size:11px">${e.message}</td></tr>`));

  $('sBody').innerHTML = h || '<div class="dim">Nothing found in this namespace.</div>';
}

/* ------------------------------------------------ uninstall */

let RELEASES = [], SELECTED = null, TERMINATING = [];

document.querySelector('.tab[data-tab="uninstall"]').addEventListener('click', scanReleases);
$('uRefresh').onclick = scanReleases;

async function scanReleases() {
  $('uScanState').innerHTML = '<span class="spin"></span> scanning cluster...';
  $('uTable').innerHTML = '';
  try {
    const d = await api('/api/discover');
    RELEASES = d.releases;
    $('nsList').innerHTML = d.namespaces.map(n => `<option value="${n}">`).join('');
    TERMINATING = d.terminating_namespaces || [];
    loadLeftovers();
    renderReleases();
    const t = new Date().toLocaleTimeString();
    $('uScanState').innerHTML = RELEASES.length
      ? `${RELEASES.length} release(s) &middot; scanned ${t}`
      : `<span class="ok">nothing installed</span> &middot; scanned ${t}`;

    // if the thing we had selected is gone, drop the detail panel
    if (SELECTED && !SELECTED.manual &&
        !RELEASES.some(r => r.namespace === SELECTED.namespace && r.name === SELECTED.name)) {
      SELECTED = null;
      $('uDetailCard').classList.add('hide');
    }
  } catch (e) {
    $('uScanState').innerHTML = `<span class="err">${e.message}</span>`;
  }
}

function renderReleases() {
  if (!RELEASES.length) {
    $('uTable').innerHTML =
      '<tr><td class="dim">No Redis installation found. Use "Enter manually" if you know it is there.</td></tr>';
    return;
  }
  if (TERMINATING.length) {
    $('uTable').innerHTML =
      `<tr><td colspan="7"><div class="alert w" style="margin:0">Namespace(s)
       <span class="mono">${TERMINATING.join(', ')}</span> are still <strong>Terminating</strong>.
       Objects inside them are on their way out and may still be listed below.
       If this persists for more than a few minutes, a finalizer is stuck:
       <span class="mono">oc get ns ${TERMINATING[0]} -o jsonpath='{.spec.finalizers}{.status.conditions}'</span>
       </div></td></tr>`;
  } else { $('uTable').innerHTML = ''; }
  $('uTable').innerHTML +=
    '<tr><th></th><th>Type</th><th>Namespace / name</th><th>Version</th><th>Status</th><th>Detail</th><th>PVCs</th></tr>' +
    RELEASES.map((r, i) => {
      const pvcTotal = r.pvcs.length;
      const risky = r.pvcs.filter(p => p.reclaim === 'Delete').length;
      return `<tr>
        <td><input type="radio" name="rel" value="${i}" style="width:auto"></td>
        <td>${r.kind === 'enterprise' ? 'Enterprise' : 'Community'}${r.managed ? '' : ' <span class="dim" title="not deployed by this app">*</span>'}</td>
        <td class="mono">${r.namespace}/<strong>${r.name}</strong></td>
        <td class="mono" style="font-size:11px">${r.version || '-'}</td>
        <td class="${r.deleting ? 'err' : (/running|ready/i.test(r.status) && !/0\//.test(r.status) ? 'ok' : 'warn')}">${r.deleting ? 'Terminating' : r.status}</td>
        <td style="font-size:11.5px" class="dim">${r.detail || ''}</td>
        <td>${pvcTotal}${risky ? ` <span class="err" title="reclaimPolicy=Delete">(${risky} destructive)</span>` : ''}</td>
      </tr>`;
    }).join('') +
    '<tr><td colspan="7" class="dim" style="font-size:11px;border:0;padding-top:10px">* not deployed by this app &mdash; only objects that can be named exactly will be removed.</td></tr>';

  document.querySelectorAll('input[name=rel]').forEach(r => {
    r.onchange = () => selectRelease(RELEASES[parseInt(r.value)]);
  });
}

$('uManualToggle').onclick = () => $('uManual').classList.toggle('hide');
$('mUse').onclick = () => {
  const ns = $('mNamespace').value.trim(), name = $('mName').value.trim();
  if (!ns || !name) return alert('Namespace and release name are both required.');
  selectRelease({
    kind: $('mKind').value, namespace: ns, name, version: '', status: 'not verified',
    detail: 'entered manually', databases: [], pvcs: [], pods: 0, managed: false,
    workload: 'deployment', manual: true,
  });
};

function selectRelease(r) {
  SELECTED = r;
  $('uTitle').textContent = `${r.namespace}/${r.name}`;

  const rows = [
    ['Type', r.kind === 'enterprise' ? 'Redis Enterprise' : 'Redis Community'],
    ['Version', r.version || '(unknown)'],
    ['Status', r.status],
    ['Pods', r.pods],
  ];
  if (r.operator_csv) rows.push(['Operator', r.operator_csv]);
  if (r.licence_expiry) rows.push(['Licence expires', r.licence_expiry + (r.licence_trial ? '  (trial)' : '')]);
  if (r.databases && r.databases.length)
    rows.push(['Databases', r.databases.map(d => `${d.name} (${d.status}, port ${d.port}, ${d.memory})`).join('<br>')]);

  $('uSummary').innerHTML = '<div class="kv">' +
    rows.map(([k, v]) => `<div>${k}</div><div class="mono">${v}</div>`).join('') + '</div>';

  renderPlan();
  $('uPvc').checked = false;
  $('uNs').checked = false;
  $('uDetailCard').classList.remove('hide');
  $('uDetailCard').scrollIntoView({ behavior: 'smooth' });
}

function renderPlan() {
  const r = SELECTED;
  if (!r) return;
  const pvc = $('uPvc').checked, ns = $('uNs').checked;

  let steps;
  if (r.kind === 'opstree') {
    steps = [
      [r.cr_kind || 'Custom resource', `${r.name} — the operator then removes the StatefulSet, Services and PDB it created`, true],
      ['Secret / ConfigMap / NetworkPolicy', "objects carrying this app's label", true],
    ];
  } else if (r.kind === 'enterprise') {
    steps = [
      ['RedisEnterpriseDatabase', r.databases.length
        ? r.databases.map(d => d.name).join(', ') : 'all in the namespace', true],
      ['RedisEnterpriseCluster', r.name + ' (3 pods, the Redis Enterprise platform)', true],
      ['Operator', 'Subscription, ClusterServiceVersion, OperatorGroup', true],
    ];
  } else {
    steps = r.managed
      ? [['Workload objects', 'Deployment, Service, ConfigMap, Secret, NetworkPolicy carrying this app\'s label', true]]
      : [[r.workload || 'Deployment', r.name, true],
         ['Service', r.name, true],
         ['ConfigMap', `${r.name}-config`, true],
         ['Secret', `${r.name}-auth`, true]];
  }

  let html = '<table>' + steps.map(([k, v]) =>
    `<tr><td style="width:190px">${k}</td><td class="mono" style="font-size:11.5px">${v}</td>
     <td class="ok" style="width:70px">delete</td></tr>`).join('');

  if (r.pvcs.length) {
    html += r.pvcs.map(p =>
      `<tr><td>PersistentVolumeClaim</td>
       <td class="mono" style="font-size:11.5px">${p.name} &middot; ${p.size} &middot; ${p.storage_class}
         &middot; reclaim=<strong class="${p.reclaim === 'Delete' ? 'err' : 'ok'}">${p.reclaim}</strong></td>
       <td class="${pvc ? 'err' : 'dim'}" style="width:70px">${pvc ? 'DELETE' : 'keep'}</td></tr>`).join('');
  }
  html += `<tr><td>Namespace</td><td class="mono" style="font-size:11.5px">${r.namespace}</td>
    <td class="${ns ? 'err' : 'dim'}" style="width:70px">${ns ? 'DELETE' : 'keep'}</td></tr></table>`;
  $('uPlan').innerHTML = html;

  const destructive = r.pvcs.filter(p => p.reclaim === 'Delete');
  let danger = '';
  if (pvc && destructive.length) {
    const total = destructive.reduce((a, p) => a + (parseInt(p.size) || 0), 0);
    danger = `<div class="alert e"><strong>This destroys data permanently.</strong>
      ${destructive.length} PVC(s) totalling ~${total}Gi sit on a StorageClass with
      <span class="mono">reclaimPolicy: Delete</span> &mdash; the underlying disks are removed
      immediately and cannot be recovered.</div>`;
  } else if (pvc) {
    danger = `<div class="alert w">PVCs will be deleted, but their StorageClass uses
      <span class="mono">reclaimPolicy: Retain</span>, so the volumes survive as
      <span class="mono">Released</span> PVs and will need manual cleanup.</div>`;
  }
  if (ns) {
    danger += `<div class="alert e"><strong>Deleting the namespace removes everything in it</strong>,
      including objects this app did not create.</div>`;
  }
  $('uDanger').innerHTML = danger;
}

$('uPvc').onchange = renderPlan;
$('uNs').onchange = renderPlan;
$('uCancel').onclick = () => { $('uDetailCard').classList.add('hide'); SELECTED = null; };

$('uGo').onclick = async () => {
  const r = SELECTED;
  if (!r) return;
  const pvc = $('uPvc').checked, ns = $('uNs').checked;

  let msg = `Uninstall ${r.kind} release '${r.name}' from namespace '${r.namespace}'?`;
  const destructive = r.pvcs.filter(p => p.reclaim === 'Delete');
  if (pvc && destructive.length)
    msg += `\n\n${destructive.length} PVC(s) on reclaimPolicy=Delete WILL BE DESTROYED PERMANENTLY.`;
  if (ns) msg += `\n\nTHE ENTIRE NAMESPACE '${r.namespace}' WILL BE DELETED.`;
  if (!confirm(msg)) return;
  if ((pvc && destructive.length) || ns) {
    const typed = prompt(`Type the namespace name to confirm: ${r.namespace}`);
    if (typed !== r.namespace) return alert('Cancelled - name did not match.');
  }

  const j = await api('/api/uninstall', {
    method: 'POST',
    body: JSON.stringify({
      kind: r.kind, namespace: r.namespace, name: r.name,
      cr_plural: r.cr_plural || null,
      workload: r.workload || 'deployment', managed: !!r.managed,
      delete_pvc: pvc, delete_namespace: ns,
    }),
  });
  // the selection is about to stop existing -- drop it now, not later
  SELECTED = null;
  $('uDetailCard').classList.add('hide');
  $('uTable').innerHTML = '';
  $('uScanState').innerHTML = '<span class="spin"></span> uninstalling...';

  document.querySelector('.tab[data-tab="deploy"]').click();
  streamJob(j.job_id, () => { scanReleases(); loadInstalledOperators(); });
};


/* ------------------------------------------------ operators */

let OPDETAIL = null, opTimer = null;

document.querySelector('.tab[data-tab="operators"]').addEventListener('click', () => {
  if (!$('opTable').innerHTML) { searchOperators(); loadInstalledOperators(); }
});

$('opQ').addEventListener('input', () => {
  clearTimeout(opTimer);
  opTimer = setTimeout(searchOperators, 250);
});
['opCatalog', 'opCategory', 'opCertified'].forEach(id => $(id).onchange = searchOperators);
$('opRefresh').onclick = () => searchOperators(true);
$('opInstalledRefresh').onclick = loadInstalledOperators;
$('opClose').onclick = () => $('opDetailCard').classList.add('hide');

async function searchOperators(refresh = false) {
  $('opCount').innerHTML = '<span class="spin"></span>';
  const p = new URLSearchParams({
    q: $('opQ').value.trim(), catalog: $('opCatalog').value,
    category: $('opCategory').value, certified: $('opCertified').checked,
    refresh: refresh === true,
  });
  try {
    const d = await api('/api/operators?' + p);
    $('opIndexed').textContent =
      `${d.indexed} packages indexed from ${d.catalogs.length} catalog sources.`;
    if ($('opCatalog').options.length <= 1)
      $('opCatalog').innerHTML = '<option value="">All catalogs</option>' +
        d.catalogs.map(c => `<option value="${c.name}">${c.name} (${c.count})</option>`).join('');
    if ($('opCategory').options.length <= 1)
      $('opCategory').innerHTML = '<option value="">Any category</option>' +
        d.categories.map(c => `<option value="${c.name}">${c.name} (${c.count})</option>`).join('');
    $('opCount').textContent = `${d.shown} of ${d.total}`;
    renderOperators(d.results);
  } catch (e) { $('opCount').innerHTML = `<span class="err">${e.message}</span>`; }
}

function renderOperators(rows) {
  if (!rows.length) { $('opTable').innerHTML = '<tr><td class="dim">No match.</td></tr>'; return; }
  $('opTable').innerHTML =
    '<tr><th>Package</th><th>Provider</th><th>Catalog</th><th>Version</th><th>Provides</th><th></th></tr>' +
    rows.map(r => `<tr>
      <td><span class="mono">${r.name}</span>${r.certified ? ' <span class="ok" title="certified">&#10003;</span>' : ''}
        <div class="dim" style="font-size:11px">${r.display_name || ''}</div></td>
      <td style="font-size:12px">${r.provider || '-'}</td>
      <td style="font-size:11.5px" class="dim">${r.catalog}</td>
      <td class="mono" style="font-size:11px">${r.version || '-'}</td>
      <td class="mono" style="font-size:11px">${(r.api_kinds || []).join(', ') || '-'}</td>
      <td><button class="ghost" style="padding:4px 10px" onclick="openOperator('${r.name}')">Details</button></td>
    </tr>`).join('');
}

window.openOperator = async (name) => {
  const d = await api('/api/operators/detail?name=' + encodeURIComponent(name));
  OPDETAIL = d;
  $('opTitle').textContent = d.display_name || d.name;
  $('opSubtitle').innerHTML =
    `<span class="mono">${d.name}</span> &middot; ${d.provider || 'unknown provider'} &middot; ${d.catalog_display || d.catalog}`;

  const meta = [];
  if (d.version) meta.push(['Latest version', d.version]);
  if (d.capabilities) meta.push(['Capability level', d.capabilities]);
  if (d.support) meta.push(['Support', d.support]);
  meta.push(['Certified', d.certified ? 'yes' : 'no']);
  if (d.categories.length) meta.push(['Categories', d.categories.join(', ')]);
  if (d.infrastructure_features.length) meta.push(['Infrastructure', d.infrastructure_features.join(', ')]);
  if (d.repository) meta.push(['Repository', `<a href="${d.repository}" target="_blank" rel="noopener">${d.repository}</a>`]);
  $('opMeta').innerHTML = '<div class="kv">' +
    meta.map(([k, v]) => `<div>${k}</div><div>${v}</div>`).join('') + '</div>';
  $('opDesc').textContent = d.long_description || d.description || '';

  $('opApis').innerHTML = d.provided_apis.length
    ? '<tr><th>Kind</th><th>API</th><th>Description</th></tr>' + d.provided_apis.map(a =>
        `<tr><td class="mono">${a.kind}</td><td class="mono" style="font-size:11px">${a.name}</td>
         <td class="dim" style="font-size:11.5px">${a.description || ''}</td></tr>`).join('')
    : '<tr><td class="dim">This operator declares no custom resources.</td></tr>';

  $('opChannel').innerHTML = d.channels.map(c =>
    `<option value="${c.name}"${c.name === d.default_channel ? ' selected' : ''}>${c.name}${c.version ? ' - ' + c.version : ''}</option>`).join('');
  $('opChannel').onchange = () => {
    const c = d.channels.find(x => x.name === $('opChannel').value);
    $('opCsv').value = c ? c.csv : '';
  };
  $('opChannel').onchange();

  const modes = d.install_modes.filter(m => m.supported).map(m => m.type);
  $('opMode').innerHTML = modes.map(m => `<option value="${m}">${m}</option>`).join('')
    || '<option value="OwnNamespace">OwnNamespace</option>';
  $('opMode').value = modes.includes('AllNamespaces') ? 'AllNamespaces' : modes[0];
  $('opMode').onchange = applyOpMode;
  applyOpMode();

  const installed = (window.OPINSTALLED || []).find(i => i.package === d.name);
  $('opInstalledNote').innerHTML = installed
    ? `<div class="alert w">Already installed in <span class="mono">${installed.namespace}</span>
       (channel ${installed.channel}, ${installed.phase || 'unknown'}, ${installed.approval} approval).
       Installing again into the same namespace will do nothing.</div>` : '';

  const isRedis = /redis|valkey|redkey/i.test(d.name + ' ' + (d.display_name || ''));
  $('opUseDeploy').classList.toggle('hide', !isRedis);

  $('opDetailCard').classList.remove('hide');
  $('opDetailCard').scrollIntoView({ behavior: 'smooth' });
};

function applyOpMode() {
  const mode = $('opMode').value;
  const all = mode === 'AllNamespaces';
  $('opNamespace').value = all ? 'openshift-operators'
    : (OPDETAIL ? OPDETAIL.name.replace(/-operator(-cert)?$/, '') || OPDETAIL.name : '');
  $('opNamespace').readOnly = all;
  $('wrapOpTarget').classList.toggle('hide', mode !== 'SingleNamespace');
  $('opModeHint').textContent = all
    ? 'Installs into openshift-operators, which already has a cluster-wide OperatorGroup. None is created.'
    : (mode === 'OwnNamespace'
        ? 'Watches only its own namespace. A namespace and OperatorGroup are created.'
        : 'Watches one other namespace, named below.');
}

function opSpec() {
  return {
    package: OPDETAIL.name,
    catalog_source: OPDETAIL.catalog,
    channel: $('opChannel').value,
    starting_csv: $('opCsv').value || null,
    install_mode: $('opMode').value,
    namespace: $('opNamespace').value.trim(),
    target_namespace: $('opTarget').value.trim() || null,
    approval: $('opApproval').value,
  };
}

$('opPreview').onclick = async () => showPreview(
  await api('/api/preview/operator', { method: 'POST', body: JSON.stringify(opSpec()) }));

$('opInstall').onclick = async () => {
  const s = opSpec();
  if (!confirm(`Install operator '${s.package}' into namespace '${s.namespace}'?\n\n` +
               `This installs the operator only. No custom resources are created, ` +
               `so nothing will be running yet.`)) return;
  const j = await api('/api/operators/install', { method: 'POST', body: JSON.stringify(s) });
  document.querySelector('.tab[data-tab="deploy"]').click();
  streamJob(j.job_id, loadInstalledOperators);
};

$('opUseDeploy').onclick = () => {
  document.querySelector('.tab[data-tab="deploy"]').click();
  $('optEnterprise').click();
  setTimeout(() => {
    const sel = $('ePackage');
    if ([...sel.options].some(o => o.value === OPDETAIL.name)) {
      sel.value = OPDETAIL.name; sel.onchange();
    }
    $('formEnterprise').scrollIntoView({ behavior: 'smooth' });
  }, 400);
};

async function loadInstalledOperators() {
  try {
    const d = await api('/api/operators/installed');
    window.OPINSTALLED = d.installed;
    $('opInstalled').innerHTML = d.installed.length
      ? '<tr><th>Package</th><th>Namespace</th><th>Channel</th><th>Version</th><th>Approval</th><th>Phase</th></tr>' +
        d.installed.map(i => `<tr>
          <td class="mono">${i.package}</td><td class="mono">${i.namespace}</td>
          <td>${i.channel || '-'}</td><td class="mono" style="font-size:11px">${i.version || '-'}</td>
          <td class="${i.approval === 'Manual' ? 'ok' : 'warn'}">${i.approval}</td>
          <td class="${i.phase === 'Succeeded' ? 'ok' : 'warn'}">${i.phase || '-'}</td></tr>`).join('')
      : '<tr><td class="dim">No operator subscriptions found.</td></tr>';
  } catch (e) { $('opInstalled').innerHTML = `<tr><td class="err">${e.message}</td></tr>`; }
}


/* ------------------------------------------------ leftovers */

let LEFTOVERS = null;

async function loadLeftovers() {
  try {
    const d = await api('/api/leftovers');
    LEFTOVERS = d;
    let html = '';

    if (d.crds_orphaned.length) {
      html += `<div class="alert w">
        <strong>${d.crds_orphaned.length} leftover CustomResourceDefinition(s).</strong>
        CRDs are cluster-scoped, so removing a namespace or an operator does not delete them.
        None of these has any object left, and no Redis operator is installed, so they are safe
        to remove &mdash; but they are shared cluster-wide, so leaving them costs nothing either.
        <div style="margin-top:8px;max-height:130px;overflow:auto" class="mono" style="font-size:11px">
          ${d.crds_orphaned.map(c => c.name).join('<br>')}
        </div>
        <div class="row" style="margin-top:10px">
          <button class="danger" id="uCleanCrds" style="padding:6px 12px">Remove these CRDs</button>
        </div></div>`;
    } else if (d.crds.length && d.operator_still_installed) {
      html += `<div class="alert i">${d.crds.length} Redis CRD(s) present and an operator is
        still subscribed &mdash; leave them alone.</div>`;
    }

    if (d.released_pvs.length) {
      const total = d.released_pvs.reduce((a, p) => a + (parseInt(p.size) || 0), 0);
      html += `<div class="alert w"><strong>${d.released_pvs.length} PersistentVolume(s) in
        Released state (~${total}Gi).</strong> Their PVC is gone but reclaimPolicy is Retain,
        so the storage is still allocated and needs manual cleanup.
        <div style="margin-top:8px" class="mono" style="font-size:11px">
        ${d.released_pvs.map(p => `${p.name} &middot; ${p.size} &middot; was ${p.claim}`).join('<br>')}
        </div></div>`;
    }

    $('uLeftovers').innerHTML = html;
    if ($('uCleanCrds')) $('uCleanCrds').onclick = cleanupCrds;
  } catch (e) { $('uLeftovers').innerHTML = ''; }
}

async function cleanupCrds() {
  const names = LEFTOVERS.crds_orphaned.map(c => c.name);
  if (!confirm(`Delete ${names.length} CustomResourceDefinition(s)?\n\n` +
    `CRDs are CLUSTER-SCOPED. This removes these object types from the whole cluster.\n\n` +
    `Each is re-checked for live objects before deletion and skipped if any exist.`)) return;
  const j = await api('/api/leftovers/cleanup', { method: 'POST', body: JSON.stringify(names) });
  document.querySelector('.tab[data-tab="deploy"]').click();
  streamJob(j.job_id, () => { scanReleases(); });
}


/* ------------------------------------------------ namespace picker */

let NAMESPACES = [];

async function loadNamespaces() {
  try {
    const d = await api('/api/namespaces');
    NAMESPACES = d.namespaces;
  } catch { NAMESPACES = []; }
  ['cAllowNs', 'eAllowNs'].forEach(buildNsPicker);
}

function buildNsPicker(id) {
  const host = $(id);
  if (!host) return;
  const userCount = NAMESPACES.filter(n => !n.system).length;
  host.innerHTML = `
    <div class="nsbar">
      <input type="text" placeholder="filter ${NAMESPACES.length} namespaces...">
      <label><input type="checkbox"> show system (${NAMESPACES.length - userCount})</label>
    </div>
    <div class="nslist"></div>
    <div class="nschips"><span class="nsempty">none selected &mdash; no NetworkPolicy will be created</span></div>`;
  host._sel = new Set();
  const [filter, sysBox] = [host.querySelector('input[type=text]'),
                            host.querySelector('input[type=checkbox]')];
  const redraw = () => drawNsList(host, filter.value, sysBox.checked);
  filter.oninput = redraw;
  sysBox.onchange = redraw;
  redraw();
}

function drawNsList(host, q, showSystem) {
  q = (q || '').toLowerCase();
  const rows = NAMESPACES.filter(n =>
    (showSystem || !n.system) && (!q || n.name.includes(q)));
  const list = host.querySelector('.nslist');
  list.innerHTML = rows.length
    ? rows.map(n => `<label class="nsrow">
        <input type="checkbox" value="${n.name}" ${host._sel.has(n.name) ? 'checked' : ''}>
        <span class="mono">${n.name}</span>
        ${n.system ? '<span class="sys">system</span>' : ''}
        ${n.phase !== 'Active' ? `<span class="sys err">${n.phase}</span>` : ''}
      </label>`).join('')
    : '<div class="nsempty" style="padding:8px 10px">no match</div>';
  list.querySelectorAll('input').forEach(cb => {
    cb.onchange = () => {
      cb.checked ? host._sel.add(cb.value) : host._sel.delete(cb.value);
      drawNsChips(host);
    };
  });
  drawNsChips(host);
}

function drawNsChips(host) {
  const box = host.querySelector('.nschips');
  const sel = [...host._sel].sort();
  box.innerHTML = sel.length
    ? sel.map(n => `<span class="nschip">${n}<b data-ns="${n}">&times;</b></span>`).join('')
    : '<span class="nsempty">none selected &mdash; no NetworkPolicy will be created</span>';
  box.querySelectorAll('b').forEach(x => {
    x.onclick = () => {
      host._sel.delete(x.dataset.ns);
      const cb = host.querySelector(`.nslist input[value="${x.dataset.ns}"]`);
      if (cb) cb.checked = false;
      drawNsChips(host);
    };
  });
}

function nsPicked(id) {
  const host = $(id);
  return host && host._sel ? [...host._sel] : [];
}


/* ------------------------------------------------ community topology */

function applyCTopology() {
  const rep = $('cTopology').value === 'replication';
  $('wrapCReplicas').classList.toggle('hide', !rep);
  $('cTopoHint').innerHTML = rep
    ? `A StatefulSet: <span class="mono">&lt;name&gt;-0</span> is the primary, the rest start with
       <span class="mono">replicaof</span>. Two Services are created &mdash; one for writes (primary only),
       one for reads (all pods).
       <span class="err">No automatic failover</span>: if the primary dies nothing promotes a replica.
       For real HA use Sentinel via the Opstree operator.`
    : 'One pod as a Deployment. Kubernetes restarts it if it dies (~30s, no data loss with AOF).';
}

/* ------------------------------------------------ opstree */

async function loadOpstree() {
  const d = await api('/api/versions/opstree');
  STATE.opstree = d;
  $('otTopology').innerHTML = d.topologies.map(t =>
    `<option value="${t.id}">${t.label}</option>`).join('');
  $('otVersion').innerHTML = d.versions.map(v =>
    `<option value="${v.id}">${v.label}</option>`).join('');
  $('otEviction').innerHTML = (STATE.community?.eviction_policies || []).map(e =>
    `<option value="${e.value}"${e.value === 'allkeys-lru' ? ' selected' : ''}>${e.label}</option>`).join('')
    || '<option value="allkeys-lru">allkeys-lru</option><option value="noeviction">noeviction</option>';
  ['otMaxmemory', 'otMaxmemoryUnit', 'otMemLim'].forEach(id => { const e=$(id); if(e){e.addEventListener('input',()=>checkMem('ot')); e.addEventListener('change',()=>checkMem('ot'));} });
  checkMem('ot');
  $('otTopology').onchange = applyOtTopology;
  $('otVersion').onchange = applyOtVersion;

  const missing = Object.entries(d.crds).filter(([, v]) => !v.present).map(([k]) => k);
  $('otWarn').innerHTML = missing.length === 4
    ? `<div class="alert i">The Opstree operator is not installed on this cluster.
       Leave "Install the operator if missing" ticked and it will be subscribed from
       <span class="mono">community-operators</span> before the custom resource is created.</div>`
    : (missing.length
        ? `<div class="alert w">Some CRDs are missing: <span class="mono">${missing.join(', ')}</span>.
           The operator may be an older version than expected.</div>`
        : `<div class="alert i">Operator already installed &mdash; all four CRDs are present.</div>`);

  buildNsPicker('otAllowNs');
  if ($('cStorageClass').innerHTML) $('otStorageClass').innerHTML = $('cStorageClass').innerHTML;
  $('otStorageClass').onchange = () => {
    const k = $('otStorageClass').selectedOptions[0].dataset.kind;
    $('otScHint').innerHTML =
      k === 'file' ? '<span class="err">NAS/file storage. Not suitable for a database.</span>' :
      k === 'block' ? '<span class="ok">Block storage - correct.</span>' : '';
  };
  applyOtTopology();
  applyOtVersion();
}

function otTopo() {
  return STATE.opstree.topologies.find(t => t.id === $('otTopology').value);
}

function applyOtTopology() {
  const t = otTopo();
  $('otTopoHint').innerHTML = `<strong>${t.kind}</strong> &mdash; ${t.detail}` +
    (t.client_aware ? `<br><span class="err">Client must be ${t.client_aware}.</span>` : '') +
    (t.ha ? '<br><span class="ok">Automatic failover.</span>'
          : '<br><span class="warn">No automatic failover.</span>');
  $('otSentinelFields').classList.toggle('hide', t.id !== 'sentinel');
  $('otSizeLabel').textContent = t.size_label || 'Size';
  $('otSize').disabled = !t.size_label;
  if (!t.size_label) $('otSize').value = 1;
  else if (parseInt($('otSize').value) < t.min_size) $('otSize').value = t.min_size;
  applyOtSize();
  $('otSize').oninput = applyOtSize;
  if (t.id === 'sentinel' && !$('otReplName').value)
    $('otReplName').value = $('otName').value + '-replication';
}

function applyOtSize() {
  const t = otTopo(), n = parseInt($('otSize').value) || t.min_size;
  const pods = t.id === 'cluster' ? n * 2 : (t.id === 'standalone' ? 1 : n);
  let msg = `${pods} pod(s) total`;
  if (t.id === 'cluster') msg += ` (${n} leaders + ${n} followers)`;
  if (t.id === 'sentinel' && n % 2 === 0)
    msg += ' — <span class="err">even count cannot break a tie; use an odd number</span>';
  if (n < t.min_size) msg += ` — <span class="err">minimum is ${t.min_size}</span>`;
  $('otSizeHint').innerHTML = msg;
}

function applyOtVersion() {
  const v = STATE.opstree.versions.find(x => x.id === $('otVersion').value);
  $('otVersionHint').innerHTML = `<span class="mono">${v.image}</span>`;
}

$('otGenPw').onclick = async (e) => {
  e.preventDefault();
  $('otPassword').value = (await api('/api/genpassword')).password;
};

function opstreeSpec() {
  return {
    namespace: $('otNamespace').value.trim(),
    name: $('otName').value.trim(),
    topology: $('otTopology').value,
    version_id: $('otVersion').value,
    size: parseInt($('otSize').value) || 3,
    password: $('otPassword').value || null,
    auth_enabled: true,
    replication_name: $('otReplName').value.trim() || null,
    master_group: $('otMasterGroup').value.trim() || 'myMaster',
    quorum: parseInt($('otQuorum').value) || 2,
    maxmemory: maxmemValue('otMaxmemory'),
    maxmemory_policy: $('otEviction').value,
    extra_config: $('otExtraConf').value.trim() || null,
    storage_class: $('otStorageClass').value || null,
    storage_size: $('otStorageSize').value.trim(),
    persistence: true,
    cpu_request: $('otCpuReq').value.trim(),
    cpu_limit: $('otCpuLim').value.trim(),
    memory_request: $('otMemReq').value.trim(),
    memory_limit: $('otMemLim').value.trim(),
    sentinel_auth_pass: $('otAuthPass').checked,
    install_operator: $('otInstallOp').checked,
    allow_namespaces: nsPicked('otAllowNs'),
    mirror: mirrorSettings(),
  };
}

$('otPreview').onclick = async () => showPreview(
  await api('/api/preview/opstree', { method: 'POST', body: JSON.stringify(opstreeSpec()) }));

$('otDeploy').onclick = async () => {
  const s = opstreeSpec(), t = otTopo();
  let msg = `Deploy ${t.label} into namespace '${s.namespace}'?`;
  if (t.client_aware) msg += `\n\nClients MUST be ${t.client_aware}. A plain redis-cli pointed at one host will not work correctly.`;
  if (s.topology === 'sentinel') msg += `\n\nSentinel monitors an existing RedisReplication named '${s.replication_name}'. It must already exist.`;
  if (!confirm(msg)) return;
  const j = await api('/api/deploy/opstree', { method: 'POST', body: JSON.stringify(s) });
  streamJob(j.job_id, () => { RELEASES = []; });
};


/* ------------------------------------------------ tests */

let TTARGET = null, TTESTS = [];

document.querySelector('.tab[data-tab="tests"]').addEventListener('click', scanForTests);
$('tRefresh').onclick = scanForTests;

async function scanForTests() {
  $('tScanState').innerHTML = '<span class="spin"></span> scanning...';
  try {
    const d = await api('/api/discover');
    RELEASES = d.releases;
    $('nsList').innerHTML = (d.namespaces || []).map(n => `<option value="${n}">`).join('');
    $('tScanState').textContent = `${RELEASES.length} release(s) · scanned ${new Date().toLocaleTimeString()}`;
    $('tTable').innerHTML = RELEASES.length
      ? '<tr><th></th><th>Type</th><th>Namespace / name</th><th>Version</th><th>Status</th></tr>' +
        RELEASES.map((r, i) => `<tr>
          <td><input type="radio" name="trel" value="${i}" style="width:auto"></td>
          <td>${r.kind}</td>
          <td class="mono">${r.namespace}/<strong>${r.name}</strong></td>
          <td class="mono" style="font-size:11px">${r.version || '-'}</td>
          <td class="${r.deleting ? 'err' : 'ok'}">${r.deleting ? 'Terminating' : r.status}</td>
        </tr>`).join('')
      : '<tr><td class="dim">Nothing deployed to test.</td></tr>';
    document.querySelectorAll('input[name=trel]').forEach(rb => {
      rb.onchange = () => pickTestTarget(RELEASES[parseInt(rb.value)]);
    });
  } catch (e) { $('tScanState').innerHTML = `<span class="err">${e.message}</span>`; }
}

async function pickTestTarget(r) {
  TTARGET = r;
  $('tTitle').textContent = `${r.namespace}/${r.name}`;

  // topology label comes back from discovery for community, otherwise infer
  const topo = r.topology || (r.kind === 'enterprise' ? 'enterprise' : 'standalone');
  TTARGET.topology = topo;

  const rows = [['Type', r.kind], ['Topology', topo], ['Status', r.status],
                ['Pods', r.pods]];
  if (r.databases && r.databases.length)
    rows.push(['Databases', r.databases.map(d => d.name).join(', ')]);
  $('tTargetInfo').innerHTML = '<div class="kv">' +
    rows.map(([k, v]) => `<div>${k}</div><div class="mono">${v}</div>`).join('') + '</div>';

  const d = await api('/api/tests?topology=' + encodeURIComponent(topo));
  TTESTS = d.tests;
  renderTests();
  $('tClientNs').value = '';
  $('tPassword').value = '';
  $('tClientImage').value = '';
  $('tImageHint').innerHTML = r.version
    ? `Defaults to this release's own image: <span class="mono">${r.version}</span> &mdash;
       demonstrably pullable here, and <span class="mono">redis-cli</span> matches the server.
       Override for a disconnected cluster or an internal mirror.`
    : `Tests run from a throwaway pod that needs <span class="mono">redis-cli</span>.
       Override if this cluster cannot reach <span class="mono">docker.io</span>.`;
  $('tClientHint').textContent =
    'Leave blank to use the first namespace the NetworkPolicy allows, so the real path is exercised.';
  $('tSelectCard').classList.remove('hide');
  $('tReportCard').classList.add('hide');
  $('tSelectCard').scrollIntoView({ behavior: 'smooth' });
}

function renderTests() {
  const tiers = {
    1: ['Tier 1', 'Safe', 'Read-only, or writes confined to a scratch keyspace that is cleaned up afterwards.'],
    2: ['Tier 2', 'Disruptive', 'Kills pods. Seeds a known keyset first, so data loss is a number. Measures the write outage.'],
    3: ['Tier 3', 'Load', 'Consumes real CPU and memory on the cluster.'],
  };
  $('tTiers').innerHTML = [1, 2, 3].map(n => {
    const list = TTESTS.filter(t => t.tier === n);
    if (!list.length) return '';
    const [label, kind, desc] = tiers[n];
    return `<div class="tiergroup">
      <div class="tierhead">${label} &mdash; ${kind}
        <span class="badge">${list.length} test${list.length > 1 ? 's' : ''}</span></div>
      <div class="tierdesc">${desc}</div>
      ${list.map(t => `
        <label class="testrow${t.disruptive ? ' disruptive' : ''}${n === 1 ? ' on' : ''}">
          <input type="checkbox" class="tchk" value="${t.id}"
                 data-disruptive="${t.disruptive}" ${n === 1 ? 'checked' : ''}>
          <span class="tbody">
            <span class="tname">${t.name}${t.disruptive ? '<span class="tag">DISRUPTIVE</span>' : ''}</span>
            <span class="tdesc">${t.describe}</span>
            ${t.expects && t.expects[TTARGET.topology]
              ? `<div class="texp">expected for ${TTARGET.topology}: ${t.expects[TTARGET.topology]}</div>`
              : ''}
          </span>
        </label>`).join('')}
    </div>`;
  }).join('');
  document.querySelectorAll('.tchk').forEach(c => {
    c.onchange = () => {
      c.closest('.testrow').classList.toggle('on', c.checked);
      updateTDanger();
    };
  });
  updateTDanger();
}

function syncTestRows() {
  document.querySelectorAll('.tchk').forEach(c =>
    c.closest('.testrow').classList.toggle('on', c.checked));
}

function chosenTests() {
  return [...document.querySelectorAll('.tchk:checked')].map(c => c.value);
}
function chosenDisruptive() {
  return [...document.querySelectorAll('.tchk:checked')]
    .filter(c => c.dataset.disruptive === 'true').map(c => c.value);
}

function updateTDanger() {
  const d = chosenDisruptive();
  $('tDanger').innerHTML = d.length
    ? `<div class="alert e" style="margin-top:14px"><strong>${d.length} disruptive test(s) selected.</strong>
       Pods will be deleted and the database will be written to. Do not run these against
       anything holding data you care about &mdash; the suite refuses if it finds keys it
       did not write, but treat that as a backstop, not permission.</div>`
    : '';
}

$('tSelectSafe').onclick = () => {
  document.querySelectorAll('.tchk').forEach(c => c.checked = c.dataset.disruptive !== 'true');
  syncTestRows(); updateTDanger();
};
$('tSelectAll').onclick = () => {
  document.querySelectorAll('.tchk').forEach(c => c.checked = true);
  syncTestRows(); updateTDanger();
};
$('tClear').onclick = () => {
  document.querySelectorAll('.tchk').forEach(c => c.checked = false);
  syncTestRows(); updateTDanger();
};

$('tRun').onclick = async () => {
  const tests = chosenTests();
  if (!tests.length) return alert('Select at least one test.');
  const disruptive = chosenDisruptive();
  if (disruptive.length) {
    if (!confirm(`Run ${disruptive.length} DISRUPTIVE test(s) against ${TTARGET.namespace}/${TTARGET.name}?\n\n` +
      `Pods will be deleted. This is a real outage for anything using this Redis.`)) return;
    const typed = prompt(`Type the release name to confirm: ${TTARGET.name}`);
    if (typed !== TTARGET.name) return alert('Cancelled - name did not match.');
  }
  const j = await api('/api/tests/run', {
    method: 'POST',
    body: JSON.stringify({
      kind: TTARGET.kind, namespace: TTARGET.namespace, name: TTARGET.name,
      topology: TTARGET.topology || null,
      password: $('tPassword').value || null,
      client_image: $('tClientImage').value.trim() || null,
      tests, client_namespace: $('tClientNs').value.trim() || null,
      confirm_disruptive: disruptive.length > 0,
    }),
  });
  document.querySelector('.tab[data-tab="deploy"]').click();
  streamJob(j.job_id, (status, result) => {
    document.querySelector('.tab[data-tab="tests"]').click();
    renderTestReport(result);
  });
};

function renderTestReport(r) {
  if (!r || !r.counts) return;
  const c = r.counts;
  $('tSummary').innerHTML =
    `<div class="kv"><div>Target</div><div class="mono">${r.target.namespace}/${r.target.name} (${r.target.topology})</div>
     <div>Result</div><div><span class="ok">${c.pass} passed</span> ·
       <span class="${c.fail ? 'err' : 'dim'}">${c.fail} failed</span> ·
       <span class="${c.warn ? 'warn' : 'dim'}">${c.warn} warnings</span> ·
       <span class="dim">${c.skip} skipped</span></div></div>` +
    '<table style="margin-top:14px"><tr><th>Test</th><th>Status</th><th>Detail</th></tr>' +
    r.results.map(x => `<tr>
      <td>${x.name}</td>
      <td class="${x.status === 'pass' ? 'ok' : x.status === 'fail' ? 'err' : x.status === 'warn' ? 'warn' : 'dim'}">${x.status.toUpperCase()}</td>
      <td style="font-size:11.5px">${x.detail}</td></tr>`).join('') + '</table>';
  $('tReport').textContent = r.report || '';
  $('tReportCard').classList.remove('hide');
}

$('tCopyReport').onclick = () => navigator.clipboard.writeText($('tReport').textContent);
$('tDownloadReport').onclick = () => {
  const blob = new Blob([$('tReport').textContent], { type: 'text/markdown' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `redis-test-${TTARGET.namespace}-${TTARGET.name}-${Date.now()}.md`;
  a.click();
};


/* ------------------------------------------------ memory sanity */

// Redis and Kubernetes invert the convention: Redis "mb" is BINARY (= K8s "Mi")
// while Redis "m" is decimal (= K8s "M"). Parsing one with the other's rules
// understates by up to 7%, which quietly loosens every ratio check.
const REDIS_UNITS = { '': 1, b: 1, k: 1000, kb: 1024, m: 1e6, mb: 1048576,
                      g: 1e9, gb: 1073741824 };
const K8S_UNITS = { '': 1, k: 1000, ki: 1024, m: 1e6, mi: 1048576,
                    g: 1e9, gi: 1073741824, t: 1e12, ti: 1099511627776 };

function parseQty(v, table) {
  const m = /^\s*([\d.]+)\s*([a-z]*)\s*$/i.exec(String(v || ''));
  if (!m) return null;
  const mult = table[m[2].toLowerCase()];
  return mult === undefined ? null : parseFloat(m[1]) * mult;
}
const redisBytes = v => parseQty(v, REDIS_UNITS);
const toBytes = v => parseQty(v, K8S_UNITS);          // Kubernetes quantities

// read a value+unit pair back as a Redis-style string
function maxmemValue(prefix) {
  const n = $(prefix).value.trim();
  return n ? n + $(prefix + 'Unit').value : '';
}
function setMaxmem(prefix, bytes) {
  if (!bytes) { $(prefix).value = ''; return; }
  const unit = bytes % 1073741824 === 0 && bytes >= 1073741824 ? 'gb' : 'mb';
  $(prefix).value = Math.round(bytes / REDIS_UNITS[unit]);
  $(prefix + 'Unit').value = unit;
}
const human = b => b >= 1073741824 ? (b / 1073741824).toFixed(1) + 'Gi'
                 : b >= 1048576 ? Math.round(b / 1048576) + 'Mi' : b + 'B';

function checkMem(prefix) {
  const mmEl = $(prefix === 'c' ? 'cMaxmemory' : 'otMaxmemory');
  const limEl = $(prefix === 'c' ? 'cMemLim' : 'otMemLim');
  const hint = $(prefix === 'c' ? 'cLimitHint' : 'otLimitHint');
  const memHint = $(prefix === 'c' ? 'cMemHint' : 'otMemHint');
  if (!mmEl || !limEl || !hint) return;

  const mm = redisBytes(maxmemValue(prefix === 'c' ? 'cMaxmemory' : 'otMaxmemory')),
        lim = toBytes(limEl.value);
  if (!lim) { hint.innerHTML = ''; return; }

  if (!mm) {
    hint.innerHTML = `<span class="err">maxmemory is not set. Redis will grow until it
      hits this ${human(lim)} ceiling and is <b>OOMKilled</b> — a crash, not an eviction.</span>`;
    return;
  }
  const pct = Math.round(mm / lim * 100);
  const headroom = human(lim - mm);
  if (pct > 80) {
    hint.innerHTML = `<span class="err">maxmemory is ${pct}% of this limit — only ${headroom}
      spare. A BGSAVE forks and copies modified pages, so this will OOMKill under write load.
      Aim for 50-70%.</span>`;
  } else if (pct > 70) {
    hint.innerHTML = `<span class="warn">maxmemory is ${pct}% of this limit (${headroom} spare).
      Tight — 50-70% is the safe band.</span>`;
  } else {
    hint.innerHTML = `<span class="ok">maxmemory is ${pct}% of this limit</span>
      — ${headroom} left for client buffers, replication buffers and BGSAVE copy-on-write.`;
  }
  if (memHint && mm) {
    memHint.innerHTML = `This is the real cache capacity: <b>${human(mm)}</b> of data.
      Disk size does not change it — Redis holds everything in RAM.`;
  }
}


/* ------------------------------------------------ sizing calculator */

// Per-key overhead in Redis. A key is not just its bytes: there is a dictEntry,
// a robj header, and an SDS header for both key and value. These are the
// commonly cited figures for 64-bit Redis 7+; they are approximations, and the
// calculator says so rather than pretending to three decimal places.
const KEY_OVERHEAD = { string: 64, hash: 100, list: 80, set: 80, zset: 120 };
const TYPE_NOTE = {
  string: 'Simplest case: ~64 bytes of metadata per key.',
  hash: 'Small hashes use a compact listpack; large ones switch to a hashtable and cost more.',
  list: 'Quicklist of listpacks — efficient for large lists.',
  set: 'Small integer sets are compact; mixed sets use a hashtable.',
  zset: 'Skiplist plus hashtable — the most expensive structure per element.',
};
const GiB = 1073741824, MiB = 1048576;
const fmt = b => b >= GiB ? (b / GiB).toFixed(2) + ' GiB'
              : b >= MiB ? (b / MiB).toFixed(0) + ' MiB' : Math.round(b) + ' B';
const roundUpGi = b => Math.max(1, Math.ceil(b / GiB)) + 'Gi';

function sizingInputs() {
  return {
    keys: +$('zKeys').value || 0,
    keyLen: +$('zKeyLen').value || 0,
    valLen: +$('zValLen').value || 0,
    type: $('zType').value,
    ttl: $('zTtl').value === '1',
    growth: +$('zGrowth').value,
    persist: $('zPersist').value,
    copies: +$('zCopies').value,
    write: +$('zWrite').value,
  };
}

function computeSizing(i) {
  const raw = i.keys * (i.keyLen + i.valLen);
  const overhead = i.keys * (KEY_OVERHEAD[i.type] + (i.ttl ? 32 : 0));
  const logical = raw + overhead;
  // jemalloc rounds every allocation up to a size class; 1.25x is a common
  // steady-state figure for mixed workloads
  const fragmented = logical * 1.25;
  const maxmemory = fragmented * (1 + i.growth / 100);

  // During BGSAVE/AOF-rewrite Redis forks. Pages the parent modifies while the
  // child writes get copied, so peak RSS exceeds the dataset by roughly the
  // proportion of it that gets written during the save.
  const cow = i.persist === 'none' ? 0 : maxmemory * i.write;
  const buffers = Math.max(64 * MiB, maxmemory * 0.1);
  const container = maxmemory + cow + buffers;

  const diskFactor = i.persist === 'aof' ? 5 : i.persist === 'rdb' ? 3 : 0;
  return {
    raw, overhead, logical, fragmented, maxmemory, cow, buffers, container,
    disk: maxmemory * diskFactor,
    totalRam: container * i.copies,
    pct: Math.round(maxmemory / container * 100),
  };
}

function renderSizing() {
  const i = sizingInputs();
  $('zTypeHint').textContent = TYPE_NOTE[i.type];
  if (!i.keys) { $('zResult').innerHTML = ''; return; }
  const r = computeSizing(i);

  $('zResult').innerHTML = `
    <div class="alert i"><strong>Set <span class="mono">maxmemory ${roundUpGi(r.maxmemory)}</span>
      and a container memory limit of <span class="mono">${roundUpGi(r.container)}</span>.</strong>
      That is ${r.pct}% utilisation — inside the 50–70% safe band.</div>
    <table>
      <tr><th>Step</th><th>Size</th><th>Why</th></tr>
      <tr><td>Raw data</td><td class="mono">${fmt(r.raw)}</td>
          <td class="dim">${i.keys.toLocaleString()} keys × ${i.keyLen + i.valLen} bytes</td></tr>
      <tr><td>+ per-key overhead</td><td class="mono">${fmt(r.overhead)}</td>
          <td class="dim">${KEY_OVERHEAD[i.type]}B metadata${i.ttl ? ' + 32B expiry' : ''} per key</td></tr>
      <tr><td>+ fragmentation</td><td class="mono">${fmt(r.fragmented)}</td>
          <td class="dim">×1.25 — jemalloc rounds to size classes</td></tr>
      <tr><td>+ growth headroom</td><td class="mono"><strong>${fmt(r.maxmemory)}</strong></td>
          <td class="dim">+${i.growth}% → this is your <strong>maxmemory</strong></td></tr>
      <tr><td>+ fork copy-on-write</td><td class="mono">${fmt(r.cow)}</td>
          <td class="dim">${i.persist === 'none' ? 'no persistence, no fork' : 'pages modified during a save get duplicated'}</td></tr>
      <tr><td>+ client &amp; replication buffers</td><td class="mono">${fmt(r.buffers)}</td>
          <td class="dim">output buffers, replication backlog</td></tr>
      <tr><td><strong>Container memory limit</strong></td><td class="mono"><strong>${fmt(r.container)}</strong></td>
          <td class="dim">the Kubernetes <span class="mono">limits.memory</span></td></tr>
      <tr><td>Disk per pod</td><td class="mono">${r.disk ? fmt(r.disk) : 'none needed'}</td>
          <td class="dim">${i.persist === 'aof' ? '5× maxmemory — AOF grows until rewritten, and a rewrite needs both files'
            : i.persist === 'rdb' ? '3× maxmemory — snapshot plus working room' : 'nothing written to disk'}</td></tr>
      <tr><td><strong>Total RAM for ${i.copies} cop${i.copies > 1 ? 'ies' : 'y'}</strong></td>
          <td class="mono"><strong>${fmt(r.totalRam)}</strong></td>
          <td class="dim">every replica holds the full dataset</td></tr>
    </table>`;

  // does it fit on this cluster?
  const pf = STATE.preflight;
  if (pf && pf.nodes) {
    const workers = pf.nodes.filter(n => n.schedulable);
    const need = r.container;
    const fits = workers.filter(n => {
      const alloc = toBytes((n.memory || '0').replace('Ki', 'KiB'));
      return alloc && alloc > need;
    }).length;
    $('zFeasible').innerHTML = fits >= i.copies
      ? `<div class="alert i" style="margin-top:14px"><strong>Fits.</strong> ${fits} of
         ${workers.length} schedulable nodes can hold a ${fmt(need)} pod, and you need
         ${i.copies}. Anti-affinity spreads copies one per node.</div>`
      : `<div class="alert e" style="margin-top:14px"><strong>Does not fit.</strong> You need
         ${i.copies} nodes able to hold ${fmt(need)}, but only ${fits} of ${workers.length}
         schedulable nodes are large enough. Reduce maxmemory, shard the data, or add capacity.</div>`;
  }

  $('zAssumptions').innerHTML = `<div class="hint" style="margin-top:14px">
    Assumptions: 64-bit Redis 7+; overhead figures are typical, not guaranteed —
    measure with <span class="mono">MEMORY USAGE &lt;key&gt;</span> on real data to confirm.
    Fragmentation 1.25× is steady-state; it is far higher on a nearly empty instance.
    Copy-on-write is the hardest to predict — it depends on how much of the dataset is
    written while a save runs.</div>`;
}

['zKeys', 'zKeyLen', 'zValLen', 'zType', 'zTtl', 'zGrowth', 'zPersist', 'zCopies', 'zWrite']
  .forEach(id => { const e = $(id); if (e) e.addEventListener('input', renderSizing); });
document.querySelector('.tab[data-tab="sizing"]').addEventListener('click', () => {
  renderSizing(); renderReverse();
});

function renderReverse() {
  const mem = toBytes($('rMem').value);
  if (!mem) { $('rResult').innerHTML = ''; return; }
  const persist = $('rPersist').value, product = $('rProduct').value;

  if (product === 'enterprise') {
    const overhead = 4 * GiB;   // ~12 processes: control plane, envoy, metrics, cluster manager
    const usable = Math.max(0, (mem - overhead) * 0.85);
    $('rResult').innerHTML = usable <= 0
      ? `<div class="alert e">${fmt(mem)} is below what Redis Enterprise needs just for its own
         processes (~4 GiB). Its documented production minimum is ~15 GiB per node.</div>`
      : `<div class="alert i"><strong>Usable dataset: about ${fmt(usable)} per node.</strong></div>
         <table><tr><th>Component</th><th>Size</th></tr>
         <tr><td>Node memory</td><td class="mono">${fmt(mem)}</td></tr>
         <tr><td>Redis Enterprise processes</td><td class="mono">−${fmt(overhead)}</td></tr>
         <tr><td>Shard overhead and buffers (15%)</td><td class="mono">−${fmt((mem - overhead) * 0.15)}</td></tr>
         <tr><td><strong>Dataset that fits</strong></td><td class="mono"><strong>${fmt(usable)}</strong></td></tr></table>
         ${mem < 15 * GiB ? `<div class="alert w" style="margin-top:12px">${fmt(mem)} is below Redis's
           documented ~15 GiB production minimum per node. It will run, but it is an unsupported
           configuration on a support ticket.</div>` : ''}`;
    return;
  }

  const cowFactor = persist === 'none' ? 0 : 0.5;
  const maxmemory = mem / (1 + cowFactor + 0.1);
  $('rResult').innerHTML =
    `<div class="alert i"><strong>Set <span class="mono">maxmemory ${roundUpGi(maxmemory)}</span>
      — about ${fmt(maxmemory)} of cache.</strong> That is ${Math.round(maxmemory / mem * 100)}% of the limit.</div>
     <table><tr><th>Component</th><th>Size</th></tr>
     <tr><td>Container memory limit</td><td class="mono">${fmt(mem)}</td></tr>
     <tr><td>maxmemory (the dataset)</td><td class="mono"><strong>${fmt(maxmemory)}</strong></td></tr>
     <tr><td>Reserved for fork copy-on-write</td><td class="mono">${fmt(maxmemory * cowFactor)}</td></tr>
     <tr><td>Reserved for buffers</td><td class="mono">${fmt(maxmemory * 0.1)}</td></tr>
     <tr><td>Disk needed</td><td class="mono">${persist === 'aof' ? fmt(maxmemory * 5)
       : persist === 'rdb' ? fmt(maxmemory * 3) : 'none'}</td></tr></table>`;
}
['rMem', 'rPersist', 'rProduct'].forEach(id => {
  const e = $(id); if (e) e.addEventListener('input', renderReverse);
});


/* ------------------------------------------------ keyspace analyzer */

$('zMeasure').onclick = async () => {
  const box = $('zMeasurePick');
  box.classList.remove('hide');
  $('zRelTable').innerHTML = '<tr><td class="dim"><span class="spin"></span> scanning…</td></tr>';
  try {
    const d = await api('/api/discover');
    RELEASES = d.releases;
    $('zRelTable').innerHTML = RELEASES.length
      ? '<tr><th>Type</th><th>Namespace / name</th><th>Status</th><th></th></tr>' +
        RELEASES.map((r, i) => `<tr>
          <td>${r.kind}</td><td class="mono">${r.namespace}/<strong>${r.name}</strong></td>
          <td class="${r.deleting ? 'err' : 'ok'}">${r.status}</td>
          <td><button class="ghost" style="padding:4px 10px"
              onclick="runAnalysis(${i})">Measure</button></td></tr>`).join('')
      : '<tr><td class="dim">Nothing deployed to measure.</td></tr>';
  } catch (e) { $('zRelTable').innerHTML = `<tr><td class="err">${e.message}</td></tr>`; }
};

window.runAnalysis = async (i) => {
  const r = RELEASES[i];
  const j = await api('/api/analyze', {
    method: 'POST',
    body: JSON.stringify({ kind: r.kind, namespace: r.namespace, name: r.name,
                           topology: r.topology || null, tests: [] }),
  });
  document.querySelector('.tab[data-tab="deploy"]').click();
  streamJob(j.job_id, (status, result) => {
    document.querySelector('.tab[data-tab="sizing"]').click();
    if (result && result.analysis) renderAnalysis(result.analysis);
  });
};

function renderAnalysis(a) {
  if (a.empty) {
    $('zAnalysis').innerHTML =
      '<div class="alert w">That database is empty — nothing to measure. Put real data in it first.</div>';
    return;
  }
  const s = a.sample || {};

  // prefill the calculator from what was measured
  const sg = a.sizing_suggestion;
  if (sg) {
    $('zKeys').value = sg.keys;
    $('zKeyLen').value = sg.keyLen;
    $('zValLen').value = sg.valLen;
    $('zType').value = sg.type;
    $('zTtl').value = sg.ttl ? '1' : '0';
  }

  const findings = (a.findings || []).map(f =>
    `<div class="alert ${f.level === 'err' ? 'e' : f.level === 'warn' ? 'w' : 'i'}"
          style="margin-bottom:8px"><strong>${f.title}</strong><br>${f.detail}</div>`).join('');

  $('zAnalysis').innerHTML = `
    <h3 style="font-size:13px;margin:18px 0 8px">Measured from
      <span class="mono">${a.namespace}/${a.pod}</span></h3>
    ${findings}
    <table>
      <tr><th>Metric</th><th>Value</th><th>Notes</th></tr>
      <tr><td>Keys</td><td class="mono">${a.keys.toLocaleString()}</td>
          <td class="dim">${a.keys_with_ttl.toLocaleString()} with a TTL (${Math.round(a.ttl_ratio * 100)}%)</td></tr>
      <tr><td>Memory in use</td><td class="mono">${a.used_memory_human} of ${a.maxmemory_human || 'unlimited'}</td>
          <td class="dim">policy ${a.maxmemory_policy}</td></tr>
      <tr><td>Mean bytes/key</td><td class="mono">${a.measured_bytes_per_key}</td>
          <td class="dim">used_memory ÷ keys — size capacity with this</td></tr>
      <tr><td>Typical (p50) bytes/key</td><td class="mono">${a.typical_bytes_per_key ?? '-'}</td>
          <td class="dim">${a.skew ? `mean is ${a.skew}× the median — outliers dominate` : 'from the sample'}</td></tr>
      <tr><td>Key length</td><td class="mono">${s.avg_key_length ?? '-'} B</td>
          <td class="dim">average over ${s.n ?? 0} sampled keys</td></tr>
      <tr><td>Value size</td><td class="mono">${s.avg_value_size ?? '-'}</td>
          <td class="dim">${s.dominant_type === 'string' ? 'bytes' : 'elements per container'}</td></tr>
      <tr><td>Per-key spread</td><td class="mono">p50 ${s.p50_memory} / p95 ${s.p95_memory} / max ${s.max_memory_in_sample}</td>
          <td class="dim">bytes, from the sample</td></tr>
      <tr><td>Types</td><td class="mono">${Object.entries(s.types || {}).map(([k, v]) => `${k} ${v}%`).join(', ')}</td>
          <td class="dim">dominant: ${s.dominant_type ?? '-'}</td></tr>
      <tr><td>Fragmentation</td><td class="mono">${a.fragmentation_ratio}</td>
          <td class="dim">RSS ÷ used_memory</td></tr>
      <tr><td>Hit ratio</td><td class="mono">${a.hit_ratio != null ? a.hit_ratio + '%' : 'too few reads'}</td>
          <td class="dim">${a.keyspace_hits.toLocaleString()} hits / ${a.keyspace_misses.toLocaleString()} misses</td></tr>
      <tr><td>Evicted keys</td><td class="mono ${a.evicted_keys ? 'warn' : ''}">${a.evicted_keys.toLocaleString()}</td>
          <td class="dim">${a.evicted_keys ? 'the working set does not fit' : 'nothing dropped'}</td></tr>
    </table>
    ${(a.bigkeys || []).length ? `<h3 style="font-size:13px;margin:18px 0 8px">Largest keys</h3>
      <pre style="max-height:180px">${a.bigkeys.join('\n')}</pre>` : ''}
    ${(a.top_commands || []).length ? `<h3 style="font-size:13px;margin:18px 0 8px">Command profile</h3>
      <table><tr><th>Command</th><th>Calls</th><th>µs/call</th></tr>
      ${a.top_commands.map(c => `<tr><td class="mono">${c.command}</td>
        <td class="mono">${c.calls.toLocaleString()}</td>
        <td class="mono ${c.usec_per_call > 1000 ? 'warn' : ''}">${c.usec_per_call.toFixed(1)}</td></tr>`).join('')}
      </table>` : ''}
    <div class="alert i" style="margin-top:12px">The calculator below has been filled in from
      these measurements. Adjust the growth headroom and replica count, and it will size the
      deployment for you.</div>`;

  renderSizing();
  $('zAnalysis').scrollIntoView({ behavior: 'smooth' });
}


/* ------------------------------------------------ redis.conf presets */

// Only directives that are NOT already the Redis default, and that are
// genuinely worth setting on Kubernetes. tcp-keepalive 300 and timeout 0 are
// defaults already -- suggesting them would be noise.
const CONF_PRESETS = {
  cache: `# free memory in a background thread instead of blocking the event loop.
# Default is "no" for all of these. Matters most when you have large keys:
# deleting or evicting one synchronously stalls every other client.
lazyfree-lazy-eviction yes
lazyfree-lazy-expire yes
lazyfree-lazy-server-del yes
replica-lazy-flush yes

# cap TOTAL client output buffers (Redis 7+, default unlimited). Without it a
# single slow consumer doing a large MGET can balloon RSS and OOMKill the pod.
maxmemory-clients 5%

# CACHE ONLY. Default "yes" stops Redis accepting writes if a background save
# fails -- a self-inflicted outage for data you can repopulate.
# Leave the default for a datastore.
stop-writes-on-bgsave-error no

# diagnosis: keep more slow entries, and enable latency tracking (off by default)
slowlog-max-len 256
latency-monitor-threshold 100`,

  replication: `# default is 1mb, which a busy primary overruns in seconds. A bigger backlog
# lets a briefly disconnected replica do a PARTIAL resync instead of a full
# RDB transfer.
repl-backlog-size 64mb
repl-backlog-ttl 3600

# give replicas room before the primary drops them mid-sync
client-output-buffer-limit replica 512mb 128mb 60

# fail loudly rather than serving data known to be stale
replica-serve-stale-data no`,
};

['otPresetCache', 'otPresetRepl', 'otPresetClear'].forEach(id => {
  const el = $(id);
  if (!el) return;
  el.onclick = (e) => {
    e.preventDefault();
    const box = $('otExtraConf');
    if (id === 'otPresetClear') { box.value = ''; return; }
    const add = id === 'otPresetCache' ? CONF_PRESETS.cache : CONF_PRESETS.replication;
    box.value = box.value.trim() ? box.value.trim() + '\n\n' + add : add;
    box.style.height = 'auto';
    box.style.height = Math.min(320, box.scrollHeight) + 'px';
  };
});


/* ------------------------------------------------ registry mirror */

function mirrorSettings() {
  const reg = $('mirrorReg') ? $('mirrorReg').value.trim() : '';
  return reg ? { registry: reg, mode: $('mirrorMode').value } : null;
}

function mirrorPreview(img) {
  const m = mirrorSettings();
  if (!m) return img;
  const [first, ...rest] = img.split('/');
  const hasReg = rest.length && (first.includes('.') || first.includes(':') || first === 'localhost');
  const registry = hasReg ? first : 'docker.io';
  const path = hasReg ? rest.join('/') : img;
  return m.mode === 'prefix' ? `${m.registry}/${registry}/${path}` : `${m.registry}/${path}`;
}

function updateMirrorHint() {
  const ex = 'docker.io/redis:8.2-alpine';
  $('mirrorHint').innerHTML = mirrorSettings()
    ? `Resolves as: <span class="mono">${ex}</span> &rarr;
       <span class="mono ok">${mirrorPreview(ex)}</span>`
    : 'No mirror set &mdash; images are used exactly as the catalogue lists them.';
}
['mirrorReg', 'mirrorMode'].forEach(id => {
  const e = $(id); if (e) e.addEventListener('input', updateMirrorHint);
  if (e) e.addEventListener('change', updateMirrorHint);
});
if ($('mirrorHint')) updateMirrorHint();

$('mirrorTest').onclick = async () => {
  const btn = $('mirrorTest');
  const ns = $('mirrorNs').value.trim();
  if (!ns) return alert('Pick a namespace the probe pod can run in.');
  btn.disabled = true; btn.textContent = 'Pulling…';
  $('mirrorResult').innerHTML = '<div class="alert i"><span class="spin"></span> starting a probe pod…</div>';
  const m = mirrorSettings();
  try {
    const d = await api('/api/mirror/test', {
      method: 'POST',
      body: JSON.stringify({
        image: 'docker.io/redis:8.2-alpine', namespace: ns,
        registry: m ? m.registry : '', mode: m ? m.mode : 'replace',
      }),
    });
    $('mirrorResult').innerHTML = d.ok
      ? `<div class="alert i"><strong>Pull succeeded.</strong>
         <span class="mono">${d.image}</span> is reachable from this cluster.</div>`
      : `<div class="alert e"><strong>Pull failed${d.reason ? ' — ' + d.reason : ''}.</strong>
         <span class="mono">${d.image}</span>
         ${d.detail ? '<pre style="margin-top:8px;max-height:140px">' + d.detail + '</pre>' : ''}
         <div style="margin-top:8px">Check the tag exists on the mirror, that the path
         convention matches how it namespaces upstream registries, and that a pull secret
         covers it.</div></div>`;
  } catch (e) {
    $('mirrorResult').innerHTML = `<div class="alert e">${e.message}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = 'Test pull';
  }
};

/* ------------------------------------------------ day-2 */

let DCUR = null;
let DSIZES = [];   // distinct PVC sizes, largest first

document.querySelector('.tab[data-tab="day2"]').addEventListener('click', scanForDay2);
$('dRefresh').onclick = scanForDay2;

async function scanForDay2() {
  $('dScanState').innerHTML = '<span class="spin"></span> scanning…';
  try {
    const d = await api('/api/discover');
    RELEASES = d.releases;
    $('dScanState').textContent =
      `${RELEASES.length} release(s) · scanned ${new Date().toLocaleTimeString()}`;
    $('dTable').innerHTML = RELEASES.length
      ? '<tr><th></th><th>Type</th><th>Namespace / name</th><th>Version</th><th>Status</th></tr>' +
        RELEASES.map((r, i) => `<tr>
          <td><input type="radio" name="drel" value="${i}" style="width:auto"></td>
          <td>${r.kind}${r.cr_kind ? ' <span class="dim">/ ' + r.cr_kind + '</span>' : ''}</td>
          <td class="mono">${r.namespace}/<strong>${r.name}</strong></td>
          <td class="mono" style="font-size:11px">${r.version || '-'}</td>
          <td class="${r.deleting ? 'err' : 'ok'}">${r.status}</td></tr>`).join('')
      : '<tr><td class="dim">Nothing deployed.</td></tr>';
    document.querySelectorAll('input[name=drel]').forEach(rb => {
      rb.onchange = () => pickDay2(RELEASES[parseInt(rb.value)]);
    });
  } catch (e) { $('dScanState').innerHTML = `<span class="err">${e.message}</span>`; }
}

function pickDay2(r) {
  DCUR = r;
  $('dTitle').textContent = `${r.namespace}/${r.name}`;
  $('dImage').value = r.version || '';
  $('dImageHint').innerHTML = `Currently <span class="mono">${r.version || 'unknown'}</span>.
    Checked before applying: the cluster must be able to pull it, and a downgrade is refused
    unless forced &mdash; Redis does not guarantee an older server can read a newer RDB or AOF.`;
  if (r.topology === 'standalone') {
    $('dScaleHint').innerHTML = 'A standalone release is one pod. Scaling past 1 would give you independent, unsynchronised copies — use the Replication topology instead.';
  }
  // Never prefill these from a constant: a hardcoded value reads as the current
  // state, and acting on it would silently change something.
  ['dMaxmemory', 'dMemLimit', 'dMemRequest', 'dCpuRequest', 'dCpuLimit',
   'dReplicas', 'dStorage'].forEach(id => $(id).value = '');
  $('dStorage').placeholder = 'loading…';
  $('dReplicas').placeholder = 'loading…';
  $('dQosHint').innerHTML = '';
  $('dMemHint').innerHTML = 'Reading the live values…';
  $('dScaleHint').innerHTML = 'Reading the live values…';
  $('dStorageHint').innerHTML = 'Reading the live values…';

  api('/api/status?namespace=' + encodeURIComponent(r.namespace) + '&name=' + encodeURIComponent(r.name))
    .then(st => {
      const L = st.live || {};
      if (L.maxmemory) {
        setMaxmem('dMaxmemory', redisBytes(L.maxmemory.replace(/([MG])$/, (x) => x.toLowerCase() + 'b')));
        $('dMemHint').innerHTML = `Currently <span class="mono">${L.maxmemory}</span>,
          <span class="mono">${L.used_memory}</span> in use, policy
          <span class="mono">${L.maxmemory_policy}</span>. RAM only &mdash; the PVC does not affect it.`;
      } else {
        $('dMemHint').innerHTML = 'RAM. Growing the PVC does not change this.';
      }

      const R = st.resources || {};
      $('dMemLimit').value = R.memory_limit || '';
      $('dMemRequest').value = R.memory_request || '';
      $('dCpuRequest').value = R.cpu_request || '';
      $('dCpuLimit').value = R.cpu_limit || '';
      $('dLimitHint').innerHTML = R.memory_limit
        ? `Currently <span class="mono">${R.memory_limit}</span>. Leave every field unchanged
           except maxmemory and the change applies <strong>live with no restart</strong>;
           touching any of these rolls the pods.`
        : 'No limit set — the container can grow until the node runs out.';
      updateQosHint();

      // replicas: show and prefill what it actually runs
      const w = (st.workloads || [])[0];
      if (w && w.ready) {
        const want = w.ready.split('/')[1];
        $('dReplicas').value = want;
        $('dScaleHint').innerHTML = `Currently <span class="mono">${w.ready}</span> ready
          (${w.kind}). Removing a replica is immediate; adding one syncs a full copy from the
          primary first.`;
      } else {
        $('dScaleHint').innerHTML = 'Could not read the current replica count.';
      }

      // storage: prefill the real size so "Grow" is an explicit increase
      const pvcs = st.pvcs || [];
      if (pvcs.length) {
        const sizes = [...new Set(pvcs.map(p => p.capacity).filter(Boolean))];
        // Sizes legitimately differ: grow the PVCs, scale up, and the new
        // replicas came from the old volumeClaimTemplates. Prefill the LARGEST
        // so the obvious action is levelling the rest up to it. Blanking the
        // field here is what made it look stuck on "loading".
        const sorted = [...sizes].sort((a, b) => toBytes(b) - toBytes(a));
        DSIZES = sorted;
        $('dStorage').value = sorted[0] || '';
        const sc = [...new Set(pvcs.map(p => p.storage_class))].join(', ');
        const mixed = sorted.length > 1
          ? `<br><strong>These are not all the same size.</strong> Growing to
             <span class="mono">${sorted[0]}</span> levels the smaller ones up and also
             rewrites the StatefulSet template, so replicas added later match.`
          : '';
        $('dStorageHint').innerHTML = `${pvcs.length} volume(s) at
          <span class="mono">${sorted.join(', ') || '?'}</span> on
          <span class="mono">${sc}</span>. Enter a LARGER size &mdash; Kubernetes can grow a
          volume but never shrink it, and only when the StorageClass allows expansion.
          This is disk for the AOF/RDB files; it does <strong>not</strong> change cache
          capacity.${mixed}`;
      } else {
        DSIZES = [];
        $('dStorage').value = '';
        $('dStorageHint').innerHTML = 'No PersistentVolumeClaims found for this release.';
      }
    })
    .catch(() => {
      $('dMemHint').textContent = 'Could not read the live values.';
      $('dScaleHint').textContent = 'Could not read the live values.';
      $('dStorageHint').textContent = 'Could not read the live values.';
      // otherwise the field keeps the "loading…" placeholder and looks stuck
      DSIZES = [];
      $('dStorage').placeholder = 'could not read the current size';
      $('dReplicas').placeholder = 'could not read';
    });
  loadAcl();
  $('dOpCard').classList.remove('hide');
  $('dOpCard').scrollIntoView({ behavior: 'smooth' });
}

async function runDay2(operation, extra, confirmMsg) {
  if (!DCUR) return;
  if (confirmMsg && !confirm(confirmMsg)) return;
  const j = await api('/api/day2', {
    method: 'POST',
    body: JSON.stringify({
      operation, kind: DCUR.kind, namespace: DCUR.namespace, name: DCUR.name,
      cr_plural: DCUR.cr_plural || null, ...extra,
    }),
  });
  document.querySelector('.tab[data-tab="deploy"]').click();
  streamJob(j.job_id, () => { RELEASES = []; });
}

$('dScale').onclick = () => runDay2('scale',
  { replicas: parseInt($('dReplicas').value) || 1 },
  `Scale ${DCUR.namespace}/${DCUR.name} to ${$('dReplicas').value} replica(s)?`);

$('dGrow').onclick = () => {
  const want = $('dStorage').value.trim();
  if (!want) return alert('Enter the new size.');
  const wantB = toBytes(want);
  if (!wantB) return alert(`"${want}" is not a size Kubernetes understands. Use 10Gi, 50Gi, 1Ti.`);

  // Compare against the LARGEST claim, not the first one listed. With a mixed
  // set the largest is the only safe floor: anything below it would be a shrink
  // for at least one volume.
  const biggest = DSIZES[0];
  if (biggest && wantB < toBytes(biggest))
    return alert(`The largest volume is already ${biggest}. Kubernetes can grow a volume `
      + `but never shrink it, so ${want} would be rejected.`);
  if (biggest && wantB === toBytes(biggest) && DSIZES.length === 1)
    return alert(`That is the size it already is (${biggest}). Enter a larger value to grow it.`);

  const levelling = DSIZES.length > 1 && wantB === toBytes(biggest);
  return runDay2('storage',
  { storage_size: want },
  (levelling
    ? `Level every PVC of ${DCUR.namespace}/${DCUR.name} up to ${want}?\n\n`
      + `Currently ${DSIZES.join(', ')}. The ones already at ${want} are left alone.\n\n`
    : `Grow every PVC of ${DCUR.namespace}/${DCUR.name} to ${want}?\n\n`) +
  `The StatefulSet template is rewritten too, so replicas added later match.\n`
  + `This cannot be undone — Kubernetes can grow a volume but never shrink it.`);
};

$('dBump').onclick = () => runDay2('image',
  { image: $('dImage').value.trim(), force: $('dForce').checked },
  `Change ${DCUR.namespace}/${DCUR.name} to ${$('dImage').value}?\n\n` +
  `The pods will roll. On a replication set the REPLICAS upgrade before the primary.`);


$('dMem').onclick = () => {
  const mm = maxmemValue('dMaxmemory'), lim = $('dMemLimit').value.trim();
  if (!mm && !lim) return alert('Give a new maxmemory, a new container limit, or both.');
  let msg = `Change cache size on ${DCUR.namespace}/${DCUR.name}?\n\n`;
  msg += mm ? `maxmemory -> ${mm}\n` : '';
  msg += lim ? `container limit -> ${lim}\n\nChanging the limit is a pod spec change, so the pods WILL ROLL.`
             : `\nThe container limit is unchanged, so this applies live with no restart.`;
  runDay2('memory', {
    maxmemory: mm || null, memory_limit: lim || null,
    memory_request: $('dMemRequest').value.trim() || null,
    cpu_request: $('dCpuRequest').value.trim() || null,
    cpu_limit: $('dCpuLimit').value.trim() || null,
    force: $('dMemForce').checked,
  }, msg);
};


/* ------------------------------------------------ day-2 resource hints */

function updateQosHint() {
  const el = $('dQosHint');
  if (!el) return;
  const ml = $('dMemLimit').value.trim(), mr = $('dMemRequest').value.trim();
  const cl = $('dCpuLimit').value.trim(), cr = $('dCpuRequest').value.trim();
  const mm = maxmemValue('dMaxmemory');

  const bits = [];
  if (ml && mr && cl && cr) {
    bits.push(ml === mr && cl === cr
      ? '<span class="ok">QoS Guaranteed</span> — requests equal limits, so this is the last thing evicted under node pressure.'
      : '<span class="warn">QoS Burstable</span> — requests differ from limits. Set them equal for Guaranteed, which is evicted last.');
  }
  const b = redisBytes(mm), lim = toBytes(ml);
  if (b && lim) {
    const pct = Math.round(b / lim * 100);
    bits.push(pct > 80
      ? `<span class="err">maxmemory is ${pct}% of the limit</span> — a BGSAVE fork will OOMKill this under write load. Aim for 50–70%.`
      : pct > 70
        ? `<span class="warn">maxmemory is ${pct}% of the limit</span> — tight; 50–70% is the safe band.`
        : `<span class="ok">maxmemory is ${pct}% of the limit</span> — inside the safe band.`);
  }
  el.innerHTML = bits.join('<br>');
}
['dMaxmemory', 'dMaxmemoryUnit', 'dMemLimit', 'dMemRequest', 'dCpuRequest', 'dCpuLimit'].forEach(id => {
  const e = $(id); if (e) e.addEventListener('input', updateQosHint);
});


/* ------------------------------------------------ ACL users: deploy form */

let CUSERS = [];
let ACL_PRESETS = [];

async function loadAclPresets() {
  if (ACL_PRESETS.length) return;
  try {
    const d = await api('/api/acl?namespace=default&name=none');
    ACL_PRESETS = d.presets || [];
  } catch { ACL_PRESETS = []; }
}

function renderCUsers() {
  const t = $('cUserTable');
  if (!CUSERS.length) { t.innerHTML = ''; return; }
  t.innerHTML = '<tr><th>Username</th><th>Key pattern</th><th>Permissions</th><th></th></tr>' +
    CUSERS.map((u, i) => `<tr>
      <td><input value="${u.username}" onchange="CUSERS[${i}].username=this.value"></td>
      <td><input value="${u.key_pattern}" onchange="CUSERS[${i}].key_pattern=this.value"></td>
      <td><select onchange="CUSERS[${i}].permissions=this.value">
        ${ACL_PRESETS.map(p => `<option value="${p.id}"${p.id === u.permissions ? ' selected' : ''}>${p.label}</option>`).join('')}
      </select></td>
      <td><button class="ghost" style="padding:4px 10px"
          onclick="CUSERS.splice(${i},1);renderCUsers()">Remove</button></td></tr>`).join('');
}
window.CUSERS = CUSERS;

$('cAddUser').onclick = async (e) => {
  e.preventDefault();
  await loadAclPresets();
  CUSERS.push({ username: '', key_pattern: '*', permissions: 'readwrite', channels: '' });
  window.CUSERS = CUSERS;
  renderCUsers();
};

/* ------------------------------------------------ ACL users: day-2 */

async function loadAcl() {
  if (!DCUR) return;
  await loadAclPresets();
  if (!$('aclPerm').innerHTML) {
    $('aclPerm').innerHTML = ACL_PRESETS.map(p =>
      `<option value="${p.id}"${p.id === 'readwrite' ? ' selected' : ''}>${p.label}</option>`).join('');
    $('aclPerm').onchange = () => {
      const p = ACL_PRESETS.find(x => x.id === $('aclPerm').value);
      $('aclPermHint').textContent = p ? p.detail : '';
    };
    $('aclPerm').onchange();
  }
  $('dAclTable').innerHTML = '<tr><td class="dim">reading the ACL…</td></tr>';
  try {
    const d = await api(`/api/acl?namespace=${encodeURIComponent(DCUR.namespace)}&name=${encodeURIComponent(DCUR.name)}`);
    $('dAclTable').innerHTML = d.users.length
      ? '<tr><th>User</th><th>Enabled</th><th>Keys</th><th>Channels</th><th>Commands</th></tr>' +
        d.users.map(u => `<tr>
          <td class="mono">${u.username}${u.username === 'default' ? ' <span class="dim">(admin)</span>' : ''}</td>
          <td class="${u.enabled ? 'ok' : 'warn'}">${u.enabled ? 'on' : 'off'}</td>
          <td class="mono" style="font-size:11px">${u.keys}</td>
          <td class="mono" style="font-size:11px">${u.channels}</td>
          <td class="mono" style="font-size:11px">${u.commands}</td></tr>`).join('')
      : '<tr><td class="dim">Could not read the ACL.</td></tr>';
  } catch (e) {
    $('dAclTable').innerHTML = `<tr><td class="err">${e.message}</td></tr>`;
  }
}

$('aclGen').onclick = async (e) => {
  e.preventDefault();
  $('aclPass').value = (await api('/api/genpassword')).password;
};

function aclSpec() {
  return {
    username: $('aclUser').value.trim(),
    password: $('aclPass').value.trim() || null,
    key_pattern: $('aclKeys').value.trim() || '*',
    permissions: $('aclPerm').value,
    channels: $('aclChannels').value.trim(),
    enabled: true,
  };
}

$('aclCreate').onclick = () => {
  const u = aclSpec();
  if (!u.username) return alert('Give the user a name.');
  runDay2('acl', { acl_action: 'create', user: u },
    `Create or update ACL user '${u.username}' on ${DCUR.namespace}/${DCUR.name}?\n\n` +
    `Keys: ${u.key_pattern}\nPermissions: ${u.permissions}\n\n` +
    `Applied live and written to the ConfigMap so it survives a restart.`);
};

$('aclDelete').onclick = () => {
  const u = aclSpec();
  if (!u.username) return alert('Give the user a name.');
  runDay2('acl', { acl_action: 'delete', user: u },
    `DELETE ACL user '${u.username}'?\n\nAny application using it will start getting ` +
    `WRONGPASS immediately.`);
};

/* ------------------------------------------------ console (redis-cli) */

let CLI = { rel: null, mode: 'read', hist: [], hpos: -1 };

async function scanForCli() {
  $('cScanState').innerHTML = '<span class="spin"></span> scanning…';
  try {
    const d = await api('/api/discover');
    const rels = d.releases.filter(r => !r.deleting);
    $('cScanState').textContent =
      `${rels.length} release(s) · scanned ${new Date().toLocaleTimeString()}`;
    $('cTable').innerHTML = rels.length
      ? '<tr><th></th><th>Type</th><th>Namespace / name</th><th>Status</th></tr>' +
        rels.map((r, i) => `<tr>
          <td><input type="radio" name="crel" value="${i}" style="width:auto"></td>
          <td>${r.kind}${r.cr_kind ? ' <span class="dim">/ ' + r.cr_kind + '</span>' : ''}</td>
          <td class="mono">${r.namespace}/<strong>${r.name}</strong></td>
          <td class="ok">${r.status}</td></tr>`).join('')
      : '<tr><td class="dim">Nothing deployed.</td></tr>';
    document.querySelectorAll('input[name=crel]').forEach(rb => {
      rb.onchange = () => pickCli(rels[parseInt(rb.value)]);
    });
  } catch (e) { $('cScanState').innerHTML = `<span class="err">${e.message}</span>`; }
}

async function pickCli(r) {
  CLI.rel = r;
  $('cPicked').classList.remove('hide');
  $('cUnlockName').textContent = r.name;
  $('cPod').innerHTML = '<option value="">loading…</option>';
  try {
    const pd = await api(`/api/cli/pods?namespace=${encodeURIComponent(r.namespace)}&name=${encodeURIComponent(r.name)}`);
    $('cPod').innerHTML = (pd.pods || []).length
      ? pd.pods.map(p =>
          `<option value="${p.name}">${p.name}${p.role ? ' — ' + p.role : ''}</option>`).join('')
      : '<option value="">(first pod)</option>';
  } catch (e) { $('cPod').innerHTML = '<option value="">(first pod)</option>'; }
  $('cUser').innerHTML = '<option value="default">default</option>';
  try {
    const d = await api(`/api/acl?namespace=${encodeURIComponent(r.namespace)}&name=${encodeURIComponent(r.name)}`);
    const names = (d.users || []).map(u => u.username || u.name).filter(Boolean);
    if (names.length) {
      $('cUser').innerHTML = names.map(n => `<option value="${n}">${n}</option>`).join('');
    }
  } catch (e) { /* ACL listing is a nicety; default always works */ }
  $('cInput').disabled = false;
  $('cPrompt').textContent = `${r.namespace}/${r.name}>`;
  if (!$('cTerm').dataset.greeted) {
    termWrite('meta', `Connected through oc exec to ${r.namespace}/${r.name}. ` +
      `Read-only mode. Type help for what that allows.`);
    $('cTerm').dataset.greeted = '1';
  }
  $('cInput').focus();
}

function termWrite(cls, text) {
  const t = $('cTerm');
  const d = document.createElement('div');
  d.className = cls;
  d.textContent = text;
  t.appendChild(d);
  t.scrollTop = t.scrollHeight;
}

document.querySelectorAll('#cModes .opt').forEach(o => {
  o.onclick = () => {
    const want = o.dataset.mode;
    if (want === 'admin' && CLI.mode !== 'admin') {
      $('cUnlock').classList.remove('hide');
      $('cUnlockInput').value = '';
      $('cUnlockInput').focus();
      $('cUnlockInput').oninput = () => {
        if ($('cUnlockInput').value.trim() === (CLI.rel ? CLI.rel.name : '')) {
          setCliMode('admin');
          $('cUnlock').classList.add('hide');
          $('cInput').focus();
        }
      };
      return;
    }
    $('cUnlock').classList.add('hide');
    setCliMode(want);
  };
});

function setCliMode(m) {
  CLI.mode = m;
  document.querySelectorAll('#cModes .opt').forEach(x =>
    x.classList.toggle('sel', x.dataset.mode === m));
  termWrite('note', `— mode: ${m} —`);
}

const CLI_HELP = {
  read: 'Read-only. Anything Redis flags readonly: GET, MGET, SCAN, TTL, EXISTS, TYPE, ' +
        'LRANGE, HGETALL, INFO, CONFIG GET, ACL LIST, MEMORY USAGE, SLOWLOG GET, CLIENT LIST.',
  write: 'Read plus anything Redis flags write: SET, SETEX, DEL, EXPIRE, INCR, LPUSH, HSET, ' +
         'SADD, ZADD, RENAME, COPY, SORT. These change application data.',
  admin: 'Read, write, plus anything Redis flags admin: CONFIG SET, ACL SETUSER, ACL DELUSER, ' +
         'CLIENT KILL, SLOWLOG RESET, LATENCY RESET, CLUSTER SETSLOT. These change the server.',
};

async function cliSubmit() {
  const inp = $('cInput');
  const cmd = inp.value.trim();
  if (!cmd || !CLI.rel) return;
  inp.value = '';
  CLI.hist.push(cmd); CLI.hpos = CLI.hist.length;

  termWrite('echo', `${CLI.rel.name}> ${cmd}`);

  const low = cmd.toLowerCase();
  if (low === 'clear') { $('cTerm').innerHTML = ''; return; }
  if (low === 'help') {
    termWrite('out', CLI_HELP[CLI.mode]);
    termWrite('deny', 'Refused in every mode, no unlock: FLUSHALL, FLUSHDB, SHUTDOWN, DEBUG, ' +
      'REPLICAOF, SLAVEOF, FAILOVER, SWAPDB, MIGRATE, CLUSTER RESET, CLUSTER FORGET, ' +
      'CLUSTER FAILOVER, SCRIPT FLUSH, FUNCTION FLUSH.');
    return;
  }
  if (low === 'exit' || low === 'quit') {
    termWrite('note', 'Nothing to close — each command is its own short-lived oc exec.');
    return;
  }

  inp.disabled = true;
  try {
    const d = await api('/api/cli', {
      method: 'POST',
      body: JSON.stringify({
        namespace: CLI.rel.namespace, name: CLI.rel.name,
        pod: $('cPod').value, username: $('cUser').value,
        mode: CLI.mode, command: cmd,
      }),
    });
    if (d.error) { termWrite('deny', d.error); }
    else if (d.blocked) { termWrite('deny', d.output); }
    else {
      if (d.warn) termWrite('note', d.warn);
      termWrite('out', d.output);
      termWrite('meta', `${d.pod} · as ${d.username} · ${d.tier}`);
    }
  } catch (e) {
    termWrite('deny', e.message || String(e));
  } finally {
    inp.disabled = false;
    inp.focus();
  }
}

$('cInput').addEventListener('keydown', ev => {
  if (ev.key === 'Enter') { ev.preventDefault(); cliSubmit(); return; }
  if (ev.key === 'ArrowUp') {
    ev.preventDefault();
    if (CLI.hpos > 0) { CLI.hpos--; $('cInput').value = CLI.hist[CLI.hpos]; }
  }
  if (ev.key === 'ArrowDown') {
    ev.preventDefault();
    if (CLI.hpos < CLI.hist.length - 1) { CLI.hpos++; $('cInput').value = CLI.hist[CLI.hpos]; }
    else { CLI.hpos = CLI.hist.length; $('cInput').value = ''; }
  }
});

$('btnCliScan').onclick = scanForCli;
document.querySelector('.tab[data-tab="console"]').addEventListener('click', () => {
  if (!CLI.rel) scanForCli();
});

/* ------------------------------------------------ handover document */

let HDOC = '';
let HFILE = 'redis-handover.md';

$('hGen').onclick = async () => {
  if (!SCUR) return showError('Pick a release first.');
  const ns = $('hClientNs').value.trim();
  $('hGen').disabled = true;
  $('hWarn').innerHTML = '<span class="dim"><span class="spin"></span> reading the release…</span>';
  try {
    const d = await api('/api/handover?namespace=' + encodeURIComponent(SCUR.namespace)
      + '&name=' + encodeURIComponent(SCUR.name)
      + '&client_namespace=' + encodeURIComponent(ns));
    HDOC = d.markdown || '';
    $('hDoc').textContent = HDOC;
    $('hOut').classList.remove('hide');
    $('hWarn').innerHTML = (d.warnings || []).length
      ? '<div class="alert w" style="margin-top:12px"><strong>Before you send this:</strong>'
        + '<ul style="margin:8px 0 0 18px;padding:0">'
        + d.warnings.map(w => `<li style="margin-bottom:6px">${w}</li>`).join('')
        + '</ul></div>'
      : '<div class="alert i" style="margin-top:12px">Nothing to flag.</div>';
    HFILE = d.filename || 'redis-handover.md';
  } catch (e) {
    $('hWarn').innerHTML = `<span class="err">${e.message}</span>`;
  } finally {
    $('hGen').disabled = false;
  }
};

$('hCopy').onclick = () => navigator.clipboard.writeText(HDOC);
$('hDownload').onclick = () => {
  const blob = new Blob([HDOC], { type: 'text/markdown' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = HFILE;
  a.click();
  URL.revokeObjectURL(a.href);
};
