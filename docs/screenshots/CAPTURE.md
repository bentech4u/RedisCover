# Capturing screenshots

The README references these filenames. Drop PNGs in with these exact names and
they appear automatically.

| Filename | What to capture |
|---|---|
| `01-login.png` | The connect screen with the API URL filled in (blank the password field) |
| `02-cluster.png` | Cluster tab — the node table and storage-class table, including the warnings |
| `03-deploy.png` | Deploy tab — the three product cards, with one selected and its form open |
| `04-sizing.png` | Sizing tab after a **Measure** run: the findings, the measurement table and the filled-in calculator |
| `05-test.png` | Test tab — tier groups with a mix of safe and disruptive tests selected |
| `06-report.png` | A finished test report with the measured write-outage table |
| `07-status.png` | Status tab — the live INFO block plus the pods/services/storage tables |
| `08-uninstall.png` | Uninstall tab showing a deletion plan with PVC reclaim policies |

Tips:

* Use a window around **1400px** wide so tables do not wrap.
* Blank out real hostnames, namespaces and passwords before publishing.
* The generated password is shown in plain text on the result panel — crop or
  redact it.
