// 内置更新 · 纯函数核心（零 Electron / 零磁盘 / 零网络依赖，可单独跑测试）
// 所有「要不要更新、这个文件能不能覆盖、进度算多少」的判断都在这里，
// updater.js 只负责发请求和读写文件，不自己拍板。

'use strict';

// 版本号只认 主.次.修订，允许前面带 v。写出来的是 1.6.0 / v1.6.0 这种。
var VERSION_RE = /^v?(\d+)\.(\d+)\.(\d+)$/;

function parseVersion(v) {
  var m = VERSION_RE.exec(String(v == null ? '' : v).trim());
  if (!m) return null;
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}

function compareVersion(a, b) {
  var x = parseVersion(a), y = parseVersion(b);
  // 任一边解析不出来就当相等：宁可不更新，也不要因为一个脏版本号乱覆盖文件。
  if (!x || !y) return 0;
  for (var i = 0; i < 3; i++) {
    if (x[i] !== y[i]) return x[i] > y[i] ? 1 : -1;
  }
  return 0;
}

function isNewer(candidate, current) {
  return compareVersion(candidate, current) > 0;
}

// 公开通道允许覆盖的文件：程序本体 + 说明。多一个都不放行。
// 这里全是写死的白名单，绝不用「目录里除了 accounts.csv 都能覆盖」这种黑名单——
// 黑名单在新版本加了新数据文件时会漏，白名单只会漏更新、不会误删数据。
var PUBLIC_FILES = [
  'main.js',
  'preload.js',
  'updater_core.js',
  'updater_lib.py',
  'updater.js',
  'gpu_mode.js',
  'package.json',
  'run_usrcloud.py',
  'usr_control.py',
  'varsort.py',
  'watch_engine.py',
  'webui_server.py',
  'ui/index.html',
  'build/icon.ico',
  'build/icon.png',
  '使用说明.txt',
  '更新记录.txt'
];

// 私密通道额外允许的文件：账号表 + 用户配置 + 登录缓存（只放 json）。
// 历史数据 / 截图批次 / 日志一律不进更新包：体积大、且是运行产生的，不是「更新」。
var PRIVATE_FILES = [
  'accounts.csv',
  '监控规则.json',
  '变量排序.json',
  '窗口位置.json',
  '关闭方式.json',
  '渲染模式.json',
  '运行参数.json',
  '登录缓存'
];

// 把相对路径规整成 a/b/c；任何绝对路径、盘符、..、空段都判为非法。
function safeRel(p) {
  var s = String(p == null ? '' : p).replace(/\\/g, '/');
  if (!s || s.charAt(0) === '/') return null;
  if (/^[A-Za-z]:/.test(s)) return null;
  var parts = s.split('/');
  for (var i = 0; i < parts.length; i++) {
    var seg = parts[i];
    if (!seg || seg === '.' || seg === '..') return null;
    // Windows 保留字符：文件名里出现这些必然不是正常程序文件
    if (/[<>:"|?*]/.test(seg)) return null;
  }
  return parts.join('/');
}

function matchPrefix(rel, prefix) {
  return rel === prefix || rel.indexOf(prefix + '/') === 0;
}

function isAllowedFile(rel, channel) {
  var s = safeRel(rel);
  if (!s) return false;
  if (PUBLIC_FILES.indexOf(s) >= 0) return true;
  if (channel === 'private') {
    for (var i = 0; i < PRIVATE_FILES.length; i++) {
      if (matchPrefix(s, PRIVATE_FILES[i])) return true;
    }
  }
  return false;
}

// 校验清单：版本合法、必须有补丁包、sha256 格式对、每个文件都在白名单里。
// 返回 {ok, error, version, patchFile, sha256, size, files, channel, notes}
function validateManifest(m, channel) {
  if (!m || typeof m !== 'object') return { ok: false, error: '清单不是对象' };
  var ver = String(m.version || '');
  if (!parseVersion(ver)) return { ok: false, error: '版本号不合法：' + ver };
  var p = m.patch || m.file;
  var patchFile = (typeof p === 'string') ? p : (p && p.file);
  var safePatch = safeRel(patchFile);
  if (!safePatch) return { ok: false, error: '补丁包路径不合法' };
  var sha = (typeof p === 'object' && p && p.sha256) ? String(p.sha256) : String(m.sha256 || '');
  if (!/^[a-fA-F0-9]{64}$/.test(sha)) return { ok: false, error: '补丁包缺少合法 sha256' };
  var size = (typeof p === 'object' && p) ? Number(p.size) : Number(m.size || 0);
  if (!isFinite(size) || size < 0) size = 0;

  var ch = (m.channel === 'private' || channel === 'private') ? 'private' : 'public';
  var files = m.files;
  if (files !== undefined) {
    if (!Array.isArray(files)) return { ok: false, error: '文件列表不是数组' };
    for (var i = 0; i < files.length; i++) {
      var rel = (typeof files[i] === 'string') ? files[i] : (files[i] && files[i].path);
      if (!isAllowedFile(rel, ch)) {
        return { ok: false, error: '清单里有不允许覆盖的文件：' + String(rel) };
      }
    }
  }
  return {
    ok: true,
    version: ver,
    patchFile: safePatch,
    sha256: sha.toLowerCase(),
    size: size,
    channel: ch,
    files: files || null,
    notes: String(m.notes || '')
  };
}

// 从 GitHub Releases 的 latest 接口返回体里挑清单文件。
// 优先叫 update.json，兼容中文名 版本信息.json；找不到返回 null。
function findManifestAsset(release) {
  if (!release || !Array.isArray(release.assets)) return null;
  var want = ['update.json', '版本信息.json'];
  for (var i = 0; i < want.length; i++) {
    for (var j = 0; j < release.assets.length; j++) {
      var a = release.assets[j];
      if (a && a.name === want[i] && a.browser_download_url) return a.browser_download_url;
    }
  }
  return null;
}

// 下载进度：total 未知时 pct 返回 -1，前端据此显示「已下载 X MB」而不画百分比。
function pctOf(done, total) {
  var d = Number(done), t = Number(total);
  if (!isFinite(d) || d < 0) d = 0;
  if (!isFinite(t) || t <= 0) return -1;
  return Math.max(0, Math.min(100, Math.floor(d * 100 / t)));
}

// 本地日期 YYYY-MM-DD（本机时区，不用 toISOString 的 UTC，否则凌晨会差一天）
function localDay(ms) {
  var d = ms == null ? new Date() : new Date(ms);
  var p = function (n) { return String(n).padStart(2, '0'); };
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
}

// 每天自动检查一次：同一天内不重复检查；跨天（含往回跳）都查一次。
function needsDailyCheck(state, nowMs) {
  var last = state && state.lastCheckDate;
  if (typeof last !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(last)) return true;
  return last !== localDay(nowMs);
}

module.exports = {
  parseVersion: parseVersion,
  compareVersion: compareVersion,
  isNewer: isNewer,
  safeRel: safeRel,
  isAllowedFile: isAllowedFile,
  validateManifest: validateManifest,
  findManifestAsset: findManifestAsset,
  pctOf: pctOf,
  localDay: localDay,
  needsDailyCheck: needsDailyCheck,
  PUBLIC_FILES: PUBLIC_FILES,
  PRIVATE_FILES: PRIVATE_FILES
};
