import { atom } from 'nanostores'

import { getApiRequestConnection } from '@/api/client'
import { getLocalModelsJobs, installLocalRuntime } from '@/hermes'
import { translateNow } from '@/i18n'
import { $activeGatewayRoute } from '@/store/gateway'
import { $localModelsEnabled } from '@/store/local-models-flag'
import { notify, notifyError } from '@/store/notifications'
import { $connection } from '@/store/session'
import type { LocalRuntimeJob } from '@/types/hermes'

// App-level tracker for local-runtime jobs (runtime installs, model
// downloads). The AUTHORITY is the backend job registry — this store is a
// cache of it (desktop guide: server truth is cached, not owned). Living at
// the store layer, not in the settings pane, is what makes a download
// survive the pane unmounting: anything can start a job, the poller follows
// it to completion, and completion/failure notify app-wide exactly once.

export const $localRuntimeJobs = atom<readonly LocalRuntimeJob[]>([])

export const $localRuntimeInstallStarting = atom(false)

// Shared by the settings button and the campaign CTA. This is request state,
// not invented job progress; the backend registry still owns the actual work.
export function localRuntimeInstallBusy(): boolean {
  return (
    $localRuntimeInstallStarting.get() ||
    $localRuntimeJobs
      .get()
      .some(job => job.status === 'running' && (job.kind === 'runtime-install' || job.kind === 'quickstart'))
  )
}

export async function startLocalRuntimeInstall(): Promise<void> {
  if (localRuntimeInstallBusy()) {
    return
  }

  const owner = activeContext
  owner.postPending = true
  $localRuntimeInstallStarting.set(true)

  try {
    const { job_id } = await installLocalRuntime()
    owner.acceptedIds.add(job_id)
    owner.postPending = false
    owner.readPending = true
    owner.acceptedInstall++

    if (owner !== activeContext) {
      return
    }

    // The pane can mount while this POST resolves. Hold the shared lock
    // through a fresh read, not merely until the request was accepted.
    await polling

    if (owner !== activeContext) {
      return
    }

    await poll()
  } catch (error) {
    if (owner === activeContext) {
      notifyError(error, translateNow('settings.localModels.installFailed'))
    }
  } finally {
    owner.postPending = false

    if (owner === activeContext && !owner.readPending) {
      $localRuntimeInstallStarting.set(false)
    }
  }
}

const POLL_ACTIVE_MS = 700
const POLL_PAUSED_MS = 3_000
let timer: null | number = null
// Jobs we've already toasted for, so a poll race can't double-notify.
const settledNotified = new Set<string>()

// The active set: running OR paused. Paused is NOT settled — it is user
// intent, not failure — and a paused job that later runs again must stay
// tracked so its eventual done/error notifies exactly once.
function isActive(status: LocalRuntimeJob['status']): boolean {
  return status === 'paused' || status === 'running'
}

// A job is the same observation unless ANY user-visible payload changed —
// bytes, percent, phase, detail, error, control flags, and the per-file
// ranges. The backend re-sorts jobs per poll, so compare by id, not index.
function jobEqual(a: LocalRuntimeJob, b: LocalRuntimeJob): boolean {
  return (
    a.job_id === b.job_id &&
    a.status === b.status &&
    a.phase === b.phase &&
    a.detail === b.detail &&
    a.done_bytes === b.done_bytes &&
    a.total_bytes === b.total_bytes &&
    a.percent === b.percent &&
    a.error === b.error &&
    a.can_pause === b.can_pause &&
    a.can_resume === b.can_resume &&
    a.pause_requested === b.pause_requested &&
    rangesEqual(a.ranges, b.ranges)
  )
}

function rangesEqual(
  a: Record<string, [number, number][]> | undefined,
  b: Record<string, [number, number][]> | undefined
): boolean {
  if (a === b) {
    return true
  }

  if (!a || !b) {
    return false
  }

  const keysA = Object.keys(a)
  const keysB = Object.keys(b)

  if (keysA.length !== keysB.length) {
    return false
  }

  return keysA.every(key => {
    const ra = a[key]
    const rb = b[key]

    if (!ra || !rb || ra.length !== rb.length) {
      return false
    }

    return ra.every((range, i) => range[0] === rb[i]?.[0] && range[1] === rb[i]?.[1])
  })
}

function jobsEqual(a: readonly LocalRuntimeJob[], b: readonly LocalRuntimeJob[]) {
  return a.length === b.length && a.every(job => b.some(other => jobEqual(job, other)))
}

