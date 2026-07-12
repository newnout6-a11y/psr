import { useCallback, useEffect, useRef, useState } from 'react'

import { marketJobsApi } from '../api'
import type { MarketJobsApiClient } from '../api'
import type { MarketJob, MarketJobsQuery } from '../types'

export type MarketJobsLoadState = 'idle' | 'loading' | 'ready' | 'error'

export interface UseMarketJobsOptions {
  apiClient?: Pick<MarketJobsApiClient, 'listJobs'>
  autoLoad?: boolean
  limit?: number
}

export interface UseMarketJobsResult {
  jobs: MarketJob[]
  nextCursor: number | null
  hasMore: boolean
  state: MarketJobsLoadState
  error: Error | null
  refresh(): Promise<void>
  loadMore(): Promise<void>
}

const DEFAULT_LIMIT = 50

export function useMarketJobs(options: UseMarketJobsOptions = {}): UseMarketJobsResult {
  const client = options.apiClient ?? marketJobsApi
  const limit = Math.max(1, options.limit ?? DEFAULT_LIMIT)
  const [jobs, setJobs] = useState<MarketJob[]>([])
  const [nextCursor, setNextCursor] = useState<number | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [state, setState] = useState<MarketJobsLoadState>('idle')
  const [error, setError] = useState<Error | null>(null)
  const requestIdRef = useRef(0)
  const loadingRef = useRef(false)

  const load = useCallback(async (params: MarketJobsQuery, append: boolean) => {
    if (loadingRef.current) return
    loadingRef.current = true
    const requestId = ++requestIdRef.current
    setState('loading')
    setError(null)
    try {
      const page = await client.listJobs({ ...params, limit })
      if (requestId !== requestIdRef.current) return
      setJobs((current) => (append ? [...current, ...page.items] : page.items))
      setNextCursor(typeof page.next_cursor === 'number' ? page.next_cursor : null)
      setHasMore(typeof page.next_cursor === 'number')
      setState('ready')
    } catch (reason) {
      if (requestId !== requestIdRef.current) return
      setError(reason instanceof Error ? reason : new Error(String(reason)))
      setState('error')
    } finally {
      if (requestId === requestIdRef.current) loadingRef.current = false
    }
  }, [client, limit])

  const refresh = useCallback(async () => {
    await load({}, false)
  }, [load])

  const loadMore = useCallback(async () => {
    if (!nextCursor || !hasMore) return
    await load({ offset: nextCursor }, true)
  }, [hasMore, load, nextCursor])

  useEffect(() => {
    if (options.autoLoad !== false) void refresh()
  }, [options.autoLoad, refresh])

  return { jobs, nextCursor, hasMore, state, error, refresh, loadMore }
}
