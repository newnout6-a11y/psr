import { Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Queue from './pages/Queue'
import Settings from './pages/Settings'
import Logs from './pages/Logs'
import OsintPage from './pages/Osint'
import Conversion from './pages/Conversion'

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="dashboard" element={<Dashboard />} />
        <Route path="conversion" element={<Conversion />} />
        <Route path="queue" element={<Queue />} />
        <Route path="settings" element={<Settings />} />
        <Route path="logs" element={<Logs />} />
        <Route path="osint" element={<OsintPage />} />
      </Route>
    </Routes>
  )
}
