import { useEffect, useRef, useCallback } from 'react'

export type WSMessage = Record<string, unknown>

export function useWebSocket(
  url: string,
  onMessage: (msg: WSMessage) => void,
  enabled = true,
) {
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimerRef = useRef<number | null>(null)
  const shouldReconnectRef = useRef(false)
  const cbRef = useRef(onMessage)
  cbRef.current = onMessage

  const connect = useCallback(() => {
    if (!enabled || !shouldReconnectRef.current) return
    const ws = new WebSocket(url)
    wsRef.current = ws

    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data) as WSMessage
        if (msg.type !== 'ping') cbRef.current(msg)
      } catch {}
    }

    ws.onclose = () => {
      if (!shouldReconnectRef.current) return
      // Auto-reconnect after 3 seconds
      reconnectTimerRef.current = window.setTimeout(() => {
        reconnectTimerRef.current = null
        if (shouldReconnectRef.current) connect()
      }, 3000)
    }

    ws.onerror = () => {
      ws.close()
    }
  }, [url, enabled])

  useEffect(() => {
    shouldReconnectRef.current = enabled
    connect()
    return () => {
      shouldReconnectRef.current = false
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }
      wsRef.current?.close()
      wsRef.current = null
    }
  }, [connect, enabled])
}
