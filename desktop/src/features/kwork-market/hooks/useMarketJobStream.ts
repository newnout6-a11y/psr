import { useEffect, useReducer, useRef } from 'react'

import { marketJobWebSocketUrl, marketJobsApi } from '../api'
import { createInitialMarketJobState, marketJobReducer } from '../state/jobReducer'
import { parseMarketStreamMessage } from '../types'
import type { MarketJobEvent, MarketJobSnapshot, MarketStreamState } from '../types'

export interface UseMarketJobStreamOptions {
  jobId?: string | null
  enabled?: boolean
  afterSeq?: number
  eventLimit?: number
  reconnectDelayMs?: number
  staleAfterMs?: number
  onEvent?: (event: MarketJobEvent) => void
  onSnapshot?: (snapshot: MarketJobSnapshot) => void
  fetchSnapshot?: (jobId: string) => Promise<MarketJobSnapshot>
}

export interface UseMarketJobStreamResult {
  snapshot: MarketJobSnapshot | null
  events: MarketJobEvent[]
  lastSeq: number
  streamState: MarketStreamState
}

const DEFAULT_RECONNECT_DELAY_MS = 3_000
const DEFAULT_STALE_AFTER_MS = 20_000

export function useMarketJobStream(options: UseMarketJobStreamOptions): UseMarketJobStreamResult {
  const jobId = options.jobId ?? null
  const enabled = options.enabled !== false
  const initialAfterSeq = Math.max(0, options.afterSeq ?? 0)
  const eventLimit = Math.max(1, options.eventLimit ?? 500)
  const reconnectDelayMs = Math.max(100, options.reconnectDelayMs ?? DEFAULT_RECONNECT_DELAY_MS)
  const staleAfterMs = Math.max(1_000, options.staleAfterMs ?? DEFAULT_STALE_AFTER_MS)
  const [state, dispatch] = useReducer(marketJobReducer, eventLimit, createInitialMarketJobState)
  const lastSeqRef = useRef(initialAfterSeq)
  const eventHandlerRef = useRef(options.onEvent)
  const snapshotHandlerRef = useRef(options.onSnapshot)
  const snapshotFetcherRef = useRef(options.fetchSnapshot ?? marketJobsApi.getJob)

  eventHandlerRef.current = options.onEvent
  snapshotHandlerRef.current = options.onSnapshot
  snapshotFetcherRef.current = options.fetchSnapshot ?? marketJobsApi.getJob

  useEffect(() => {
    lastSeqRef.current = initialAfterSeq
    dispatch({ type: 'reset', lastSeq: initialAfterSeq, eventLimit })
  }, [eventLimit, initialAfterSeq, jobId])

  useEffect(() => {
    if (!enabled || !jobId) {
      dispatch({ type: 'stream_state', streamState: 'disconnected' })
      return
    }

    let disposed = false
    let socket: WebSocket | null = null
    let reconnectTimer: number | null = null
    let staleTimer: number | null = null
    let resyncing = false
    let staleDetected = false
    let lastMessageAt = Date.now()

    const clearReconnect = () => {
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer)
        reconnectTimer = null
      }
    }

    const scheduleReconnect = (delay = reconnectDelayMs) => {
      if (disposed || resyncing || reconnectTimer !== null) return
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null
        connect()
      }, delay)
    }

    const resync = async () => {
      if (disposed || resyncing) return
      resyncing = true
      clearReconnect()
      dispatch({ type: 'stream_state', streamState: 'resyncing' })
      socket?.close()
      try {
        const snapshot = await snapshotFetcherRef.current(jobId)
        if (disposed) return
        const sequence = Math.max(lastSeqRef.current, snapshot.last_event_sequence ?? 0)
        lastSeqRef.current = sequence
        dispatch({ type: 'snapshot', snapshot, resetEvents: true })
        snapshotHandlerRef.current?.(snapshot)
        resyncing = false
        scheduleReconnect(0)
      } catch {
        if (!disposed) {
          resyncing = false
          dispatch({ type: 'stream_state', streamState: 'stale' })
          scheduleReconnect()
        }
      }
    }

    const connect = () => {
      if (disposed || resyncing) return
      staleDetected = false
      dispatch({ type: 'stream_state', streamState: 'connecting' })
      const nextSocket = new WebSocket(marketJobWebSocketUrl(jobId, lastSeqRef.current))
      socket = nextSocket
      nextSocket.onopen = () => {
        if (disposed || socket !== nextSocket) return
        lastMessageAt = Date.now()
        dispatch({ type: 'stream_state', streamState: 'live' })
      }
      nextSocket.onmessage = (message) => {
        if (disposed || socket !== nextSocket) return
        try {
          const parsed = parseMarketStreamMessage(JSON.parse(message.data) as unknown)
          if (!parsed || parsed.job_id !== jobId) return
          lastMessageAt = Date.now()
          if (parsed.type === 'resync_required') {
            void resync()
            return
          }
          if (parsed.seq <= lastSeqRef.current) return
          lastSeqRef.current = parsed.seq
          dispatch({ type: 'event', event: parsed })
          eventHandlerRef.current?.(parsed)
        } catch {
          // Ignore malformed frames; a later replay or resync restores consistency.
        }
      }
      nextSocket.onerror = () => nextSocket.close()
      nextSocket.onclose = () => {
        if (disposed || resyncing || socket !== nextSocket) return
        dispatch({ type: 'stream_state', streamState: staleDetected ? 'stale' : 'disconnected' })
        scheduleReconnect()
      }
    }

    staleTimer = window.setInterval(() => {
      if (disposed || resyncing || socket?.readyState !== WebSocket.OPEN) return
      if (Date.now() - lastMessageAt <= staleAfterMs) return
      staleDetected = true
      dispatch({ type: 'stream_state', streamState: 'stale' })
      socket.close()
    }, Math.min(staleAfterMs, 5_000))
    connect()

    return () => {
      disposed = true
      clearReconnect()
      if (staleTimer !== null) window.clearInterval(staleTimer)
      socket?.close()
    }
  }, [enabled, eventLimit, initialAfterSeq, jobId, reconnectDelayMs, staleAfterMs])

  return {
    snapshot: state.snapshot,
    events: state.events,
    lastSeq: state.lastSeq,
    streamState: state.streamState,
  }
}
