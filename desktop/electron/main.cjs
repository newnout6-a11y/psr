const { app, BrowserWindow, ipcMain, shell, dialog, session } = require('electron')
const path = require('path')
const { spawn, exec, execSync } = require('child_process')
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

const SESSION_HUB_DIR = 'C:\\pechenki\\session_hub'
const SESSION_HUB_PORT = 8669
const KWORK_VERIFY_PARTITION = 'persist:kwork-verification'

function isSessionHubRunning() {
  return new Promise((resolve) => {
    http.get(`http://127.0.0.1:${SESSION_HUB_PORT}/cookies?domain=kwork.ru`, (res) => {
      resolve(res.statusCode === 200)
    }).on('error', () => resolve(false))
      .setTimeout(1500, function() { this.destroy(); resolve(false) })
  })
}

function launchSessionHubAsAdmin(exePath) {
  // exec() держит дочерний процесс живым, пока PowerShell не завершится.
  // Start-Process -Verb RunAs вызывает стандартный UAC Windows.
  const cmd = `powershell.exe -NoProfile -Command "Start-Process -FilePath '${exePath}' -Verb RunAs"`
  console.log('[Electron] Running:', cmd)
  exec(cmd, (error, stdout, stderr) => {
    if (error) {
      console.error('[Electron] Session Hub launch error:', error.message)
    }
    if (stderr) {
      console.error('[Electron] Session Hub stderr:', stderr)
    }
    if (stdout) {
      console.log('[Electron] Session Hub stdout:', stdout)
    }
  })
}


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
let kworkVerifyWindow = null
let kworkVerifySaveTimer = null

function runtimeDir() {
  if (usesPackagedBackend()) return path.join(app.getPath('userData'), 'data', 'runtime')
  return path.join(PSR_ROOT, 'data', 'runtime')
}

function kworkManualCookiesPath() {
  return path.join(runtimeDir(), 'kwork_manual_cookies.json')
}

function getJson(url, timeoutMs = 3500) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, (res) => {
      let raw = ''
      res.setEncoding('utf8')
      res.on('data', (chunk) => { raw += chunk })
      res.on('end', () => {
        try {
          resolve(JSON.parse(raw || '{}'))
        } catch (error) {
          reject(error)
        }
      })
    })
    req.on('error', reject)
    req.setTimeout(timeoutMs, () => {
      req.destroy(new Error('timeout'))
    })
  })
}

function normalizeKworkUrl(rawUrl) {
  try {
    const parsed = new URL(rawUrl || 'https://kwork.ru/')
    if (!parsed.hostname.endsWith('kwork.ru')) return 'https://kwork.ru/'
    return parsed.toString()
  } catch (_) {
    return 'https://kwork.ru/'
  }
}

function normalizeElectronCookie(rawCookie) {
  const name = String(rawCookie?.name || '').trim()
  const value = String(rawCookie?.value || '')
  if (!name || !value) return null

  const cookie = {
    url: 'https://kwork.ru/',
    name,
    value,
    path: rawCookie.path || '/',
    secure: rawCookie.secure !== false,
    httpOnly: !!rawCookie.httpOnly,
  }
  const domain = String(rawCookie.domain || '.kwork.ru').trim()
  if (domain && domain.includes('kwork.ru')) cookie.domain = domain
  const expires = Number(rawCookie.expirationDate || rawCookie.expires || 0)
  if (Number.isFinite(expires) && expires > 0) cookie.expirationDate = expires
  return cookie
}

async function importSessionHubCookies(targetSession) {
  try {
    const data = await getJson(`http://127.0.0.1:${SESSION_HUB_PORT}/cookies?domain=kwork.ru`)
    const cookies = Array.isArray(data.cookies) ? data.cookies : []
    let imported = 0
    for (const rawCookie of cookies) {
      const cookie = normalizeElectronCookie(rawCookie)
      if (!cookie) continue
      try {
        await targetSession.cookies.set(cookie)
        imported += 1
      } catch (error) {
        console.warn('[Electron] Failed to import Kwork cookie:', cookie.name, error.message)
      }
    }
    return imported
  } catch (error) {
    console.warn('[Electron] Session Hub cookies import skipped:', error.message)
    return 0
  }
}

