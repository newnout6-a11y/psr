import { useEffect, useRef, useCallback } from 'react'

export type WSMessage = Record<string, unknown>

export function useWebSocket(
  url: string,
  onMessage: (msg: WSMessage) => void,
  enabled = true,
) {
  const wsRef = useRef<WebSocket | null>(null)
  const cbRef = useRef(onMessage)
  cbRef.current = onMessage

  const connect = useCallback(() => {
    if (!enabled) return
    const ws = new WebSocket(url)
    wsRef.current = ws

    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data) as WSMessage
        if (msg.type !== 'ping') cbRef.current(msg)
      } catch {}
    }

    ws.onclose = () => {
      // Auto-reconnect after 3 seconds
      setTimeout(() => {
        if (enabled) connect()
      }, 3000)
    }

    ws.onerror = () => {
      ws.close()
    }
  }, [url, enabled])

  useEffect(() => {
    connect()
    return () => {
      wsRef.current?.close()
    }
  }, [connect])
}
