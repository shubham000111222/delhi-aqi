# Signed incident-tracking acknowledgement

This is a **narrow, fail-closed recovery tool**, not trade authorization or a live-readiness
certificate. It accepts an offline Ed25519 signature from a provisioned public key for one
short-lived acknowledgement, bound to a database identity, mode and exact kill-state snapshot.
The database consumes each request once, transactionally with the acknowledgement audit and
kill-state update. A signature proves possession of a key, not who held it.

Live/testnet manual and automatic resets are currently blocked pending authenticated reset and independent recovery evidence. The action **does not reset the kill switch**, prove positions flat, rebuild missing incidents,
or authorize a ramp increase. It records that incident history is unverified and sets a separate
durable hold. Entries and reset remain blocked while that hold exists. There is **no implemented
authenticated reconciliation/hold-clearing workflow** yet; do not delete the hold or edit the
SQLite database to bypass it. Normal pending incident outbox records should instead replay on
kill-switch initialization; this signed action is for the rarer `incident_tracking_failed` latch
where the outbox itself could not be written.

## Trust and limitations

- Provision `operator-keys.json` in the **parent of the selected config directory**. Its trust
  bundle contains public keys only. The service reads this fixed path; a missing, malformed or
  untrusted key fails closed. The project does not verify Windows ACLs or authenticate individual
  people. Use separate OS identities and ACLs so the trading process cannot modify its own trust
  bundle, and keep the encrypted private key and passphrase on a separate offline operator device.
  If the trading process can edit the key bundle, code or database, this is **not** a security
  boundary against that process. Apply the same protection to the executable and config.
- Backups or clones of a database share its embedded identity; replay protection is local to one
  database, not a distributed consensus system. Do not run multiple independent writable clones.
- Requests expire after at most 15 minutes and are rejected if the clock is wrong, the state or
  mode changes, the signer is removed, or the request is already consumed. The key file and SQLite
  cannot be locked in a single transaction, so revocation concurrent with an apply is not a
  distributed atomic operation. Audit records include the public-key fingerprint and reason;
  never put secrets in the reason.
- Existing `Approvals` records remain unverified and cannot authorize this action or trading.
  This one signature path does not implement general action-bound approvals for allocation,
  operator authentication on CLI/Telegram/dashboard resets, venue reconciliation or global
  autonomy enforcement.

## Windows 11 / PowerShell: controlled paper-only procedure

Use a separate offline operator device for the first two steps. Install the project dependencies
there from a trusted source. Do **not** paste private keys, passphrases or credentials into chat.
Paths below are examples: adjust them to your own secure operator device and paper installation.

1. On the operator device, create an encrypted private key **outside the repository** and a public
   bundle. Both target paths must not already exist. The tool prompts for a passphrase twice; it
   never prints the private key. On Windows, set and verify restrictive ACLs yourself (POSIX file
   permissions alone are not Windows ACLs).

   ```powershell
   New-Item -ItemType Directory -Force C:\OperatorVault | Out-Null
   .\.venv\Scripts\python.exe tools\generate_operator_key.py --private-key C:\OperatorVault\operator.pem --public-bundle C:\OperatorVault\operator-keys.json
   ```

   Record the printed public fingerprint. Transfer **only** `operator-keys.json` to the paper
   machine, verify the fingerprint out of band and place it in the parent of its selected config
   folder (for `C:\Users\shubh\Downloads\trad\delhi-aqi\config`, that is the repository root).
   Protect the public bundle from modification by the trading account. The root bundle is ignored
   by Git; still check `git status` and never commit secrets. A provisioning mechanism and ACL
   review are operator responsibilities.

2. On the paper machine, only if an actual tracking fault exists, create a request using the
   public fingerprint and a non-secret review reason. The paths under `$env:TEMP` are examples.
   PowerShell 5 UTF-8 BOMs are accepted.

   ```powershell
   $KeyId = 'PASTE_PUBLIC_FINGERPRINT_HERE'
   tradebot recovery-request --mode paper --key-id $KeyId --reason 'Reviewed incident tracking failure' | Set-Content -Encoding UTF8 "$env:TEMP\recovery-request.json"
   ```

   Check the command succeeded and inspect the JSON. Transfer the request file to the offline
   operator device. Do not sign an unexpected action, database, mode, complete kill-state snapshot, state hash, reason or expiry. The snapshot may contain operator-entered reasons; handle the request accordingly. Older requests without a snapshot are incompatible and must be regenerated.

3. On the operator device, review the printed request, type `SIGN` only after review, and enter
   the private-key passphrase at the prompt. The signature output path must not already exist.

   ```powershell
   .\.venv\Scripts\python.exe tools\sign_recovery.py --private-key C:\OperatorVault\operator.pem --request C:\OperatorVault\recovery-request.json --signature C:\OperatorVault\recovery.sig
   ```

   Transfer the public signature and the unchanged request back to the paper machine. The private
   key and passphrase stay offline. If the request expires or the state changes, create a new one.

4. On the paper machine, apply it. A successful message says the kill level is unchanged and
   incident history remains unverified. The separate hold still prevents entries and reset.

   ```powershell
   tradebot recovery-apply --mode paper --request "$env:TEMP\recovery-request.json" --signature "$env:TEMP\recovery.sig"
   tradebot status --mode paper
   ```

Do not run these steps for real money as a substitute for independent venue-state checks,
authenticated reset, incident reconciliation or the other unfinished go/no-go requirements.

## Signed live allocation-cap reduction

The same offline key and `tools/sign_recovery.py` can sign the distinct
`reduce_live_allocation_cap` action. This is a **reduction only**, not live authorization,
strategy promotion, a kill reset, or evidence of safe trading. It changes the persisted
operator cap used by the normal live `RampController.multiplier()`; the effective multiplier
is the minimum of that cap, the configured initial multiplier, and 0.10. For example,
`0.03` caps the ramp multiplier at 3%, not 3% of account equity. A configured stricter
multiplier still wins. The displayed `previous_cap` is the stored operator cap (initially
0.10), not necessarily the smaller effective multiplier. No signed cap-increase/clear
workflow exists; do not apply a reduction unless it is intended to persist.

With an already provisioned key and operator process as above, the request/apply commands
below target the **live-mode database**, even if no exchange is connected. Use an isolated
configuration and data directory for practice; do not use a real trading database for a drill.
The private key remains on the offline machine. Transfer only the request and signature.

```powershell
tradebot allocation-request --mode live --key-id $KeyId --reason 'Reduce allocation cap' --cap '0.03' | Set-Content -Encoding UTF8 "$env:TEMP\allocation-request.json"
# On the offline operator machine, after secure transfer and review:
python tools/sign_recovery.py --private-key 'C:\secure\operator-key.pem' --request "$env:TEMP\allocation-request.json" --signature "$env:TEMP\allocation.sig"
# Transfer the signature back, then on the trading machine:
tradebot allocation-apply --mode live --request "$env:TEMP\allocation-request.json" --signature "$env:TEMP\allocation.sig"
```

The signature binds the database ID, mode, action, old and new operator caps, key, reason,
nonce and 15-minute maximum expiry. Consumption, cap update and audit are transactional;
concurrent/stale/replayed requests cannot apply. Key possession is not human identity:
the public trust bundle, database and process need separate OS protection. The cap is not
an exchange-side order limit: raw SQL, direct exchange/custom adapter calls, alternate sizing
paths, misconfigured processes and orders already sized or in flight can bypass it. Real
venue behavior has not been tested. An invalid cap blocks normal live sizing. Live ramp
increases and live/testnet kill resets remain blocked.
