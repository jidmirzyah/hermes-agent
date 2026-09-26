import type { ToolCallMessagePartProps } from '@assistant-ui/react'
import type { ConnectionTargetState, ConnectorsConnectResult } from '@hermes/shared'
import { useStore } from '@nanostores/react'
import { useEffect, useMemo, useRef, useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { sessionRoute } from '@/app/routes'
import { ToolFallback } from '@/components/assistant-ui/tool/fallback'
import { Button } from '@/components/ui/button'
import { ConnectorCard, ConnectorRow, type ConnectorRowMark, ConnectorSummary } from '@/components/ui/connector-card'
import { useI18n } from '@/i18n'
import { connectionRows, connectorCalls, connectorTitle, connectorToolName, recordOf } from '@/lib/connector-tools'
import { cn } from '@/lib/utils'
import { createConnectorFlow } from '@/store/connector-flow'
import { requestGatewayForAgent } from '@/store/gateway'
import { notifyError } from '@/store/notifications'

export function ConnectorTool(props: ToolCallMessagePartProps) {
  const view = useSessionView()
  const runtimeId = useStore(view.$runtimeId)
  const storedId = useStore(view.$storedId)
  const messages = useStore(view.$messages)

  // One live card per offer. Every manage_connections call renders through
  // here, but only ONE is the card the user acts on; the rest are settled
  // tool rows. Which one: consecutive calls naming the same apps are one
  // exchange — connect, the wait the agent parks in while the user signs in,
  // the status it runs when the connection lands — and the FIRST of the last
  // exchange is the card. The newest would demote the card mid-authorization
  // into a row and mint a fresh one below it. A catalog listing (status with
  // nothing named) after a targeted ask never starts an exchange: it is a
  // read, not an offer.
  const offers = messages
    .flatMap(message => message.parts)
    .filter(
      part =>
        part.type === 'tool-call' &&
        (part.toolName === 'manage_connections' || connectorCalls(part.toolName, part.args).length > 0)
    )

  const keyOf = (part: (typeof offers)[number]) =>
    part.type === 'tool-call'
      ? connectionRows(part.args, part.result)
          .map(row => row.connector)
          .sort()
          .join('|')
      : ''

  const targeted = (part: (typeof offers)[number]) => {
    if (part.type !== 'tool-call') {
      return false
    }

    const asked = recordOf(part.args).connectors

    return Array.isArray(asked) && asked.length > 0
  }

  let liveId: string | undefined
  let liveKey: string | null = null
  let sawTargeted = false

  for (const part of offers) {
    if (part.type !== 'tool-call') {
      continue
    }

    const key = keyOf(part)

    if (sawTargeted && !targeted(part)) {
      continue
    }

    sawTargeted ||= targeted(part)

    if (key !== liveKey) {
      liveKey = key
      liveId = part.toolCallId
    }
  }

  const historical = liveId !== props.toolCallId

  const [owner, setOwner] = useState<{
    storedId: string
    runtimeId: string
    connectionId: null | string
    profile: string
  } | null>(null)

  useEffect(() => {
    if (!sessionId || !active) {
      setOwner(null)

      return
    }

    let cancelled = false

    void connectionOwnerFor(sessionId, 'connectors.connect').then(resolved => {
      if (!cancelled) {
        setOwner(resolved)
      }
    })

    return () => {
      cancelled = true
    }
  }, [storedId, runtimeId, historical])
  const rows = connectionRows(props.args, props.result)
  const signature = rows.map(row => row.connector).join('|')
  const target = view.kind === 'tile' ? `tile:${storedId}` : 'main'

  // The TUI shape, stolen: the agent parks inside manage_connections
  // action="wait", which blocks the turn and polls the gateway, instead of
  // deciding what "not connected" means and building around the app. Each
  // card action sends one hidden line so the agent takes the right next call.
  // Read through a ref so the flow (memoised on identity) always nudges the
  // live composer target, never the one it was built with. Busy is the
  // composer's problem: a hidden request mid-turn steers or queues there.
  const nudgeRef = useRef((_text: string) => {})

  nudgeRef.current = (text: string) => {
    requestComposerSubmit(`[connectors] ${text}`, { displayKind: 'hidden', target })
  }

  return owner
}

/** The browser leg of a connection came back through `hermes://connections/done`. Show the session
 *  that opened the operation and tell its backend to read the account now instead of at its next
 *  tick. Nothing in the link is trusted to move a row: the op id only names which card to show, and
 *  the backend reads the account itself. An operation this window holds no card for, or one that
 *  already settled, is ignored: the tab can come back long after Continue, and a stale link must
 *  not pull the user away from where they are. */
export async function openConnectionDoneLink(
  op: string,
  navigate: (to: string) => void,
  storedSessionIdFor: (runtimeSessionId: string) => string
): Promise<void> {
  const request = Object.values($connectionRequests.get()).find(entry => entry.opId === op)

  if (!request?.sessionId || request.settled) {
    return
  }

  const storedId = storedSessionIdFor(request.sessionId)
  navigate(sessionRoute(storedId))

  const owner = await connectionOwnerFor(storedId, 'connectors.operation.wake')

        await window.hermesDesktop.openExternal(url)
      },
      onWaiting: slug =>
        nudgeRef.current(
          `The user clicked Connect for ${connectorTitle(slug)} and the sign-in is open in their browser. Call manage_connections action="wait" connectors=["${slug}"] now and hold there until it reports connected. Do NOT call connect again — a second link cancels the one they are signing in with. Say nothing until wait returns.`
        )
    })
  } catch {
    // The wake only shortens the wait. The operation can settle and leave the live registry between
    // the link and this RPC (4004); the watcher reads the account at its next tick regardless.
  }
}

