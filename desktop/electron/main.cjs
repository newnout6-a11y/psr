const { app, BrowserWindow, ipcMain, shell } = require('electron')
const path = require('path')
const { spawn, execSync } = require('child_process')
const http = require('http')
const fs = require('fs')

const isDev = process.env.NODE_ENV === 'development' || !app.isPackaged

function hasBackendRoot(root) {
  if (!root) return false
  return fs.existsSync(path.join(root, 'src', 'api', 'server.py'))
}

function resolvePSRRoot() {
  const candidates = [
    process.env.PSR_ROOT,
    path.resolve(__dirname, '..', '..'),
    'C:\\psr',
    app.isPackaged ? path.join(process.resourcesPath, 'psr-backend') : null,
  ]

  for (const root of candidates) {
    if (root && hasBackendRoot(root)) return root
  }

  return path.resolve(__dirname, '..', '..')
}

// Backend root is either the live repo (preferred, keeps .env local) or packaged resources.
const PSR_ROOT = resolvePSRRoot()

function usesPackagedBackend() {
  if (!app.isPackaged || !process.resourcesPath) return false
  const root = path.resolve(PSR_ROOT).toLowerCase()
  const resources = path.resolve(process.resourcesPath).toLowerCase()
  return root.startsWith(resources)
}

// Locate Python: try env var, then known path, then system python
function findPython() {
  const candidates = [
    process.env.PSR_PYTHON,
    'C:\\Users\\Redmi\\AppData\\Local\\Programs\\Python\\Python312\\python.exe',
    'python3',
    'python',
  ]
  for (const p of candidates) {
    if (!p) continue
    try {
      execSync(`"${p}" --version`, { timeout: 3000, stdio: 'ignore' })
      return p
    } catch (_) {}
  }
  return 'python'
}

let pythonProcess = null
let mainWindow = null

function startPythonServer() {
  const pythonExe = findPython()
  const serverScript = path.join(PSR_ROOT, 'src', 'api', 'server.py')

  if (!fs.existsSync(serverScript)) {
    console.error(`[Electron] Backend entrypoint not found: ${serverScript}`)
    return
  }

  console.log(`[Electron] Starting Python backend from ${PSR_ROOT}: ${pythonExe} ${serverScript}`)
  const backendEnv = { ...process.env, PSR_ROOT }

  if (usesPackagedBackend()) {
    backendEnv.PSR_DATA_DIR = path.join(app.getPath('userData'), 'data')
    const referenceDir = path.join(PSR_ROOT, 'data', 'reference')
    if (fs.existsSync(referenceDir)) {
      backendEnv.PSR_REFERENCE_DIR = referenceDir
    }
  }

  pythonProcess = spawn(
    pythonExe,
    ['-m', 'uvicorn', 'src.api.server:app', '--host', '127.0.0.1', '--port', '7788', '--no-access-log'],
    {
      cwd: PSR_ROOT,
      env: backendEnv,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    }
  )

  pythonProcess.stdout.on('data', (d) => {
    const text = d.toString().trim()
    if (text) console.log(`[Python] ${text}`)
  })
  pythonProcess.stderr.on('data', (d) => {
    const text = d.toString().trim()
    if (text) console.log(`[Python ERR] ${text}`)
  })
  pythonProcess.on('exit', (code) => {
    console.log(`[Electron] Python backend exited with code ${code}`)
    pythonProcess = null
  })
}

function waitForBackend(url, retries, interval, callback) {
  http.get(url, (res) => {
    if (res.statusCode === 200) {
      callback(null)
    } else {
      retry()
    }
  }).on('error', () => {
    if (retries <= 0) {
      callback(new Error('Backend did not start in time'))
    } else {
      setTimeout(() => waitForBackend(url, retries - 1, interval, callback), interval)
    }
  })

  function retry() {
    if (retries <= 0) {
      callback(new Error('Backend did not start in time'))
    } else {
      setTimeout(() => waitForBackend(url, retries - 1, interval, callback), interval)
    }
  }
}

function createWindow() {
  const windowIcon = isDev
    ? path.join(__dirname, '..', 'public', 'icon.png')
    : path.join(__dirname, '..', 'dist', 'icon.png')

  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1100,
    minHeight: 700,
    backgroundColor: '#030303',
    titleBarStyle: 'hidden',
    titleBarOverlay: {
      color: '#030303',
      symbolColor: '#a8a29e',
      height: 36,
    },
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    show: false,
    icon: windowIcon,
  })

  mainWindow.once('ready-to-show', () => {
    mainWindow.show()
  })

  // Open external links in browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })

  if (isDev) {
    mainWindow.loadURL('http://127.0.0.1:5173')
    mainWindow.webContents.openDevTools({ mode: 'detach' })
  } else {
    mainWindow.loadFile(path.join(__dirname, '..', 'dist', 'index.html'))
  }
}

app.whenReady().then(() => {
  startPythonServer()

  const healthUrl = 'http://127.0.0.1:7788/api/health'
  const maxWait = isDev ? 60 : 30  // seconds

  waitForBackend(healthUrl, maxWait * 2, 500, (err) => {
    if (err) {
      console.error('[Electron] Warning: backend may not be ready:', err.message)
    } else {
      console.log('[Electron] Backend is ready')
    }
    createWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
  }
})

app.on('before-quit', () => {
  if (pythonProcess) {
    console.log('[Electron] Killing Python backend...')
    try {
      process.platform === 'win32'
        ? spawn('taskkill', ['/pid', pythonProcess.pid.toString(), '/f', '/t'])
        : pythonProcess.kill('SIGTERM')
    } catch (_) {}
  }
})

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow()
})

ipcMain.handle('get-psr-root', () => PSR_ROOT)
ipcMain.handle('open-external', (_, url) => shell.openExternal(url))
