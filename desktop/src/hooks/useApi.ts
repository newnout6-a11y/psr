import { useState, useEffect, useCallback } from 'react'

export function useApi<T>(
  fetcher: () => Promise<T>,
  deps: unknown[] = [],
  initialValue?: T,
) {
  const [data, setData] = useState<T | undefined>(initialValue)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const fetch = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const result = await fetcher()
      setData(result)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    fetch()
  }, [fetch])

  return { data, loading, error, refetch: fetch }
}
