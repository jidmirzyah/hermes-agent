// The caller owns OLD's lifetime. Record backend provenance in the same receipt
// as the shared chat assertion without launching a second app or provider.
import fs from 'node:fs';
import path from 'node:path';
import { readChatIdentity, runDesktopChatSmoke, waitForChatReady } from '../../../tests-js/scripts/desktop-chat-smoke.ts';
import { assertBackendOrigin, localBackendProcess, readInstallationCommit } from '../../../tests-js/scripts/desktop-smoke-process.ts';

/**
 * @param {import('@playwright/test').ElectronApplication} app
 * @param {import('@playwright/test').Page} page
 * @param {{mockUrl: string, outDir: string, expectCommit: string,
 *   origin: 'source'|'bundled', root: string, executable: string}} options
 */
export async function runUpdateWindowChat(app, page, options) {
  const receiptPath = path.join(options.outDir, 'desktop-chat-old.json');
  fs.mkdirSync(options.outDir, { recursive: true });
  try {
    const running = await app.evaluate(({ app: electronApp }) => ({
      pid: process.pid, executable: process.execPath, resources: process.resourcesPath,
      userData: electronApp.getPath('userData'),
    }));
    const { userData } = running;
    if (!process.env.HERMES_DESKTOP_USER_DATA_DIR || fs.realpathSync(userData) !== fs.realpathSync(process.env.HERMES_DESKTOP_USER_DATA_DIR)) {
      throw new Error('OLD update window did not honor isolated userData');
    }
    if (fs.realpathSync(running.executable) !== fs.realpathSync(options.executable)) {
      throw new Error('OLD update window executable differs from the installed app');
    }
    if (options.origin === 'bundled' && fs.realpathSync(path.join(running.resources, 'agent-payload')) !== fs.realpathSync(options.root)) {
      throw new Error('OLD update window resources differ from the installed payload');
    }
    await waitForChatReady(page);
    const identity = await readChatIdentity(page);
    const connection = await page.evaluate(() => window.hermesDesktop.getConnection());
    const base = new URL(connection.baseUrl);
    if (connection.mode !== 'local' || !['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname)) {
      throw new Error('OLD update-window chat did not use the local installed backend');
    }
    if (options.origin === 'source' && fs.realpathSync(identity.hermesRoot) !== fs.realpathSync(options.root)) {
      throw new Error('OLD update-window chat resolved another source installation');
    }
    const backend = localBackendProcess(Number(base.port), running.pid);
    assertBackendOrigin(backend, options.root, options.origin);
    const provenanceCommit = readInstallationCommit(options.root, options.origin);
    if (provenanceCommit !== options.expectCommit) throw new Error('Installed OLD commit differs from the expected commit');
    const chat = await runDesktopChatSmoke(page, { ...options, phase: 'old', provenanceCommit });
    const result = { ...chat, origin: options.origin, root: options.root,
      executable: options.executable, appPid: running.pid,
      backend: { pid: backend.pid, parentPid: backend.parentPid, executable: backend.executable },
      localModeConfigured: true, userData,
      launchKind: 'pre-update-window' };
    fs.writeFileSync(receiptPath, JSON.stringify(result, null, 2) + '\n');
  } catch (error) {
    await page.screenshot({ path: path.join(options.outDir, 'desktop-chat-old-failed.png') }).catch(() => {});
    fs.writeFileSync(receiptPath, JSON.stringify({ status: 'failed', phase: 'old',
      expectedCommit: options.expectCommit, origin: options.origin, root: options.root,
      error: String(error) }, null, 2) + '\n');
    throw error;
  }
}