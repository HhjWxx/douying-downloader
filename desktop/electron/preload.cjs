const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('desktop', {
  api: (method, route, body) => ipcRenderer.invoke('api', method, route, body),
  browser: (action, data) => ipcRenderer.invoke('browser', action, data),
  chooseDirectory: () => ipcRenderer.invoke('choose-directory'),
  openPath: (path) => ipcRenderer.invoke('open-path', path),
  preview: (path) => ipcRenderer.invoke('preview', path),
  readReport: (path) => ipcRenderer.invoke('read-report', path),
  importData: () => ipcRenderer.invoke('import-data'),
  diagnostics: () => ipcRenderer.invoke('diagnostics'),
  restart: () => ipcRenderer.invoke('restart'),
  onEvent: (callback) => {
    const listener = (_event, message) => callback(message);
    ipcRenderer.on('desktop-event', listener);
    return () => ipcRenderer.removeListener('desktop-event', listener);
  },
});
