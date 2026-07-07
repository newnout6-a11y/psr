import { useState, useEffect, useCallback, useRef } from 'react'

export function useApi<T>(
  fetcher: () => Promise<T>,
  deps: unknown[] = [],
  initialValue?: T,
) {
  const [data, setData] = useState<T | undefined>(initialValue)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const requestId = useRef(0)

  const fetch = useCallback(async () => {
    const currentRequest = requestId.current + 1
    requestId.current = currentRequest
    setLoading(true)
    setError(null)
    try {
      const result = await fetcher()
      if (requestId.current !== currentRequest) return
      setData(result)
    } catch (e) {
      if (requestId.current !== currentRequest) return
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (requestId.current === currentRequest) setLoading(false)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    fetch()
  }, [fetch])

  return { data, loading, error, refetch: fetch }
}