/** Try again for one target of the open operation: one RPC, and the fresh link when the backend
 *  minted one. The backend re-mints only what is actually dead. A settled operation is dead: the
 *  RPC would open a second one that no card on this row can answer. */
export async function reissueConnectionTarget(
  owner: ConnectionOwner,
  request: ConnectionRequest,
  name: string
): Promise<null | string> {
  if (!connectionRequestOpen(request)) {
    return null
  }

  const reply = await requestGatewayForAgent<ConnectorsConnectResult>(
    owner.connectionId,
    owner.profile,
    'connectors.connect',
    {
      connectors: [name],
      owner: { session_id: request.sessionId, type: 'session' },
      reconnect: true
    },
    45000
  )

  const minted = reply.targets.find(target => target.name === name)

  return connectorAuthorizationUrl(minted?.connect_url)
}

/** Names requested by a manage_connections part, including an event-projected row. */
function requestedConnectorNames(args: ToolCallMessagePartProps['args']): string[] {
  const connectors = recordOf(args).connectors
  const entries = Array.isArray(connectors) ? connectors : [connectors]

  return entries.flatMap(entry => {
    const row = recordOf(entry)
    const name = connectorText(entry) ?? connectorText(row.name) ?? connectorText(row.connector)
    const trimmed = name?.trim()

    return trimmed ? [trimmed] : []
  })
}

/** The card lives on the tool row whose id opened the operation and on no other. */
export function connectionRequestOwnsPart(props: ToolCallMessagePartProps, request: ConnectionRequest | null): boolean {
  return Boolean(request && props.toolCallId === request.toolCallId)
}

export function ConnectorTool(props: ToolCallMessagePartProps) {
  const view = useSessionView()
  const runtimeId = useStore(view.$runtimeId)
  const storedId = useStore(view.$storedId)
  const $request = useMemo(() => sessionConnectionRequest(runtimeId), [runtimeId])
  const request = useStore($request)
  const targetNames = requestedConnectorNames(props.args)

  const untargetedStatus =
    props.toolName === 'manage_connections' &&
    (recordOf(props.args).action ?? 'status') === 'status' &&
    targetNames.length === 0

  const live = !untargetedStatus && connectionRequestOwnsPart(props, request)
  // Owner routes and hints are keyed by the stored id, not the runtime id the events carry.
  const owner = useConnectionOwner(storedId, live)

  if (!live || !request) {
    return <ToolFallback {...props} />
  }

  return owner ? <ConnectorOffer owner={owner} request={request} /> : null
}

  return (
    <ConnectorOffer
      flow={flow}
      key={`${runtimeId}:${signature}`}
      onSkipped={slug =>
        nudgeRef.current(
          `The user chose Not now for ${connectorTitle(slug)}. Do not connect it, do not route around it with another client, credential or CLI for the same app. Continue the task without it, or ask what they want to do.`
        )
      }
    />
  )

  ;(row?.querySelector<HTMLElement>(FOCUSABLE_IN_ROW) ?? row)?.focus()
}