function notifySettled(previous: readonly LocalRuntimeJob[], next: readonly LocalRuntimeJob[]) {
  const wasActive = new Set(previous.filter(j => isActive(j.status)).map(j => j.job_id))

  for (const job of next) {
    if (isActive(job.status) || !wasActive.has(job.job_id) || settledNotified.has(job.job_id)) {
      continue
    }

    settledNotified.add(job.job_id)
    acceptedIds.delete(job.job_id)

    if (job.status === 'done') {
      notify({
        durationMs: 6_000,
        kind: 'success',
        title: translateNow('settings.localModels.title'),
        message:
          job.kind === 'model-download'
            ? translateNow('settings.localModels.downloadDoneToast', job.target)
            : job.kind === 'model-activate'
              ? translateNow('settings.localModels.activateDoneToast', job.target)
              : job.kind === 'quickstart'
                ? translateNow('settings.localModels.quickstartDoneToast', job.target)
                : translateNow('settings.localModels.installDoneToast')
      })
    } else {
      notifyError(
        new Error(job.error ?? job.detail ?? 'failed'),
        job.kind === 'model-download'
          ? translateNow('settings.localModels.downloadFailed', job.target)
          : job.kind === 'model-activate'
            ? translateNow('settings.localModels.activateFailed', job.target)
            : job.kind === 'quickstart'
              ? translateNow('settings.localModels.quickstartFailed')
              : translateNow('settings.localModels.installFailed')
      )
    }
  }
}

let inFlight = false
let refreshRequested = false

async function poll(): Promise<void> {
  timer = null
  inFlight = true

  try {
    const { jobs } = await getLocalModelsJobs()
    const previous = $localRuntimeJobs.get()

    if (!jobsEqual(previous, jobs)) {
      notifySettled(previous, jobs)
      $localRuntimeJobs.set(jobs)
    }
  } catch {
    // Preserve the last snapshot while the backend is unreachable.
  } finally {
    inFlight = false
  }

  if (refreshRequested) {
    refreshRequested = false
    void poll()

    return
  }

  const jobs = $localRuntimeJobs.get()
  const anyRunning = jobs.some(job => job.status === 'running')

  if (anyRunning || jobs.some(job => job.status === 'paused')) {
    timer = window.setTimeout(() => void poll(), anyRunning ? POLL_ACTIVE_MS : POLL_PAUSED_MS)
  }

  timer = null
  polling = (async () => {
    try {
      const { jobs } = await getLocalModelsJobs()

      if (context !== generation || accepted !== owner.acceptedInstall) {
        return
      }

      const previous = $localRuntimeJobs.get()

      // Acceptance can arrive after a pane already read the terminal job.
      notifySettled(previous, jobs)

      if (!jobsEqual(previous, jobs)) {
        $localRuntimeJobs.set(jobs)
      }

      owner.jobs = jobs

      if (owner.readPending) {
        owner.readPending = false
        $localRuntimeInstallStarting.set(owner.postPending)
      }
    } catch {
      // Backend unreachable — keep the last snapshot; the next poll retries.
    } finally {
      if (context === generation) {
        polling = null

        if (owner.readPending || $localRuntimeJobs.get().some(j => j.status === 'running')) {
          timer = setTimeout(() => void poll(), POLL_ACTIVE_MS)
        }
      }
    }
  })()

  return polling
}

export function watchLocalRuntimeJobs(): void {
  if (inFlight) {
    refreshRequested = true

    return
  }

  if (timer !== null) {
    window.clearTimeout(timer)
    timer = null
  }

  void poll()
}

// Selector: the in-flight (running or paused) download job for a catalog
// model id, if any. Paused stays visible — the row parks, it doesn't
// vanish (progress loss is information loss).
export function runningDownloadFor(jobs: readonly LocalRuntimeJob[], modelId: string): LocalRuntimeJob | null {
  return jobs.find(j => j.kind === 'model-download' && isActive(j.status) && j.model_id === modelId) ?? null
}

// Selector: every model on its way to the library right now — plain
// downloads plus quickstart runs while they are still fetching bytes
// (later quickstart phases mean the model is staged and activating),
// paused ones included. The model picker renders these as disabled
// progress rows.
const DOWNLOAD_PHASES = new Set([
  'starting',
  'installing-runtime',
  'downloading-runtime',
  'unpacking-runtime',
  'verifying-runtime',
  'downloading'
])

export function runningModelDownloads(jobs: readonly LocalRuntimeJob[]): LocalRuntimeJob[] {
  return jobs.filter(
    j =>
      isActive(j.status) && (j.kind === 'model-download' || (j.kind === 'quickstart' && DOWNLOAD_PHASES.has(j.phase)))
  )
}

export function runningRuntimeInstall(jobs: readonly LocalRuntimeJob[]): LocalRuntimeJob | null {
  return jobs.find(j => j.kind === 'runtime-install' && isActive(j.status)) ?? null
}
