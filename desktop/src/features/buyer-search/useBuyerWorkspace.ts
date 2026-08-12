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
  const latestRef = useRef(state)
  latestRef.current = state

  useEffect(() => {
    const controller = new AbortController()
    buyerSearchApi.getWorkspace(controller.signal)
      .then((snapshot) => setState(normalizeWorkspace(snapshot.state)))
      .catch(() => setSaveState('offline'))
      .finally(() => setReady(true))
    return () => controller.abort()
  }, [])

  useEffect(() => {
    if (!ready) return
    setSaveState('saving')
    const timer = window.setTimeout(() => {
      buyerSearchApi.putWorkspace(latestRef.current)
        .then(() => setSaveState('saved'))
        .catch(() => setSaveState('offline'))
    }, 450)
    return () => window.clearTimeout(timer)
  }, [ready, state])

  useEffect(() => {
    const flush = () => {
      if (document.visibilityState === 'hidden') {
        void buyerSearchApi.putWorkspace(latestRef.current, { keepalive: true })
      }
    }
    window.addEventListener('pagehide', flush)
    document.addEventListener('visibilitychange', flush)
    return () => {
      window.removeEventListener('pagehide', flush)
      document.removeEventListener('visibilitychange', flush)
    }
  }, [])

  const updateBuilder = useCallback((patch: Partial<BuyerSearchWorkspaceBuilder>) => {
    setState((current) => ({ ...current, builder: { ...current.builder, ...patch } }))
  }, [])

  const updateView = useCallback((patch: Partial<BuyerSearchWorkspaceView>) => {
    setState((current) => ({ ...current, view: { ...current.view, ...patch } }))
  }, [])

  return { state, ready, saveState, updateBuilder, updateView }
}
