// 内置更新 · 渲染层↔主进程的小桥接
// 页面跑在沙箱里拿不到 electron 模块；只暴露「重启」一个动作，其余更新逻辑都在 Python 后端。
// app.relaunch() 是 Electron 官方跨端接口，Windows / macOS 都走它，不需要分平台写重启命令。
'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('desktopUpdater', {
  restart: function () { return ipcRenderer.invoke('updater:restart'); },
  platform: function () { return process.platform; },
});

// 巡站截图 · 把列表里选中的截图当真实图片文件拖到系统其它窗口。
// dragstart 是同步事件：必须用 sendSync 等主进程 startDrag 完成后才返回，
// 否则 Electron 错过这次拖拽的同步上下文，预览会退回整页半透明快照而非截图本身。
contextBridge.exposeInMainWorld('desktopDrag', {
  start: function (files) { return ipcRenderer.sendSync('patrol:dragstart', files); },
});
