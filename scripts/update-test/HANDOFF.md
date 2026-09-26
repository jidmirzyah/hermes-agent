# testing the bundles branch against your normal hermes install

instructions:

1. close hermes, the gateway, etc. make sure you have no running hermes processes.

2. apply my updater override:
   macos/linux:
   `curl -fsSL "https://raw.githubusercontent.com/REPO/main/scripts/update-test/hermes-update-rehearsal.sh?t=$(date +%s)" | bash -s -- pre`

   windows (open PowerShell with **Run as Administrator** — the backup takes a disk snapshot, which needs admin):
   `& ([scriptblock]::Create((irm "https://raw.githubusercontent.com/REPO/main/scripts/update-test/hermes-update-rehearsal.ps1" -Headers @{"Cache-Control"="no-cache"}))) pre`

   this backs up your entire hermes home and any desktop app settings. _from this point on, nothing you do in hermes will be preserved, until you restore your backup at the end._

3. boot hermes up to ensure everything is working, still. if you normally have a background service, gateway, etc, make sure it's running.

4. update hermes like you normally do.

5. test hermes. make sure nothing breaks, everything you use still works, etc.

6. close hermes, the gateway, etc. make sure you have no running hermes processes.

7. restore your backup:

   macos/linux:
   `curl -fsSL "https://raw.githubusercontent.com/REPO/main/scripts/update-test/hermes-update-rehearsal.sh?t=$(date +%s)" | bash -s -- post --yes`

   windows (again as **Administrator**):
   `& ([scriptblock]::Create((irm "https://raw.githubusercontent.com/REPO/main/scripts/update-test/hermes-update-rehearsal.ps1" -Headers @{"Cache-Control"="no-cache"}))) post -Yes`

   this puts hermes back to exactly how it was beforehand.

# testing the bundles branch from a fresh install

macos/linux, in a terminal

```bash
curl -fsSL https://raw.githubusercontent.com/REPO/main/scripts/install.sh | HERMES_REPO_URL=https://github.com/REPO.git bash
```

windows, in powershell

```powershell
$env:HERMES_REPO_URL = "https://github.com/REPO.git"
& ([scriptblock]::Create((irm "https://raw.githubusercontent.com/REPO/main/scripts/install.ps1" -Headers @{"Cache-Control"="no-cache"}))) -HermesHome C:\bench\fresh\home -NonInteractive
```