async function saveKworkVerificationCookies(targetSession) {
  const kworkCookies = await targetSession.cookies.get({ domain: 'kwork.ru' })
  fs.mkdirSync(runtimeDir(), { recursive: true })
  fs.writeFileSync(
    kworkManualCookiesPath(),
    JSON.stringify(
      {
        saved_at: new Date().toISOString(),
        source: 'electron-kwork-verification',
        cookies: kworkCookies,
      },
      null,
      2,
    ),
    'utf8',
  )
  return kworkCookies.length
}

async function openKworkVerificationWindow(targetUrl = 'https://kwork.ru/') {
  const safeUrl = normalizeKworkUrl(targetUrl)
  const verifySession = session.fromPartition(KWORK_VERIFY_PARTITION)
  const imported = await importSessionHubCookies(verifySession)

  if (kworkVerifyWindow && !kworkVerifyWindow.isDestroyed()) {
    kworkVerifyWindow.focus()
    await kworkVerifyWindow.loadURL(safeUrl)
    return { ok: true, reused: true, imported }
  }

  kworkVerifyWindow = new BrowserWindow({
    width: 1180,
    height: 860,
    minWidth: 900,
    minHeight: 640,
    parent: mainWindow || undefined,
    title: 'Kwork manual verification',
    backgroundColor: '#ffffff',
    webPreferences: {
      partition: KWORK_VERIFY_PARTITION,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })
  kworkVerifyWindow.setMenuBarVisibility(false)
  kworkVerifySaveTimer = setInterval(() => {
    saveKworkVerificationCookies(verifySession).catch(() => {})
  }, 5000)
  kworkVerifyWindow.on('closed', async () => {
    if (kworkVerifySaveTimer) {
      clearInterval(kworkVerifySaveTimer)
      kworkVerifySaveTimer = null
    }
    try {
      const saved = await saveKworkVerificationCookies(verifySession)
      console.log(`[Electron] Saved Kwork manual verification cookies: ${saved}`)
    } catch (error) {
      console.warn('[Electron] Failed to save Kwork verification cookies:', error.message)
    }
    kworkVerifyWindow = null
  })
  await kworkVerifyWindow.loadURL(safeUrl)
  return { ok: true, reused: false, imported }
}

function kworkVerificationStatus() {
  const file = kworkManualCookiesPath()
  if (!fs.existsSync(file)) return { ok: true, exists: false, cookie_count: 0 }
  try {
    const data = JSON.parse(fs.readFileSync(file, 'utf8'))
    return {
      ok: true,
      exists: true,
      saved_at: data.saved_at || '',
      cookie_count: Array.isArray(data.cookies) ? data.cookies.length : 0,
    }
  } catch (error) {
    return { ok: false, exists: true, error: error.message, cookie_count: 0 }
  }
}

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

app.whenReady().then(async () => {
  // Проверяем, запущен ли Session Hub
  const hubAlreadyRunning = await isSessionHubRunning()
  console.log('[Electron] Session Hub running:', hubAlreadyRunning)

  if (!hubAlreadyRunning) {
    const choice = await dialog.showMessageBox({
      type: 'question',
      buttons: ['Да', 'Нет'],
      title: 'Session Hub',
      message: 'Session Hub не запущен. Запустить?',
      detail: 'Потребуется для работы с браузером и куками. Будет запрошено подтверждение прав администратора.',
      defaultId: 0,
      cancelId: 1
    })

    if (choice.response === 0) {
      const exePath = path.join(SESSION_HUB_DIR, 'dist', 'session_hub.exe')
      const pyPath = path.join(SESSION_HUB_DIR, 'server.py')

      try {
        if (fs.existsSync(exePath)) {
          console.log('[Electron] Starting Session Hub as admin (exe)...')
          launchSessionHubAsAdmin(exePath)
        } else if (fs.existsSync(pyPath)) {
          console.log('[Electron] Starting Session Hub (py)...')
          const pythonExe = findPython()
          spawn(pythonExe, [pyPath], { cwd: SESSION_HUB_DIR, detached: true, stdio: 'ignore', windowsHide: false }).unref()
        } else {
          console.error('[Electron] Session Hub not found in', SESSION_HUB_DIR)
        }
      } catch (e) {
        console.error('[Electron] Error starting Session Hub:', e)
      }
    }
  }

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
ipcMain.handle('open-kwork-verification', (_, url) => openKworkVerificationWindow(url))
ipcMain.handle('kwork-verification-status', () => kworkVerificationStatus())
