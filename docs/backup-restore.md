# Backing up the private `data/` submodule

The `data/` submodule is the one part of this project that is **irreplaceable**.
The code is public and regenerable; `data/cache/` is regenerable. But
`data/journal.json` (your decision log), the target-model **provenance** and
`backups/`, `provenance-log.jsonl`, and `research/` are the ledger of *why you did
what you did*. The process-attribution and tax-calendar features make that
history load-bearing — losing it would be the most expensive bug this project
could have. It currently lives on this machine and (at most) one git remote. That
is not a backup.

This is a 3-2-1 problem: **3** copies, on **2** kinds of media, **1** off-site.
The git remote is one off-site copy but it is (a) a single point of failure and
(b) private-repo access that can lapse. An independent, encrypted, restorable
archive closes the gap.

## Tooling

`tools/backup-data.ps1` makes an **AES-256 encrypted** `.7z` of `data/` (using
7-Zip with `-mhe=on`, which encrypts file *contents and names*), and can
verify/restore one. It excludes `data/cache/` (regenerable + live session auth).
Secrets (`secrets.env`, tokens) never live under `data/`, so they are out of scope
by construction.

Prerequisite: 7-Zip on `PATH` (`scoop install 7zip` or `winget install
7zip.7zip`). Prefer `age`/`gpg`? See the alternatives below.

## Commands

```powershell
# 1. Make an encrypted backup AND immediately drill the restore (recommended).
#    Passphrase from -Passphrase, else $env:ASSAY_BACKUP_PASSPHRASE, else prompt.
pwsh tools/backup-data.ps1 -Verify

# 2. Choose where it lands (default: $env:ASSAY_BACKUP_DIR or ~\assay-backups).
pwsh tools/backup-data.ps1 -Dest D:\backups\assay -Verify

# 3. Restore-drill an existing archive (extract to a temp dir, check it parses).
pwsh tools/backup-data.ps1 -Archive D:\backups\assay\assay-data-<ts>.7z

# 4. Actually restore an archive somewhere (never overwrites the live repo).
pwsh tools/backup-data.ps1 -Archive <path>.7z -RestoreTo C:\tmp\assay-restore

# 5. Prove the whole mechanism with a synthetic dataset (no real data, CI-safe).
pwsh tools/backup-data.ps1 -SelfTest
```

The passphrase goes in your **password manager**, never in the repo. If you lose
it, the backup is confetti — that is the point of encryption, but it cuts both
ways.

## The restore drill (do this, don't just intend to)

A backup you have never restored is a hypothesis. Run one of:

- `-SelfTest` — proves the create → verify → restore → wrong-passphrase-rejection
  path against a throwaway dataset. Runs anywhere, including CI and a
  code-only checkout where the `data/` submodule isn't initialized.
- `-Verify` / `-Archive <path>` — the real drill against your actual `data/`: it
  tests archive integrity, extracts to a temp dir, and confirms
  `target-model.json` / `journal.json` decrypt and parse. Requires the submodule
  to be checked out (`git submodule update --init`).

Do the real drill on the machine that has the submodule, at least once, and again
whenever you change the backup tooling or destination.

## Suggested cadence

- After any session that writes journal entries, commits a target-model change,
  or saves research → `-Verify` backup.
- Keep the last N archives; copy at least one **off-box** (cloud sync folder, an
  encrypted USB stick, or an object store). The `.7z` is already encrypted, so a
  plain cloud folder is acceptable.

## Alternatives (if you prefer age or gpg)

The archive format is not sacred; any authenticated-encryption tool works. Tar
the tree (minus cache) and encrypt:

```powershell
# age (modern, simple; install: scoop install age)
tar --exclude=cache -C data -cf - . | age -p -o assay-data.tar.age
age -d assay-data.tar.age | tar -C restore-dir -xf -

# gpg (symmetric)
tar --exclude=cache -C data -cf - . | gpg -c -o assay-data.tar.gpg
gpg -d assay-data.tar.gpg | tar -C restore-dir -xf -
```

Whichever you pick, the discipline is the same: **encrypted, off-site, and
actually restored once.**
