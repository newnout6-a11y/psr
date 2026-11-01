import { useCallback, useEffect, useRef, useState } from 'react'

import { buyerSearchApi } from './api'
import type {
  BuyerSearchWorkspaceBuilder,
  BuyerSearchWorkspaceState,
  BuyerSearchWorkspaceView,
} from './types'

export const DEFAULT_BUYER_WORKSPACE: BuyerSearchWorkspaceState = {
  builder: {
    name: '',
    brief: '',
    exact_queries: [],
    taxonomy_selections: [],
    min_budget: '',
    max_budget: '10000',
    max_offers: '',
    min_buyer_hired_percent: '',
    max_age_hours: '24',
    target_projects: 100,
    workers: 2,
    query_batch_size: 1,
    account_registration_ids: [],
    enrichment_enabled: false,
    scoring_profile_id: '',
    advanced_open: false,
    preview_queries: [],
  },
  view: {
    active_section: 'setup',
    selected_run_id: null,
    selected_project_id: null,
    project_filters: {},
    project_sort: 'score_desc',
    cursor: null,
    cursor_history: [],
    selected_project_ids: [],
    shortlist_tags: '',
    shortlist_note: '',
    include_attachments: false,
    inspector_tab: 'details',
    inspector_open: true,
    runs_pane_width: 272,
    inspector_width: 390,
  },
}

function normalizeWorkspace(raw: unknown): BuyerSearchWorkspaceState {
  if (!raw || typeof raw !== 'object') return DEFAULT_BUYER_WORKSPACE
  const state = raw as Partial<BuyerSearchWorkspaceState>
  return {
    builder: { ...DEFAULT_BUYER_WORKSPACE.builder, ...(state.builder || {}) },
    view: { ...DEFAULT_BUYER_WORKSPACE.view, ...(state.view || {}) },
  }
}

export function useBuyerWorkspace() {
  const [state, setState] = useState<BuyerSearchWorkspaceState>(DEFAULT_BUYER_WORKSPACE)
  const [ready, setReady] = useState(false)
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'offline'>('idle')
  const [saveRetryCount, setSaveRetryCount] = useState(0)
  const latestRef = useRef(state)
  const hydratedRef = useRef(false)
  const revisionRef = useRef(0)
  const saveQueueRef = useRef<Promise<void>>(Promise.resolve())
  latestRef.current = state

  const queueSave = useCallback((revision: number, snapshot: BuyerSearchWorkspaceState, keepalive = false) => {
    // Serialize PUTs: a slow old request must not overwrite a newer saved
    // workspace. Ignore queued snapshots already superseded by a new edit.
    saveQueueRef.current = saveQueueRef.current.catch(() => undefined).then(async () => {
      if (revision !== revisionRef.current) return
      try {
        await buyerSearchApi.putWorkspace(snapshot, { keepalive })
        if (revision === revisionRef.current) {
          setSaveRetryCount(0)
          setSaveState('saved')
        }
      } catch {
        if (revision === revisionRef.current) {
          setSaveState('offline')
          setSaveRetryCount((count) => count + 1)
        }
      }
    })
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    let retryTimer: number | null = null
    let failures = 0
    const restore = async () => {
      try {
        const snapshot = await buyerSearchApi.getWorkspace(controller.signal)
        if (controller.signal.aborted) return
        hydratedRef.current = true
        setState(normalizeWorkspace(snapshot.state))
        setReady(true)
      } catch {
        if (controller.signal.aborted) return
        setSaveState('offline')
        // Never replace a persisted workspace with an empty default state
        // when startup failed. Retry GET until the authoritative state loads.
        retryTimer = window.setTimeout(restore, Math.min(30_000, 1_000 * 2 ** Math.min(failures++, 5)))
      }
    }
    void restore()
    return () => {
      controller.abort()
      if (retryTimer !== null) window.clearTimeout(retryTimer)
    }
  }, [])

  useEffect(() => {
    if (!ready || !hydratedRef.current) return
    const revision = ++revisionRef.current
    setSaveState('saving')
    const timer = window.setTimeout(() => {
      queueSave(revision, latestRef.current)
    }, 450)
    return () => window.clearTimeout(timer)
  }, [ready, state, queueSave])

  useEffect(() => {
    if (!ready || !hydratedRef.current || saveState !== 'offline') return
    const timer = window.setTimeout(() => {
      queueSave(revisionRef.current, latestRef.current)
    }, Math.min(30_000, 2_000 * 2 ** Math.min(saveRetryCount, 4)))
    return () => window.clearTimeout(timer)
  }, [queueSave, ready, saveRetryCount, saveState])

  useEffect(() => {
    const flush = () => {
      if (hydratedRef.current && document.visibilityState === 'hidden') {
        const revision = ++revisionRef.current
        queueSave(revision, latestRef.current, true)
      }
    }
    window.addEventListener('pagehide', flush)
    document.addEventListener('visibilitychange', flush)
    return () => {
      window.removeEventListener('pagehide', flush)
      document.removeEventListener('visibilitychange', flush)
    }
  }, [queueSave])

  const updateBuilder = useCallback((patch: Partial<BuyerSearchWorkspaceBuilder>) => {
    setState((current) => ({
      ...current,
      builder: {
        ...current.builder,
        // A manually reviewed query plan belongs to the brief, exact phrases
        // and category selection that produced it. Never silently reuse it
        // after any of those inputs have changed.
        ...(Object.prototype.hasOwnProperty.call(patch, 'brief') ||
          Object.prototype.hasOwnProperty.call(patch, 'exact_queries') ||
          Object.prototype.hasOwnProperty.call(patch, 'taxonomy_selections')
          ? { preview_queries: [] }
          : {}),
        ...patch,
      },
    }))
  }, [])

  const updateView = useCallback((patch: Partial<BuyerSearchWorkspaceView>) => {
    setState((current) => ({ ...current, view: { ...current.view, ...patch } }))
  }, [])

  return { state, ready, saveState, updateBuilder, updateView }
}
