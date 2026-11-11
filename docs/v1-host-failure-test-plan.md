# V1 host failure test and recovery plan — proposed for review

Prepared 2026-11-08. **This is a proposal, not authorization to execute it.** No service was stopped and no failure was injected while preparing it. Recheck every path, service, backup and running workload immediately before a maintenance window; the current V1 deployment is native, while the required Compose deployment is still unfinished. Repeat the applicable drills on the final Compose configuration before using them as release evidence.

## Current host and impact

- `archer-v1.service` is an active user service serving loopback port 8000 from `build/release-v1-tool-defaults-20261108`. The owner reaches it through the existing Tailscale-only route. It has `Restart=on-failure` and supervises an API and worker.
- The active `ollama.service` serves the local model on loopback port 11434. Stopping or killing it can interrupt other clients, not only Archer. Running Docker workloads include `coding-host-egress-proxy`, `open-webui`, and `sglang-router`; their presence is not evidence that they are safe to interrupt.
- `/` and Archer's data and local backup locations are on the same `/dev/nvme0n1p2` filesystem. On inspection it had about 3.1 TiB free. A root-disk failure, destructive storage test, or failed power-loss recovery could affect the operating system, unrelated workloads, the data, local backups and the backup key together. Local backups alone cannot recover a lost disk or machine.
- `archer-v1-backup.timer` is active. Its most recent observed run, 2026-11-08 15:43 PST, succeeded; this is not a substitute for a fresh pre-window backup and restore verification.

## Preconditions before any fault

1. Agree on a maintenance window and rollback owner, confirm local console/physical access, and identify everyone using the machine, Ollama, the GPU and the listed containers. Do not begin while an unrelated workload is active without its owner's agreement. Record service status, deployed image/release identity, model digest, Tailscale-only routing, available disk space and the exact test scope. Avoid recording credentials, real prompts or private content.
2. Quiesce Archer use and approved-file edits. Run the existing backup service, check its actual success and archive receipt, and restore that newly created archive to a **new private temporary destination**, following [backup and restore](backup-restore.md). Verify integrity, owner presence, and revoked sessions without switching the live instance. Confirm the key is usable and protected. Do not treat an old backup or a successful timer alone as this check.
3. For any actual host power interruption or risk to the NVMe device, first arrange and verify a recoverable backup **outside this host and its NVMe**, including whatever is needed for the operating system and other workloads, not just Archer. Store the key securely and separately. The current V1 local-retention decision does not provide this protection. Without such a recovery arrangement, do not run a forced power or root-device failure test; leave that gate open. A normal reboot is not proof of sudden power loss.
4. Prepare a fresh disposable Archer data directory and synthetic owner, credentials, notes and operation IDs. Use the fixed SQLite runtime and a dedicated worker/port; do not run two workers against the production database. Record a bounded set of before/after counts, operation outcomes, run events and journal entries without copying real user content. Prepare the fault harness and exact process identities before scheduling any interruption.
5. Before each phase, confirm health and an abort contact. Abort on failed backup/restore, unexpected live jobs or processes, an unidentified PID/device, failing storage health, insufficient space, loss of console access, or any unplanned effect on another workload. Do not retry uncertain writes as new operations. Capture evidence before cleanup.

## Proposed tests

### 1. Provider outage and recovery

Use a disposable Archer instance against a **separately owned, isolated local model-server instance** if one can be configured without altering the production service or downloading/replacing the model. Establish its exact process identity and real-model inference first. During a synthetic run, terminate only that owned server process, record the run and any confirmed tool operation, restart that isolated server, and submit a distinct synthetic recovery request. Verify a failed or uncertain terminal state rather than fabricated success, preserved committed output and journal, no automatic retry or duplicate write, and success of the new request after recovery. If a dedicated instance cannot be made safe, return for a separate review of the impact and exact procedure for stopping the shared `ollama.service`; do not silently target it. Record whether the fault occurred before or after an actual tool commit.

### 2. GPU workload termination

On the same separately owned inference instance, start a real GPU-backed request, establish the exact owned runner PID and observable GPU association, then terminate that runner alone during generation. Check the terminal run, journal, no duplicate write, model-server behavior, and a new successful inference after recovery. A model-server stop, a client disconnect, or an observed process kill without established GPU association is **not** a GPU-termination result. Do not reset the GPU or kill an unidentified/shared process. If runner ownership or GPU attribution cannot be established, stop and report the gate unverified.

### 3. Storage failure on disposable storage

Prepare a separately backed disposable filesystem for synthetic Archer data, with a fault-injection mechanism that can prove the exact target is the disposable device and can return write errors or exhaust only that filesystem. Design and review the concrete device mapping and cleanup before any privileged command. Never apply a device-mapper fault, fill operation, remount, or filesystem repair to `/dev/nvme0n1p2` or any live production mount. Exercise faults before and after the note transaction commit, restart the disposable worker after removing the fault, and verify SQLite integrity and foreign keys, note/operation/journal counts, uncertain outcomes, and no automatic duplicate write. Restore only the disposable test data if necessary. A bounded disposable-filesystem error is evidence of that error path, not proof of physical NVMe failure.

### 4. Host power loss

Only after the outside-host recovery arrangement in precondition 3 and a separately reviewed, explicit power-interruption procedure: run a synthetic write at a known boundary, interrupt host power in the agreed window, and recover from local console. After boot, inspect filesystem and service health before starting any new writes; compare the synthetic operation, journal, database integrity and recovery state with the pre-fault receipt. Check the other workloads and Tailscale independently. Do not run repair tools blindly on the production filesystem, overwrite the original data, or claim that a graceful shutdown/reboot proves this scenario. A single successful trial cannot prove all commit boundaries or physical storage failure behavior.

## Recovery and return to service

1. Restore power/network only through the agreed procedure. If the host fails to boot or the filesystem reports errors, stop and diagnose from console; preserve the disk and backup rather than attempting automatic destructive repair.
2. Confirm the intended Ollama service and model digest, private-only binding, Archer service and worker readiness, and Tailscale route. Check other affected workloads with their owners. A green process state alone is not a functional test.
3. Inspect every interrupted run and tool operation before any retry. `needs_reconciliation` means the effect may have occurred; inspect the note and journal rather than rerunning it. Record committed versus unknown outcomes and verify there are no duplicates. Check SQLite integrity/foreign keys on a safe copy or with the documented process, and stop on inconsistencies.
4. If Archer data must be restored, stop the instance that will use the restored data. Follow [the clean restore procedure](backup-restore.md) to a new path; do not overwrite the original. Review `RESTORE-REVIEW.json`, reconcile effects since the snapshot, sign in again, renew needed grants and review memory settings. Switch paths only after review and keep the original for investigation. A backup predating a completed action cannot prove that action did not occur.
5. Verify owner login, a new local-inference run, a synthetic note operation, saved-state reconnect, the backup timer, and private access. Then ask the owner to perform the external Tailscale demonstration. Record exact configuration, timestamps, fault boundary, observed outcomes, recovery time, unresolved failures and evidence location; do not label simulated faults as real physical power or disk failure.

## Decision requested

Please review whether to proceed with preparation for the contained provider/GPU and disposable-storage drills, and whether an independently restorable off-host backup for the entire machine can be arranged before considering a physical power-loss test. Approving this plan alone does not authorize an unspecified power cut, GPU reset, root-disk fault, deletion, or interruption of unrelated workloads. The exact harness, targets, maintenance window, backup evidence and recovery commands must be ready for review first.
