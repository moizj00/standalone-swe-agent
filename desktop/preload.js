// Preload script. Runs in an isolated context with access to a limited Node
// surface, before the renderer (the React dashboard) loads.
//
// The dashboard is a self-contained web app that talks to its own Express proxy
// over HTTP, so it needs nothing from Electron today. We still expose a tiny,
// explicit bridge for future native features (e.g. an OS folder picker to choose
// the agent workspace) without ever enabling nodeIntegration in the renderer.

'use strict';

const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('sweDesktop', {
  isElectron: true,
  platform: process.platform,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  },
});