/** Move focus to the row the backend changed. Only while the card already holds focus, and never
 *  out of a field the user is typing in — a transition the user is not looking at must not take
 *  the keyboard away from wherever they are. */
export function useConnectorFocusHandoff(
  targets: readonly ConnectionTarget[],
  cardRef: RefObject<HTMLDivElement | null>
): void {
  const seen = useRef<Map<string, ConnectionTargetState> | null>(null)
  const states = targets.map(target => `${target.name}=${target.state}`).join('|')

  // The ref holds what the last frame said, for comparison only: nothing renders from it, so it
  // cannot lag a render the way a mirrored atom would.
  // eslint-disable-next-line no-restricted-syntax
  useEffect(() => {
    const previous = seen.current
    seen.current = new Map(targets.map(target => [target.name, target.state]))

    const card = cardRef.current

    const moved = targets.find(target => {
      const before = previous?.get(target.name)

      return before !== undefined && before !== target.state
    })

    const active = document.activeElement

    if (!previous || !moved || !card?.contains(active) || active?.matches(EDITABLE)) {
      return
    }

    focusChangedRow(card, moved.name)
    // The target states are the whole input; `states` changes exactly when one of them moves.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [states])
}

export const MARK_LABEL = {
  connected: (copy: ConnectorCopy) => copy.connected,
  idle: (copy: ConnectorCopy) => copy.notConnected,
  waiting: (copy: ConnectorCopy) => copy.waiting
} satisfies Record<ConnectorRowMark, (copy: ConnectorCopy) => string>

interface ConnectorOfferProps {
  flow: ReturnType<typeof createConnectorFlow>
  /** The user waved the app off. */
  onSkipped: (slug: string) => void
}

export function ConnectorOffer({ flow, onSkipped }: ConnectorOfferProps) {
  const state = useStore(flow.state)
  const { t } = useI18n()
  const copy = t.connectors
  const [query, setQuery] = useState('')
  const active = state.rows.some(row => row.phase === 'opening' || row.phase === 'waiting')

  const cardCopy: ConnectorCardCopy = {
    connectAction: copy.connect,
    decline: copy.skip,
    envRequired: '',
    grantAction: copy.grant,
    retryAction: copy.retry,
    stateConnected: copy.connected,
    stateDeclined: copy.skipped,
    stateDisabled: copy.disabled,
    stateFailed: copy.failed,
    stateNeedsAuth: copy.needsAuth,
    toolCount: count => String(count),
    trustCommunity: '',
    trustCommunityTip: () => '',
    trustVerified: () => '',
    trustVerifiedTip: () => ''
  }

        return next
      })
    }
  }

  const rows = state.rows.filter(row => connectorTitle(row.connector).toLowerCase().includes(query.toLowerCase()))
  // A targeted ask ("connect Gmail") is one or two cards, each already a
  // complete question. A heading, a disclaimer and a refresh control over
  // them is a settings panel dropped into the chat. Only a real catalog — the
  // model asked for status with nothing named — earns the chrome.
  const catalog = state.rows.length > 4

  return (
    <div className="my-2 grid min-w-0 max-w-lg gap-1" data-connector-offer>
      {catalog ? (
        <div className="grid gap-0.5 px-1">
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm font-medium">{copy.title}</span>
            <Button onClick={() => void flow.refresh()} size="xs" variant="text">
              {copy.refresh}
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">{copy.disclaimer}</p>
        </div>
      ) : null}
      {state.error ? (
        <p className="flex flex-wrap items-center gap-2 px-1 text-xs text-destructive" role="alert">
          {copy.statusError}
          <Button onClick={() => void flow.refresh()} size="xs" variant="text">
            {copy.retry}
          </Button>
        </p>
      ) : null}
      {!state.available && !state.error ? <p className="px-1 text-xs text-muted-foreground">{copy.unavailable}</p> : null}
      {catalog ? <SearchField onChange={setQuery} placeholder={copy.search} value={query} /> : null}
      <div className={cn('grid min-w-0', catalog && 'max-h-96 overflow-y-auto')}>
        {rows.map(row => (
          <div className="grid" key={row.connector}>
            <ConnectorCard
              actionDisabled={!state.available || row.enabled === false || !!state.error}
              collapseWhenSettled={false}
              connector={{
                name: row.connector,
                title: row.name || connectorTitle(row.connector),
                description: row.description || copy.describe(row.name || connectorTitle(row.connector))
              }}
              copy={{
                ...cardCopy,
                connectTitle: copy.connectTitle,
                decline: row.phase === 'opening' || row.phase === 'waiting' ? copy.cancel : copy.skip,
                connectAction: ['expired', 'revoked'].includes(row.connectionStatus ?? '') ? copy.grant : copy.connect
              }}
              dismissed={row.phase === 'skipped'}
              onConnect={() => void flow.connect(row.connector)}
              onDismiss={() => {
                const wasPending = ['opening', 'waiting'].includes(row.phase)
                flow.skip(row.connector)

                // A cancel mid-authorization is not a skip: the agent may be
                // parked in wait and will hear the timeout itself.
                if (!wasPending) {
                  onSkipped(row.connector)
                }
              }}
              otherBusy={active && !['opening', 'waiting'].includes(row.phase)}
              outcome={
                row.phase === 'connected'
                  ? { status: 'connected' }
                  : row.phase === 'error'
                    ? {
                        status: 'error',
                        detail:
                          row.error === 'connect'
                            ? copy.connectError
                            : row.error === 'unavailable'
                              ? copy.unavailable
                              : copy.statusError
                      }
                    : undefined
              }
              phase={row.phase === 'opening' ? copy.opening : row.phase === 'waiting' ? copy.waiting : undefined}
              state={
                row.enabled === false
                  ? 'disabled'
                  : ['expired', 'revoked'].includes(row.connectionStatus ?? '')
                    ? 'needs_auth'
                    : 'not_configured'
              }
              variant="avatar"
            />
            {row.phase === 'timeout' ? (
              <div className="flex flex-wrap items-center gap-2 px-3.5 text-xs text-muted-foreground">
                <span>{copy.timeout}</span>
                <Button onClick={() => void flow.keepWaiting(row.connector)} size="xs" variant="textStrong">
                  {copy.keepWaiting}
                </Button>
              </div>
            ) : null}
          </div>
        ))}
        {!rows.length && state.available ? <p className="px-1 text-xs text-muted-foreground">{copy.empty}</p> : null}
      </div>
    </div>
  )
}

