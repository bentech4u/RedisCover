# Screenshots

| File | Screen |
|---|---|
| `01-deploy.png` | Deploy tab — the three product cards |
| `02-sizing.png` | Sizing calculator |
| `03-enterprise.png` | Redis Enterprise operator / cluster / database form |
| `04-day2.png` | Day-2 — scale, cache size and resources, grow storage, ACL users |
| `05-console.png` | Console — redis-cli with the mode tiers and refusal banner |
| `06-status.png` | Status — live `INFO`, workloads and pods |

Still worth adding:

| Suggested name | What to capture |
|---|---|
| `07-cluster.png` | Cluster tab — nodes and storage classes, including the warnings |
| `08-test.png` | Test tab — tier groups with safe and disruptive tests selected |
| `09-report.png` | A finished test report with the measured write-outage table |
| `10-uninstall.png` | Uninstall tab showing a deletion plan with PVC reclaim policies |

Tips:

* Use a window around **1400px** wide so tables do not wrap.
* Blank out real hostnames, namespaces and passwords before publishing.
* The generated password appears in plain text on the result panel — crop or
  redact it.
* Recapture `01-deploy.png` whenever a tab is added — it is the only shot that
  shows the tab bar, so it is the one that goes stale.
