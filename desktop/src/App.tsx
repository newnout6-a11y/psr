import { lazy, Suspense } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Queue from './pages/Queue'
import Skipped from './pages/Skipped'
import Settings from './pages/Settings'
import Logs from './pages/Logs'
import OsintPage from './pages/Osint'
import Earnings from './pages/Earnings'
import Health from './pages/Health'
import Chat from './pages/Chat'
import Conversations from './pages/Conversations'
import Orders from './pages/Orders'
import KworkRegistration from './pages/KworkRegistration'

const KworkMarket = lazy(() => import('./pages/KworkMarket'))
const KworkMarketWorkspace = lazy(() => import('./pages/KworkMarketWorkspace'))
const KworkMarketJob = lazy(() => import('./pages/KworkMarketJob'))

const marketFallback = <div className="p-5 text-sm text-zinc-500">Загрузка рабочего пространства рынка...</div>

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="dashboard" element={<Dashboard />} />
        <Route path="queue" element={<Queue />} />
        <Route path="skipped" element={<Skipped />} />
        <Route path="conversations" element={<Conversations />} />
        <Route path="orders" element={<Orders />} />
        <Route path="kwork-market" element={<Suspense fallback={marketFallback}><KworkMarketWorkspace /></Suspense>} />
        <Route path="kwork-market/jobs/:jobId" element={<Suspense fallback={marketFallback}><KworkMarketJob /></Suspense>} />
        <Route path="kwork-market/legacy" element={<Suspense fallback={marketFallback}><KworkMarket /></Suspense>} />
        <Route path="kwork-registration" element={<KworkRegistration />} />
        <Route path="earnings" element={<Earnings />} />
        <Route path="health" element={<Health />} />
        <Route path="chat" element={<Chat />} />
        <Route path="settings" element={<Settings />} />
        <Route path="logs" element={<Logs />} />
        <Route path="osint" element={<OsintPage />} />
      </Route>
    </Routes>
  )
}