const MISSING_CALL_RESULT = { error: 'No result for this call.' }

/** Keep execution output in the standard disclosure, with one row per inner call.
 *  The gateway labels every call the tool_search bridge runs — hosted, MCP or local —
 *  so hosted-only, MCP-only and mixed batches all render the same way. */
export function ConnectorExecution(props: ToolCallMessagePartProps) {
  const labels = toolLabels(props.args)
  const output = recordOf(props.result)
  const results = Array.isArray(output.results) ? output.results : []

  if (labels.length === 0) {
    return <ToolFallback {...props} />
  }

  const input = recordOf(props.args)
  const batch = Array.isArray(input.calls) ? input.calls : [input]

  return (
    <>
      {labels.map((label, index) => {
        // A hosted batch answers one result per call; anything else answers once for the
        // whole call, and every row shows that same outcome (a rejected batch, an error).
        // A batch that answered short says so on the rows it left out.
        const item = results[index] ?? (results.length > 0 ? MISSING_CALL_RESULT : props.result)
        const result = recordOf(item)

        return (
          <ToolFallback
            {...props}
            args={recordOf(recordOf(batch[index]).arguments ?? props.args)}
            isError={Boolean(result.error) || props.isError === true}
            key={`${props.toolCallId}:${index}`}
            result={item}
            toolCallId={`${props.toolCallId}:${index}`}
            toolName={toolLabelTitle(label)}
          />
        )
      })}
    </>
  )
}
