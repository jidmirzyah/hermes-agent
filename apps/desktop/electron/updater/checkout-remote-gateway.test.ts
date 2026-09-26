import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'

import { afterEach, expect, it, vi } from 'vitest'

import * as updaterProcess from '../updater-process'

import { type CheckoutStrategyDeps, createCheckoutStrategy } from './checkout'
import type { SourceUpdate } from './checkout-source'

const IS_WINDOWS: boolean = process.platform === 'win32'
const NO_GATEWAY_FLAG: string = IS_WINDOWS ? '-NoGateway' : '--no-gateway'

afterEach((): void => {
  vi.restoreAllMocks()
})

// One gateway per host (#117529): a Desktop served by a remote gateway must
// tell the hand-off script not to (re)start a local one, and a locally-owned
// Desktop must keep the default so its gateway comes back after the update.
it.each([true, false])('hand-off passes the no-gateway flag iff a remote gateway serves the app: %s', async (remote: boolean): Promise<void> => {
  const root: string = fs.mkdtempSync(path.join(os.tmpdir(), 'checkout-remote-gateway-'))
  const home: string = path.join(root, 'profile')
  const scriptDirectory: string = path.join(root, 'scripts', 'desktop-update')
  fs.mkdirSync(home)
  fs.mkdirSync(scriptDirectory, { recursive: true })
  fs.writeFileSync(path.join(scriptDirectory, IS_WINDOWS ? 'windows.ps1' : 'posix.sh'), '')
  fs.writeFileSync(path.join(scriptDirectory, 'runtime.ps1'), '')
  fs.mkdirSync(path.join(root, '.hermes', 'bin'), { recursive: true })
  fs.writeFileSync(path.join(root, '.hermes', 'bin', 'hermes.exe'), '')

  const status: SourceUpdate = { supported: true, branch: 'main', targetSha: 'a'.repeat(40), updateAvailable: true }

  const deps: CheckoutStrategyDeps = {
    readSourceUpdate: async (): Promise<SourceUpdate> => status,
    hermesHome: home,
    isWindows: IS_WINDOWS,
    isMac: process.platform === 'darwin',
    defaultUpdateBranch: 'main',
    updateHandoffDwellMs: 0,
    resolveUpdateRoot: (): string => root,
    resolveUpdaterBinary: (): null => null,
    remoteGatewayActive: (): boolean => remote,
    emitUpdateProgress: vi.fn(),
    rememberLog: vi.fn(),
    startHermes: async (): Promise<void> => {},
    stopBackendsForUpdate: async (): Promise<void> => {},
    repairMacUpdaterHelper: (): void => {},
    preflightStateDb: (): void => {},
    runningAppBundle: (): null => null,
    markQuittingForHandoff: (): void => {},
    quit: (): void => {}
  }

  const spawned: string[][] = []
  vi.spyOn(updaterProcess, 'spawnUpdaterProcess').mockImplementation(
    (_command: string, args: string[]): updaterProcess.UpdaterChild => {
      spawned.push(args)

      return { unref: (): void => {} }
    }
  )

  try {
    expect(await createCheckoutStrategy(deps).apply()).toMatchObject({ ok: true, handedOff: true })
    expect(spawned).toHaveLength(1)
    const args: string[] = spawned[0]!
    expect(args).toContain(IS_WINDOWS ? '-Branch' : '--branch')

    if (remote) {
      expect(args).toContain(NO_GATEWAY_FLAG)
    } else {
      expect(args).not.toContain(NO_GATEWAY_FLAG)
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})
