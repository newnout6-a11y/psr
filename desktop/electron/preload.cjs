const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('electronAPI', {
  getPSRRoot: () => ipcRenderer.invoke('get-psr-root'),
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
  openKworkVerification: (url) => ipcRenderer.invoke('open-kwork-verification', url),
  getKworkVerificationStatus: () => ipcRenderer.invoke('kwork-verification-status'),
})
