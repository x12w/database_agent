const { contextBridge, ipcRenderer } = require('electron')
contextBridge.exposeInMainWorld('desktop', Object.freeze({
  isDesktop: true,
  restart: () => ipcRenderer.invoke('desktop:restart'),
  openDataDirectory: () => ipcRenderer.invoke('desktop:open-data'),
  chooseKnowledgeFile: () => ipcRenderer.invoke('desktop:choose-knowledge'),
}))
