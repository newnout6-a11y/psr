import { Component, type ErrorInfo, type ReactNode } from 'react'
import { AlertTriangle, LayoutDashboard, RefreshCw } from 'lucide-react'

type RouteErrorBoundaryProps = {
  children: ReactNode
  resetKey: string
}

type RouteErrorBoundaryState = {
  error: Error | null
}

export default class RouteErrorBoundary extends Component<RouteErrorBoundaryProps, RouteErrorBoundaryState> {
  state: RouteErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): RouteErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Route render failed', error, info)
  }

  componentDidUpdate(previousProps: RouteErrorBoundaryProps) {
    if (previousProps.resetKey !== this.props.resetKey && this.state.error) {
      this.setState({ error: null })
    }
  }

  render() {
    if (!this.state.error) return this.props.children

    return (
      <div className="flex min-h-full items-center justify-center p-6" role="alert">
        <div className="factory-panel w-full max-w-xl p-5">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-red-500" />
            <div className="min-w-0 flex-1">
              <h2 className="text-base font-semibold text-stone-900">Раздел не удалось открыть</h2>
              <p className="mt-1 text-sm text-stone-600">
                Ошибка изолирована внутри текущего раздела. Навигация и остальные страницы продолжают работать.
              </p>
              <div className="mt-4 flex flex-wrap gap-2">
                <button type="button" className="btn btn-primary" onClick={() => window.location.reload()}>
                  <RefreshCw className="h-4 w-4" />
                  Повторить
                </button>
                <button type="button" className="btn btn-ghost" onClick={() => { window.location.hash = '#/dashboard' }}>
                  <LayoutDashboard className="h-4 w-4" />
                  На панель
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    )
  }
}
