import type {
  JsonRecord,
  MarketJobEvent,
  MarketJobPhase,
  MarketJobSnapshot,
  MarketJobState,
  MarketStreamState,
} from '../types'
import { isMarketJobSnapshot } from '../types'

export const MARKET_JOB_EVENT_LIMIT = 500

export interface MarketJobClientState {
  snapshot: MarketJobSnapshot | null
  events: MarketJobEvent[]
  lastSeq: number
  streamState: MarketStreamState
  eventLimit: number
}

export type MarketJobAction =
  | { type: 'reset'; lastSeq?: number; eventLimit?: number }
  | { type: 'snapshot'; snapshot: MarketJobSnapshot; resetEvents?: boolean }
  | { type: 'event'; event: MarketJobEvent }
  | { type: 'stream_state'; streamState: MarketStreamState }

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isMarketJobState(value: unknown): value is MarketJobState {
  return typeof value === 'string' && [
    'preparing', 'mapping', 'planning', 'running', 'pausing', 'paused', 'completing', 'enriching', 'analyzing', 'finalizing',
    'completed', 'stopping', 'stopped', 'blocked', 'failed',
  ].includes(value as MarketJobState)
}

function isMarketJobPhase(value: unknown): value is MarketJobPhase {
  return typeof value === 'string' && ['prepare', 'map', 'plan', 'collect', 'enrich', 'analyze', 'export'].includes(value as MarketJobPhase)
}

function numberRecord(value: unknown): Record<string, number> | null {
  if (!isRecord(value)) return null
  const entries = Object.entries(value)
  if (!entries.every(([, item]) => typeof item === 'number')) return null
  return Object.fromEntries(entries) as Record<string, number>
}

function snapshotFromEvent(event: MarketJobEvent): MarketJobSnapshot | null {
  if (event.type !== 'job.snapshot') return null
  const candidate = event.payload.snapshot ?? event.payload
  return isMarketJobSnapshot(candidate) ? candidate : null
}

function applyEvent(snapshot: MarketJobSnapshot | null, event: MarketJobEvent): MarketJobSnapshot | null {
  const embeddedSnapshot = snapshotFromEvent(event)
  if (embeddedSnapshot) return embeddedSnapshot
  if (!snapshot) return snapshot
  const job = snapshot.job
  let nextJob = job
  if (event.type === 'job.state_changed' && isMarketJobState(event.payload.state)) {
    nextJob = { ...nextJob, state: event.payload.state }
  }
  if (event.type === 'job.phase_changed' && isMarketJobPhase(event.payload.phase)) {
    nextJob = { ...nextJob, phase: event.payload.phase }
  }
  if (event.type === 'job.metrics') {
    const counters = numberRecord(event.payload.counters) ?? numberRecord(event.payload)
    if (counters) nextJob = { ...nextJob, counters: { ...nextJob.counters, ...counters } }
  }
  if (typeof event.revision === 'number') nextJob = { ...nextJob, revision: event.revision }
  return { ...snapshot, job: nextJob, last_event_sequence: event.seq }
}

function appendBounded(events: MarketJobEvent[], event: MarketJobEvent, limit: number): MarketJobEvent[] {
  if (events.length < limit) return [...events, event]
  return [...events.slice(-(limit - 1)), event]
}

export function createInitialMarketJobState(eventLimit = MARKET_JOB_EVENT_LIMIT): MarketJobClientState {
  return {
    snapshot: null,
    events: [],
    lastSeq: 0,
    streamState: 'disconnected',
    eventLimit: Math.max(1, eventLimit),
  }
}

export function marketJobReducer(state: MarketJobClientState, action: MarketJobAction): MarketJobClientState {
  switch (action.type) {
    case 'reset':
      return {
        ...createInitialMarketJobState(action.eventLimit ?? state.eventLimit),
        lastSeq: Math.max(0, action.lastSeq ?? 0),
      }
    case 'snapshot':
      return {
        ...state,
        snapshot: action.snapshot,
        events: action.resetEvents ? [] : state.events,
        lastSeq: Math.max(state.lastSeq, action.snapshot.last_event_sequence ?? 0),
      }
    case 'event':
      if (action.event.seq <= state.lastSeq) return state
      return {
        ...state,
        snapshot: applyEvent(state.snapshot, action.event),
        events: appendBounded(state.events, action.event, state.eventLimit),
        lastSeq: action.event.seq,
      }
    case 'stream_state':
      return { ...state, streamState: action.streamState }
  }
}
