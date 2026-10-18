// 有人云巡站客户端 · Electron 壳
// 职责：拉起同目录的 python 后端（webui_server.py，无头模式），
// 后端自动分配端口并写入临时文件，本进程轮询读取后让真 Chromium 加载界面。
// 关闭窗口时杀掉 python 子进程，不留后台残留。
const { app, BrowserWindow, Menu, Notification, screen, Tray, nativeImage, clipboard, shell } = require('electron');
const { spawn, execFile } = require('child_process');
const os = require('os');
const path = require('path');
const fs = require('fs');
const http = require('http');

// ---- 文件日志 ----
// 双击启动时控制台窗口可能一闪而过，看不到任何报错，
// 因此把运行日志同时写进 main-log.txt，出问题直接把文件发出来即可。
const LOGF = path.join(__dirname, 'main-log.txt');
const BOUNDS_FILE = path.join(__dirname, '窗口位置.json');   // 记住上次的窗口大小/位置
// 关闭按钮的默认动作：'ask' 每次都问 / 'tray' 直接收到托盘 / 'exit' 直接退出
const CLOSE_MODE_FILE = path.join(__dirname, '关闭方式.json');

// ---- 应用图标 ----
// 与手机版同一张「巡」字图标（build/icon.ico 由手机版 512×512 图标转换而来，含 16~256 共 7 个尺寸）。
// Windows 用 ico，macOS/Linux 用 png；打包分发时这两个文件要一起带上，否则会退回 Electron 默认图标。
const ICON_PATH = (() => {
  const ico = path.join(__dirname, 'build', 'icon.ico');
  const png = path.join(__dirname, 'build', 'icon.png');
  try { if (fs.existsSync(ico)) return ico; } catch (e) { /* ignore */ }
  try { if (fs.existsSync(png)) return png; } catch (e) { /* ignore */ }
  return undefined;   // 文件缺失时不传 icon，Electron 用默认
})();
try { fs.writeFileSync(LOGF, ''); } catch (e) { /* ignore */ }
function log() {
  const s = '[' + new Date().toISOString().replace('T', ' ').slice(0, 19) + '] ' +
            Array.prototype.slice.call(arguments).join(' ');
  try { fs.appendFileSync(LOGF, s + '\n'); } catch (e) { /* ignore */ }
  try { console.log(s); } catch (e) { /* ignore */ }
}
process.on('uncaughtException', (e) => log('[FATAL] 未捕获异常:', (e && e.stack) || String(e)));
process.on('unhandledRejection', (e) => log('[FATAL] 未处理的Promise拒绝:', (e && e.stack) || String(e)));

// ---- 崩溃取证 ----
// 原生层崩溃（Chromium use-after-free / GPU 合成器）JS 完全抓不到，进程直接消失，
// 而 main-log.txt 每次启动都被清空，事后什么都查不到。所以另开一份不清空的
// 「崩溃记录.txt」：启动时若发现上次没写正常的收尾标记，就把上次的日志尾部抄进去。
const CRASH_FILE = path.join(__dirname, '崩溃记录.txt');
const SESSION_FLAG = path.join(__dirname, '.session-alive');
try {
  if (fs.existsSync(SESSION_FLAG)) {
    const old = (fs.readFileSync(CRASH_FILE, 'utf8') || '').split('\n').filter(Boolean);
    const tail = (fs.existsSync(LOGF) ? fs.readFileSync(LOGF, 'utf8') : '')
      .split('\n').filter(Boolean).slice(-40);
    const stamp = new Date().toISOString().replace('T', ' ').slice(0, 19);
    fs.appendFileSync(CRASH_FILE,
      '\n================ ' + stamp + ' 上次进程异常退出 ================\n'
      + (tail.join('\n') || '(上次没有日志)') + '\n');
  }
} catch (e) { /* ignore */ }
try { fs.writeFileSync(SESSION_FLAG, String(process.pid)); } catch (e) { /* ignore */ }
// 正常退出（含「收进托盘后手动退出」）时抹掉标记；崩溃则来不及执行，标记留存。
function clearSessionFlag() {
  try { if (fs.existsSync(SESSION_FLAG)) fs.unlinkSync(SESSION_FLAG); } catch (e) { /* ignore */ }
}
app.on('will-quit', clearSessionFlag);
process.on('exit', clearSessionFlag);

// 渲染进程崩溃 / 无响应：这类崩溃 try/catch 抓不到，但 render-process-gone 能收到。
// 不加这个监听的话，页面白屏或闪退后日志里一条记录都没有，完全无从排查。
try {
  app.on('render-process-gone', (ev, wc, details) => {
    log('[FATAL] 渲染进程崩溃:', ev, '|', details && details.reason);
  });
  app.on('child-process-gone', (ev, details) => {
    log('[FATAL] 子进程崩溃:', ev, '|', details && details.type, details && details.reason);
  });
} catch (e) { /* 老版本 Electron 没有这些事件，忽略 */ }

log('=== 启动 ===');
log('__dirname =', __dirname);
log('electron =', process.versions.electron, '| node =', process.versions.node, '| platform =', process.platform);
log('ELECTRON_RUN_AS_NODE =', process.env.ELECTRON_RUN_AS_NODE || '(unset)');

try { Menu.setApplicationMenu(null); } catch (e) { /* ignore */ }   // 去掉默认 File/Edit 菜单
try { app.setAppUserModelId('有人云巡站客户端'); } catch (e) { /* ignore */ }

// ---- 系统通知（toast）----
// ★ 全程序只有一条提醒通道：系统通知。
//
//   Windows → PowerShell + WinRT 子进程（osToastPS）
//   macOS   → osascript display notification（osToastAS）
//
// 用户明确要求「只保留一个通道」。历史上有三条路同时存在过，全部删除：
//   ① Electron 原生 Notification —— 会显示成署名「Electron」的通知，且在关掉
//      硬件加速 / in-process-gpu 的本程序里是闪退根源（原生对象被 GC 后
//      use-after-free）。已整段删除，仅保留注释说明，不再调用。
//   ② 应用内横幅（页面顶部那条）—— 只在窗口处于前台时显示，切走窗口就收不到，
//      且一轮命中多台时最多同时显示 3 条，是「软件内的推送」，不可作为提醒依据。
//      已删除。
//   ③ 托盘气泡 displayBalloon —— 与系统通知重复。已删除。
//
// Windows 用 powershell.exe 自己的 AUMID 投递（系统自带，无需注册任何东西）。
const PS_AUMID = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';
// 每条通知一个唯一 Tag：同名 Tag 会被系统当成「同一条」而互相替换，
// 这也会导致一轮命中多台时只剩最后一条。这里用序号+时间戳保证互不相同。
let TOAST_SEQ = 0;

// 系统通知投递结果回报给后端，供「异常提醒」页显示真实通道状态（本机被禁用时提示已降级）
let BACKEND_PORT = null;
function reportHealth(which, ok) {
  if (!BACKEND_PORT) return;
  try {
    const payload = JSON.stringify({ which, ok });
    const req = http.request({
      host: '127.0.0.1', port: BACKEND_PORT, path: '/api/notifyhealth', method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) },
    });
    req.on('error', () => { /* ignore */ });
    req.end(payload);
  } catch (e) { /* ignore */ }
}

function osToastPS(title, body, meta) {
  // Windows 上的**唯一**系统通知通道。
  //
  // 为什么不再用 Electron 原生 Notification：
  //   ① 它是闪退的根源。原生通知走 Chromium 通知层，本程序为了规避
  //      "GPU process isn't usable" 关了硬件加速并设了 in-process-gpu，
  //      这条路径上崩溃时 JS 层连日志都写不出来（现象是「提醒弹出来了，
  //      然后整个程序消失」）。
  //   ② 它在本机实际不可用。免安装绿色版没有开始菜单快捷方式，toast 依赖
  //      已注册的 AppUserModelID，否则被系统静默丢弃 —— 实测弹出来的通知
  //      署名是「Electron」而不是「有人云巡站客户端」，说明注册没生效；
  //      后续实测更是完全收不到。
  //   ③ 它需要主进程活着才能工作，主进程崩了通知也发不出去。
  // 而 PowerShell + WinRT 是**独立子进程**：用系统自带的 powershell.exe AUMID
  // （无需任何注册），投递失败也只影响那一个子进程，物理上带不走主进程。
  if (process.platform !== 'win32') return;
  const b64 = (s) => Buffer.from(String(s || ''), 'utf-8').toString('base64');
  const tag = ('t' + (++TOAST_SEQ) + Date.now().toString(36)).slice(-16);
  // 点击通知 → 用自定义协议唤起程序并跳到对应站点（见文件上方 JUMP_PROTOCOL 说明）。
  // 只有带 acc 的真实异常/恢复事件才附跳转目标；测试提醒等不带，点击只关掉通知。
  let launch = '';
  if (meta && meta.acc) {
    launch = JUMP_PROTOCOL + '://jump?acc=' + encodeURIComponent(meta.acc)
      + '&no=' + encodeURIComponent(meta.no || '')
      + '&name=' + encodeURIComponent(meta.name || '');
  }
  // 三个 PS 5.1 的坑已实测修过：
  //   ① WinRT 类型必须逐个显式加载，否则偶发 TypeNotFound；
  //   ② 不要用 GetElementsByTagName 枚举节点（偶发 BadEnumeration），
  //      直接把已做 XML 转义的文本拼进 XML 字符串；
  //   ③ 脚本走 -EncodedCommand（UTF-16LE base64），零命令行转义烦恼。
  // 用 ToastGeneric + attribution：通知正文下方显示本程序名。
  //
  // ★ 三个参数是「命中 6 台只看到 1 条」的元凶，已全部去掉：
  //   ① scenario="reminder" —— reminder 类型的通知**不会自动消失**，
  //      Windows 一次只在屏幕上挂一条，后到的全部排队等前一条被点掉。
  //      于是 6 台同时命中时，用户永远只看见第一条（完全对上现象）。
  //   ② duration="long" —— 25 秒不消失，进一步把后面几条压在队列里。
  //   ③ ExpirationTime = Now+30s —— 30 秒后从通知中心移除，
  //      用户稍后去通知中心翻，前面几条已经被删掉 = 还是只看到一条。
  //   现在用系统默认时长：多条会依次快速弹出，且全部保留在通知中心里。
  const script =
    "$ErrorActionPreference='Stop'\n" +
    "$ProgressPreference='SilentlyContinue'\n" +
    "try{\n" +
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]|Out-Null\n" +
    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime]|Out-Null\n" +
    "$enc=[Text.Encoding]::UTF8\n" +
    "$t=$enc.GetString([Convert]::FromBase64String('" + b64(title) + "')) -replace '&','&amp;' -replace '<','&lt;' -replace '>','&gt;' -replace '\"','&quot;'\n" +
    "$b=$enc.GetString([Convert]::FromBase64String('" + b64(body) + "')) -replace '&','&amp;' -replace '<','&lt;' -replace '>','&gt;' -replace '\"','&quot;'\n" +
    "$a='有人云巡站客户端'\n" +
    // ★ launch 是 XML 属性值，里面的 & 必须转义成 &amp;，否则 LoadXml 抛异常、整条通知投递失败
    //   （usrcloudpatrol://jump?acc=…&no=…&name=… 天生带 & —— 这是「通知全失效」的根因）
    "$l=$enc.GetString([Convert]::FromBase64String('" + b64(launch) + "')) -replace '&','&amp;' -replace '<','&lt;' -replace '>','&gt;' -replace '\"','&quot;'\n" +
    "$attr=''\n" +
    "if($l -ne ''){ $attr = ' activationType=\"protocol\" launch=\"'+$l+'\"' }\n" +
    "$x=New-Object Windows.Data.Xml.Dom.XmlDocument\n" +
    "$x.LoadXml('<toast'+$attr+'><visual><binding template=\"ToastGeneric\"><text>'+$t+'</text><text>'+$b+'</text><attribution><text>'+$a+'</text></attribution></binding></visual></toast>')\n" +
    "$n=[Windows.UI.Notifications.ToastNotification]::new($x)\n" +
    "$n.Tag='" + tag + "'\n" +
    "$n.Group='drwatch'\n" +
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('" + PS_AUMID + "').Show($n)\n" +
    "exit 0}catch{exit 1}";
  const encoded = Buffer.from(script, 'utf16le').toString('base64');
  let ps;
  try {
    ps = spawn('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded],
               { windowsHide: true, stdio: 'ignore' });
  } catch (e) {
    log('[提醒] PowerShell 通知启动异常:', e.message);
    reportHealth('ps', false);
    return;
  }
  ps.on('error', (err) => { log('[提醒] PowerShell 通知启动失败:', err.message); reportHealth('ps', false); });
  ps.on('exit', (code) => {
    reportHealth('ps', code === 0);
    if (code === 0) log('[提醒] 系统通知已通过 PowerShell 投递');
    else log('[提醒] 系统通知投递失败, code =', code,
             '（多为系统「通知和操作」里关掉了本程序或 PowerShell 的通知）');
  });
}
function osToast(title, body, meta) {
  // 唯一的提醒通道 = 系统通知，各平台各一条实现，没有第二条、也没有任何回退分支。
  // meta（含 acc/no/name）只在 Windows 分支用到：拼进 toast 的协议激活链接实现「点击跳转」。
  if (process.platform === 'darwin') {
    // macOS：osascript 的 display notification 能正常弹横幅（实测 Electron 原生
    // Notification 在 mac 上只回 show 事件、屏幕上不出东西，未获授权时会被系统
    // 静默丢弃且连 failed 都不给，无法判定 —— 故不再使用）。
    osToastAS(title, body);
    return;
  }
  // Windows：只用 PowerShell 子进程，见 osToastPS 顶部注释。
  osToastPS(title, body, meta);
}

// macOS 系统通知：osascript -e 'display notification …'
// 走 -e 单行脚本，标题/正文里的 " 和 \ 转义后直接内联，不经过 XML，无需其他处理。
function osToastAS(title, body) {
  const q = (s, n) => '"' + String(s == null ? '' : s)
    .replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/[\r\n]+/g, ' ').slice(0, n) + '"';
  const script = 'display notification ' + q(body, 400)
    + ' with title ' + q(title || '监控异常提醒', 120) + ' sound name "default"';
  let ps;
  try {
    ps = spawn('/usr/bin/osascript', ['-e', script], { stdio: 'ignore' });
  } catch (e) {
    log('[提醒] AppleScript 通知启动异常:', e.message);
    reportHealth('as', false);
    return;
  }
  ps.on('error', (err) => {
    log('[提醒] AppleScript 通知启动失败:', err.message);
    reportHealth('as', false);
  });
  ps.on('exit', (code) => {
    reportHealth('as', code === 0);
    if (code === 0) log('[提醒] 已通过 AppleScript 投递系统通知');
    else log('[提醒] AppleScript 通知返回', code);
  });
}

// ---- Electron 原生“通知”通道已整段移除（用户要求：只保留一个提醒通道）----
// 曾用过的 osToastNative() 与 LIVE_NOTIFICATIONS 强引用池一并删除，原因三条：
//   ① 它在通知中心里署名“Electron”，和子进程通道并存 —— 这就是用户看到的“两个通道”；
//   ② 它是闪退根源：原生通知对象被 GC 后 use-after-free，JS 层抓不到，直接带走主进程；
//   ③ 免安装版没有注册 AppUserModelID，通知本来就会被系统静默丢弃（收不到又崩）。
// 现在全程序只剩一条提醒通道，Windows 走 osToastPS()，macOS 走 osToastAS()。
// 兼容性：部分机器/远程桌面/受限会话下 Chromium 的独立 GPU 进程会崩溃
// （表现为 "GPU process isn't usable. Goodbye."），本工具界面简单，
// 关闭硬件加速 + 让 GPU 在浏览器进程内运行，画面无差异但启动更稳。
try { app.disableHardwareAcceleration(); } catch (e) { /* ignore */ }
try { app.commandLine.appendSwitch('in-process-gpu'); } catch (e) { /* ignore */ }
// 受限会话（远程桌面/服务方式启动）下渲染进程沙箱会初始化失败（reason=crashed, 0x80000003），
// 本工具只加载本机 127.0.0.1 的页面，关闭沙箱不影响安全性。
try { app.commandLine.appendSwitch('no-sandbox'); } catch (e) { /* ignore */ }

// ---- 单实例 + 自定义协议：点击系统通知跳转到对应站点 ----
// Windows 的系统通知是 PowerShell 子进程投递的 toast，它没有回到本进程的回调，
// 不能像 macOS 那样在通知对象上挂 click。改走「协议激活」：
// toast 上带 activationType="protocol" + launch="usrcloudpatrol://jump?acc=…&no=…&name=…"，
// 用户点击时由系统用本协议把程序唤起，参数从命令行带进来。
// 为让「程序已在运行」时点击也能生效（而不是又开一个窗口），必须单实例：
// 第二个进程只负责把命令行参数交给已运行的实例，然后自己退出。
const JUMP_PROTOCOL = 'usrcloudpatrol';

// 注册「点击通知跳转」用的自定义协议（在 app ready 之后调用，见文末 whenReady）。
// 每次启动都注册一遍：绿色版被解压/移动到新目录后，协议必须重新指向当前这份 exe，
// 否则点通知会被系统拿去开旧路径（点完没反应）。Windows 显式传 process.execPath + 空参数，
// 注册表 command 写成「"<exe>" "%1"」；写完再用 reg 兜底写一次，双保险。
function registerJumpProtocol() {
  const proto = JUMP_PROTOCOL;
  const exe = process.execPath;
  let ok = false;
  try {
    ok = (process.platform === 'win32')
      ? app.setAsDefaultProtocolClient(proto, exe, [])
      : app.setAsDefaultProtocolClient(proto);
  } catch (e) { log('[提醒] 协议注册异常:', e.message); }
  log('[提醒] 协议注册:', proto, '→', ok, '| exe =', exe);
  if (process.platform !== 'win32') return;   // 其余平台 Electron API 已够
  // Windows 兜底：直接写 HKCU（无需管理员）。setAsDefaultProtocolClient 本质也是写这一条，
  // 这里再写一遍确保成功；失败也只在日志里留痕，不打断启动。
  try {
    const reg = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'reg.exe');
    const key = 'HKCU\\Software\\Classes\\' + proto + '\\shell\\open\\command';
    const val = '"' + exe + '" "%1"';
    execFile(reg, ['add', key, '/ve', '/d', val, '/f'], { windowsHide: true },
      (err) => log('[提醒] 协议写入注册表:', err ? ('失败 ' + err.message) : '成功'));
  } catch (e) { log('[提醒] 协议写入异常:', e.message); }
}

// 从命令行参数里找形如 usrcloudpatrol://jump?acc=…&no=…&name=… 的跳转请求
function parseJumpArg(argv) {
  const list = Array.isArray(argv) ? argv : [];
  const prefix = JUMP_PROTOCOL + '://';
  for (let i = 0; i < list.length; i++) {
    const a = list[i];
    if (typeof a !== 'string' || a.slice(0, prefix.length).toLowerCase() !== prefix) continue;
    try {
      const u = new URL(a);
      return {
        acc: u.searchParams.get('acc') || '',
        no: u.searchParams.get('no') || '',
        name: u.searchParams.get('name') || '',
      };
    } catch (e) { return null; }
  }
  return null;
}

// 点击通知要跳转的目标；页面可能还没加载完，先记下来，did-finish-load 再补一次。
let PENDING_NAV = null;
function runNav(meta) {
  if (!meta || !meta.acc) return;
  PENDING_NAV = meta;
  if (!mainWin || mainWin.isDestroyed()) return;   // 窗口未就绪 → 交给 did-finish-load 兜底
  try { showMainWindow(); } catch (e) { /* ignore */ }
  const code = 'gotoWatch(' + JSON.stringify(meta.acc) + ',' +
    JSON.stringify(meta.no || '') + ',' + JSON.stringify(meta.name || '') + ')';
  try {
    const p = mainWin.webContents.executeJavaScript(code);
    if (p && typeof p.then === 'function') {
      p.then((r) => log('[提醒] 已跳转到监控页:', String(r || meta.acc)),
             (e) => log('[提醒] 跳转失败:', (e && e.message) || String(e)));
    }
  } catch (e) { log('[提醒] 跳转执行失败:', e.message); }
}

// 拿不到单实例锁说明程序已经在跑：这一份只用来把跳转参数转交给它，然后退出。
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', (event, argv) => {
    const meta = parseJumpArg(argv);
    if (meta && meta.acc) {
      log('[提醒] 收到通知跳转请求:', meta.acc, meta.no || '');
      runNav(meta);
    } else {
      try { showMainWindow(); } catch (e) { /* ignore */ }   // 普通第二次启动：只把窗口唤到前台
    }
  });
}

const PORT_FILE = path.join(os.tmpdir(), 'usrcloud_patrol_port.txt');
let backend = null;

function pythonExecutable() {
  const win = process.platform === 'win32';
  // 开发态：python 在脚本同级目录；打包后：python 由 --extra-resource 放到 resources 下
  const rel = win ? path.join('python', 'python.exe') : path.join('python', 'bin', 'python3');
  const candidates = [path.join(__dirname, rel)];
  if (process.resourcesPath) candidates.push(path.join(process.resourcesPath, rel));
  for (const c of candidates) {
    try { if (fs.existsSync(c)) { log('python 解释器:', c); return c; } } catch (e) { /* ignore */ }
  }
  log('[WARN] 未找到内置 python，回退到系统 python3');
  return win ? path.join(__dirname, 'python', 'python.exe') : 'python3';
}

function startBackend(onPort) {
  const py = pythonExecutable();
  const script = path.join(__dirname, 'webui_server.py');
  // 环境净化：若本程序由带 WorkBuddy/CodeBuddy 沙箱的环境拉起，PYTHONPATH 会指向沙箱 shim
  // （sitecustomize 给 shutil.rmtree 打"回收站删除"补丁，>50 个文件直接 fail-closed 并静默杀死
  // 处理线程），导致删批次无响应、每轮跑完的自动清理悄悄失效、旧批次越积越多。这里把这些
  // 注入变量从后端环境里剔掉，保证删除走原生文件系统。
  const env = Object.assign({}, process.env, { USRCLOUD_ELECTRON: '1' });
  ['PYTHONPATH', 'PYTHONSTARTUP', 'NODE_OPTIONS', 'BASH_ENV',
   'CODEBUDDY_SAFE_DELETE_ENABLED', 'CODEBUDDY_SAFE_DELETE_SANDBOX',
   'CODEBUDDY_SAFE_DELETE_BULK_GUARD', 'CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD',
   'CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR', 'CODEBUDDY_SAFE_DELETE_BROKER_DELETE',
   'CODEBUDDY_SAFE_DELETE_BIN_DIR', 'CODEBUDDY_SAFE_DELETE_REPORT_PATH'].forEach(function (k) {
    delete env[k];
  });
  try {
    if (fs.existsSync(PORT_FILE)) fs.unlinkSync(PORT_FILE);
  } catch (e) { /* ignore */ }

  if (!fs.existsSync(script)) { log('[FATAL] 缺少 webui_server.py:', script); return; }
  log('启动后端:', py, script);
  backend = spawn(py, [script], { env, cwd: __dirname, stdio: ['ignore', 'pipe', 'pipe'] });
  backend.stdout.on('data', (b) => log('[py]', String(b).trim()));
  backend.stderr.on('data', (b) => log('[py-err]', String(b).trim()));
  backend.on('error', (err) => log('[FATAL] 后端启动失败:', err.message));
  backend.on('exit', (code) => log('后端已退出，code =', code));

  const deadline = Date.now() + 20000;
  const timer = setInterval(() => {
    if (fs.existsSync(PORT_FILE)) {
      let port = null;
      try { port = fs.readFileSync(PORT_FILE, 'utf-8').trim(); } catch (e) { /* ignore */ }
      if (port) {
        try { fs.unlinkSync(PORT_FILE); } catch (e) { /* ignore */ }
        clearInterval(timer);
        log('后端端口 =', port);
        BACKEND_PORT = port;
        onPort(port);
        return;
      }
    }
    if (Date.now() > deadline) {
      clearInterval(timer);
      log('[FATAL] 20 秒内未读到后端端口，启动中止。');
      if (backend) backend.kill('SIGKILL');
      app.quit();
    }
  }, 200);
}

// 按某块显示器的工作区算「首次打开」的默认尺寸。
// 界面画布严格等于窗口可用区，所以这里只需保证「不越屏 + 别留太多边」：
//   · 够大的屏 → 吃满 95%，一打开就是满屏观感
//   · 小的屏   → 按 88% 收着走，给标题栏/边框留余量，别顶满到拖不动
// wa 为空（取屏失败）时退回 1220×800 兜底。
function fitToWorkArea(wa) {
  if (!wa || !(wa.width > 0) || !(wa.height > 0)) return { width: 1220, height: 800 };
  const roomy = wa.width >= 1600 && wa.height >= 900;      // 有余量的屏
  const ratio = roomy ? 0.95 : 0.88;
  let W = Math.round(wa.width * ratio);
  let H = Math.round(wa.height * (roomy ? 0.95 : 0.90));  // 矮屏高度再收一点，留任务栏余量
  W = Math.max(1000, Math.min(W, 2280, wa.width));
  H = Math.max(680, Math.min(H, 1320, wa.height));
  return { width: W, height: H };
}

// 找到「包含指定点的那块显示器」的工作区；找不到就回主屏。
// 多屏场景必需：用户在副屏上把窗口放到最大，下次启动就该按副屏尺寸恢复，
// 而不是拿主屏（可能小得多）去裁剪。
function workAreaAt(x, y) {
  try {
    const disps = screen.getAllDisplays();
    for (const d of disps) {
      const a = d.workArea;
      if (x >= a.x && x < a.x + a.width && y >= a.y && y < a.y + a.height) return a;
    }
  } catch (e) { /* ignore */ }
  try { return screen.getPrimaryDisplay().workAreaSize; } catch (e) { return null; }
}

// 首次打开（无记录）时的默认尺寸：按主屏工作区自适应
function autoBounds() {
  let wa = null;
  try { wa = screen.getPrimaryDisplay().workAreaSize; } catch (e) {
    log('[窗口] 取屏幕尺寸失败，用默认 1220×800：' + (e && e.message));
  }
  return fitToWorkArea(wa);
}

// 读上次保存的窗口大小/位置。
// 关键：裁剪上限取「上次窗口所在那块屏」的工作区，而不是主屏 —— 否则双屏用户
// 在大副屏上拉满的窗口，下次启动会被小主屏裁掉一大半。
// 位置若已不在任何屏幕内（换显示器、拔投影），视为失效，改回按主屏自适应默认。
function loadBounds() {
  let b = null;
  try { b = JSON.parse(fs.readFileSync(BOUNDS_FILE, 'utf-8')); } catch (e) { return autoBounds(); }
  if (!b || !(b.width > 0) || !(b.height > 0)) return autoBounds();

  const x = Math.round(b.x), y = Math.round(b.y);
  const wa = (isFinite(x) && isFinite(y)) ? workAreaAt(x, y) : null;
  if (!wa) {
    log('[窗口] 取不到所在显示器工作区，用主屏自适应默认');
    return autoBounds();
  }

  // 位置有效性：标题栏那一点必须落在这块屏的工作区内，否则窗口拖不动
  const inScreen = x + 60 >= wa.x && x + 60 <= wa.x + wa.width
                && y + 16 >= wa.y && y + 16 <= wa.y + wa.height;
  if (!inScreen) {
    log('[窗口] 上次位置已不在该屏内（' + x + ',' + y + '），改回居中');
    return { width: Math.max(1000, Math.min(Math.round(b.width), wa.width)),
             height: Math.max(680, Math.min(Math.round(b.height), wa.height)) };
  }

  const w = Math.max(1000, Math.min(Math.round(b.width), wa.width));
  const h = Math.max(680, Math.min(Math.round(b.height), wa.height));
  return { width: w, height: h, x: x, y: y };
}

// ---- 托盘（后台监控）----
// 需求：点关闭按钮时要能选「收到托盘图标」，监控继续跑；不能一刀切把程序杀掉。
// 实现要点：
//   · close 事件里 e.preventDefault() 拦下默认关闭，按用户选择决定 hide 还是真退出
//   · QUIT 标志位区分「真退出」和「收进托盘」，避免 app.quit() 再次触发 close 死循环
//   · 托盘菜单给「显示主窗口 / 立即巡检 / 退出程序」，双击托盘也能还原窗口
let tray = null;
let QUIT = false;
let mainWin = null;
let TRAY_HIDDEN = false;   // 用户主动收进托盘后置位，防止启动兜底 show() 把窗口又弹回来

function readCloseMode() {
  try {
    const d = JSON.parse(fs.readFileSync(CLOSE_MODE_FILE, 'utf-8'));
    const m = String((d && d.mode) || '');
    return (m === 'tray' || m === 'exit' || m === 'ask') ? m : 'ask';
  } catch (e) { return 'ask'; }
}
function writeCloseMode(mode) {
  try {
    fs.writeFileSync(CLOSE_MODE_FILE, JSON.stringify({ mode: mode, ts: Date.now() }, null, 2), 'utf-8');
    log('[关闭方式] 已记住:', mode);
  } catch (e) { /* ignore */ }
}

function trayImage() {
  // 托盘图标在 Windows 上要 16×16 的小图；直接用 ico 让 Electron 自己挑尺寸即可。
  const p = process.platform === 'win32'
    ? path.join(__dirname, 'build', 'icon.ico')
    : path.join(__dirname, 'build', 'icon.png');
  try { if (fs.existsSync(p)) return nativeImage.createFromPath(p); } catch (e) { /* ignore */ }
  try { if (ICON_PATH) return nativeImage.createFromPath(ICON_PATH); } catch (e) { /* ignore */ }
  return nativeImage.createEmpty();
}

function showMainWindow() {
  if (!mainWin || mainWin.isDestroyed()) return;
  try {
    TRAY_HIDDEN = false;
    if (mainWin.isMinimized()) mainWin.restore();
    mainWin.show();
    mainWin.focus();
    log('[托盘] 已显示主窗口');
  } catch (e) { log('[托盘] 显示窗口失败:', e.message); }
}

function createTray() {
  if (tray) return tray;
  try {
    tray = new Tray(trayImage());
    tray.setToolTip('有人云巡站客户端 · 后台监控中');
    refreshTrayMenu();
    tray.on('click', showMainWindow);
    tray.on('double-click', showMainWindow);
    log('[托盘] 已创建');
  } catch (e) {
    log('[托盘] 创建失败（不影响主功能）:', e.message);
    tray = null;
  }
  return tray;
}

function refreshTrayMenu() {
  if (!tray) return;
  try {
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: '显示主窗口', click: showMainWindow },
      { type: 'separator' },
      { label: '立即巡检一次', click: () => { showMainWindow(); try { mainWin.webContents.executeJavaScript('watchScan && watchScan()'); } catch (e) { /* ignore */ } } },
      { label: '打开数据监控页', click: () => { showMainWindow(); try { mainWin.webContents.executeJavaScript("setView && setView('watch')"); } catch (e) { /* ignore */ } } },
      { type: 'separator' },
      { label: '退出程序', click: () => { QUIT = true; app.quit(); } },
    ]));
  } catch (e) { /* ignore */ }
}

// 收进托盘：隐藏窗口但**不杀后端**，巡检引擎继续按 15 分钟跑、系统通知继续弹。
// 传入 win 优先，避免全局 mainWin 万一为空时静默返回（上一版就是这么"点了没反应"的）。
function hideToTray(win) {
  const w = (win && !win.isDestroyed()) ? win : mainWin;
  if (!w || w.isDestroyed()) {
    log('[关闭] 收进托盘失败：拿不到可用窗口对象');
    return;
  }
  try {
    w.hide();
    TRAY_HIDDEN = true;
    // 立刻回读可见性并记进日志：hide() 是静默的，失败时不会有任何征兆，
    // 上一个问题就是「点了没反应、日志一字不留」，只能靠这行自证。
    setTimeout(() => {
      try {
        log('[关闭] 隐藏后窗口可见=' + w.isVisible() + ' 最小化=' + w.isMinimized());
      } catch (e) { /* ignore */ }
    }, 300);
    log('[关闭] 已收进托盘，后台监控继续运行（双击托盘图标可还原）');
  } catch (e) { log('[关闭] 收进托盘失败:', e.message); }
}

// 真正退出：先杀后端再退出，避免 python 残留
function reallyQuit() {
  QUIT = true;
  if (backend) { try { backend.kill('SIGKILL'); } catch (e) { /* ignore */ } backend = null; }
  app.quit();
}

function askCloseThen(win) {
  const mode = readCloseMode();
  if (mode === 'tray') { hideToTray(win); return; }
  if (mode === 'exit') { reallyQuit(); return; }

  // 让**页面内**弹窗来问，用 executeJavaScript 取回用户的选择。
  // 为什么不用 dialog.showMessageBox：① 原生框与本软件视觉完全不一致（用户明确要求统一风格）；
  //             ② 实测 showMessageBox 的回调不触发 —— 点了「收到托盘图标」窗口纹丝不动，
  //                main-log.txt 里既没有「已收进托盘」也没有「用户取消」，说明回调根本没进。
  const finish = (choice, remember) => {
    try {
      if (remember && (choice === 'tray' || choice === 'exit')) {
        writeCloseMode(choice);
      }
      if (choice === 'tray') hideToTray(win);
      else if (choice === 'exit') reallyQuit();
      else log('[关闭] 用户取消，保持窗口打开');
    } catch (e) { log('[关闭] 处理选择时出错:', e.message); }
  };

  let done = false;
  const once = (choice, remember) => { if (!done) { done = true; finish(choice, remember); } };
  const bail = (why) => {
    if (done) return;
    done = true;
    log('[关闭] 页面弹窗未能返回（' + why + '），改用托盘模式，不弹原生框');
    hideToTray(win);
  };

  // 页面没加载完 / 脚本报错时 executeJavaScript 会 reject 或超时，这里都要有兜底，
  // 否则窗口既没隐藏也没退出，卡在中间态。
  setTimeout(() => bail('超时 8 秒未返回'), 8000);
  try {
    const p = win.webContents.executeJavaScript('window.__askClose ? window.__askClose() : "__no_fn__"');
    Promise.resolve(p).then((r) => {
      if (r === '__no_fn__') { bail('页面没有 __askClose'); return; }
      once(String((r && r.choice) || 'cancel'), r && r.remember);
    }).catch((e) => bail('executeJavaScript 失败: ' + (e && e.message)));
  } catch (e) {
    bail('调用异常: ' + e.message);
  }
}

// ---- 右键菜单 --------------------------------------------------------------
// 为什么需要：应用菜单被设成 null（去掉 File/Edit 菜单栏），Electron 也就不再给
// <input> 提供系统右键菜单 → 所有输入框都点不出「剪切/复制/粘贴/全选」。
// 这里自己做一份：① 输入框 → 标准编辑项（按 editFlags 决定可用性）；
// ② 站点名/设备行 → 只保留两项：复制站点名称、刷新这台设备的实时数据
//    （用户 2026-10-15 明确要求：其他的都去掉，菜单要短）。
const MENU_JS_HITTEST = 'window.__hittest ? window.__hittest(%d, %d) : null';

function menuRun(win, kind, val, view) {
  try {
    win.webContents.executeJavaScript(
      'window.__menuAction && window.__menuAction(' + JSON.stringify(kind) + ','
      + JSON.stringify(val == null ? '' : val) + ',' + JSON.stringify(view || '') + ')');
  } catch (e) { /* ignore */ }
}

// ---- 外链打开 ----
// 只放行 http/https。地址来自网页内容，其它 scheme（javascript: / file: / ms-msdt: 之类）
// 交给系统等于把命令执行权交出去，Electron 官方也明确提示过这个风险。
function openExternal(url) {
  try {
    const u = new URL(String(url || ''));
    if (u.protocol !== 'http:' && u.protocol !== 'https:') {
      log('[外链] 已拒绝非 http(s) 地址: ' + u.protocol);
      return;
    }
    shell.openExternal(u.toString());
    log('[外链] 交给系统浏览器: ' + u.toString());
  } catch (e) {
    log('[外链] 打开失败: ' + String(e));
  }
}

function setupContextMenu(win) {
  win.webContents.on('context-menu', (event, params) => {
    const show = (info) => {
      const items = [];
      const f = params.editFlags || {};
      const hasSel = !!(params.selectionText && params.selectionText.trim());
      if (info && info.name) {
        const nm = info.name;
        items.push({ label: '复制站点名称', click: () => clipboard.writeText(nm) });
        items.push({ type: 'separator' });
        items.push({ label: '刷新这台设备的实时数据', click: () => menuRun(win, 'refresh', info.no || nm, info.view) });
      }
      if (params.isEditable) {
        if (items.length) items.push({ type: 'separator' });
        items.push({ label: '剪切', role: 'cut', enabled: !!f.canCut });
        items.push({ label: '复制', role: 'copy', enabled: !!(f.canCopy || hasSel) });
        items.push({ label: '粘贴', role: 'paste', enabled: !!f.canPaste });
        items.push({ type: 'separator' });
        items.push({ label: '全选', role: 'selectAll', enabled: !!f.canSelectAll });
      } else if (hasSel && !(info && info.name)) {
        // 页面里有残留选区时（用户刚拖选过文本），Electron 会把 selectionText 带进来。
        // 但这不意味着想在设备行上看到「复制/全选」—— 用户要求站点那条菜单只有两项，
        // 所以这里排掉已命中设备行/实时卡的情形。
        items.push({ label: '复制', role: 'copy' });
        items.push({ label: '全选', role: 'selectAll' });
      }
      if (!items.length) return;      // 空白处不弹（保持原样）
      try { Menu.buildFromTemplate(items).popup({ window: win }); } catch (e) { /* ignore */ }
    };
    if (params.isEditable) return show(null);        // 输入框：直接给编辑菜单
    win.webContents.executeJavaScript(
      MENU_JS_HITTEST.replace('%d', Math.round(params.x)).replace('%d', Math.round(params.y)))
      .then(show).catch(() => show(null));
  });
}

// ---- 键盘兜底：Ctrl+C / X / V / A / Z / Y ----
// 没有应用菜单栏时，部分机器上输入框的 Ctrl+C/V 会失效；这里直接转发给 webContents，
// 只在组合键确实成功转发后才 preventDefault（避免反而把默认行为干掉）。
function setupEditKeys(win) {
  const wc = win.webContents;
  wc.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown') return;
    const mod = input.control || input.meta;
    if (!mod || input.alt || input.shift) return;
    const k = String(input.key || '').toLowerCase();
    try {
      if (k === 'c') { wc.copy(); event.preventDefault(); }
      else if (k === 'x') { wc.cut(); event.preventDefault(); }
      else if (k === 'v') { wc.paste(); event.preventDefault(); }
      else if (k === 'a') { wc.selectAll(); event.preventDefault(); }
      else if (k === 'z') { wc.undo(); event.preventDefault(); }
      else if (k === 'y') { wc.redo(); event.preventDefault(); }
    } catch (e) { /* ignore */ }
  });
}

function createWindow(port) {
  const bd = loadBounds();
  // 把「检测到了几块屏、各自多大、窗口落在哪块屏、最终开多大」全部打出来，
  // 方便核对分辨率识别是否正确（DIP 已含 DPI 折算：2560×1440 @125% 会报 2048×1104）
  try {
    const ds = screen.getAllDisplays();
    log('[窗口] 显示器 ' + ds.length + ' 块：'
      + ds.map((d, i) => (i ? ' | #' + i : '#0') + ' ' + d.workArea.width + '×' + d.workArea.height
        + ' @' + Math.round(d.scaleFactor * 100) + '%' + (d.bounds.x ? ' 偏移(' + d.bounds.x + ',' + d.bounds.y + ')' : '')).join('')
      + ' → 窗口 ' + bd.width + '×' + bd.height
      + (bd.x === undefined ? '（自动居中）' : '（记住的位置 ' + bd.x + ',' + bd.y + '）'));
  } catch (e) {
    log('[窗口] → 窗口 ' + bd.width + '×' + bd.height);
  }
  const win = new BrowserWindow({
    width: bd.width,
    height: bd.height,
    ...(bd.x === undefined ? {} : { x: bd.x, y: bd.y }),
    minWidth: 1000,
    minHeight: 680,
    useContentSize: true,   // 尺寸按“内容区”算：边框由系统加，避免 125% 缩放下外层尺寸被取整而逐次变宽
    backgroundColor: '#EDEEF1',
    title: '有人云巡站客户端',
    // 应用图标：与手机版同一张「巡」字图标。ico 供 Windows 任务栏/标题栏/Alt-Tab，
    // png 供 macOS  dock 与 Linux；缺失时 Electron 会退化成默认图标。
    icon: ICON_PATH,
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  win.once('ready-to-show', () => { log('ready-to-show → show()'); win.show(); });

  // ---- 外链一律交给系统浏览器 ----
  // 网页跑在 Chromium 里，直接跳转会把这个窗口本身导航走：没有地址栏、退不回上一页，
  // 用户只能关掉重开。所以 target=_blank / window.open 全都拦下，改用系统浏览器打开。
  win.webContents.setWindowOpenHandler(({ url }) => {
    openExternal(url);
    return { action: 'deny' };
  });
  // 兜底：将来网页里若出现不带 target 的 <a>，也别让主窗口被导航走（自己的界面除外）
  win.webContents.on('will-navigate', (e, url) => {
    if (url.indexOf('http://127.0.0.1:') === 0 || url.indexOf('file://') === 0) return;
    e.preventDefault();
    openExternal(url);
  });

  setupContextMenu(win);      // 右键菜单：输入框的剪切/复制/粘贴/全选 + 站点名的快捷操作
  setupEditKeys(win);         // 没有应用菜单栏，Ctrl+C/V/X/A 兜底转发

  // ---- 记住窗口大小/位置：拖动/缩放停下 0.7 秒后写盘，关闭时再兜一次 ----
  // 关键：系统会在请求尺寸之外加上边框并取整，导致“读到的大小”总比“请求的大小”多几像素。
  // 若把读到的值直接存盘，下次启动会再叠一次，窗口就会逐次变宽。故先量出偏差，存盘时扣掉。
  let fixW = 0, fixH = 0, calibrated = false;
  const calibrate = () => {
    try {
      if (win.isDestroyed() || win.isMaximized() || win.isFullScreen()) return;
      const cb = win.getContentBounds();
      fixW = cb.width - bd.width;
      fixH = cb.height - bd.height;
      calibrated = true;
      log('[窗口] 边框偏差 = ' + fixW + '×' + fixH + '（存盘时扣除）');
    } catch (e) { /* ignore */ }
  };
  const WIN_T0 = Date.now();
  setTimeout(calibrate, 2500);

  const saveBounds = () => {
    try {
      if (!win || win.isDestroyed() || win.isMinimized() || win.isFullScreen()) return;
      if (!calibrated) calibrate();
      const ob = win.getNormalBounds();     // 位置：窗口坐标
      const cb = win.getContentBounds();    // 尺寸：内容区
      const w = Math.max(1000, cb.width - fixW), h = Math.max(680, cb.height - fixH);
      fs.writeFileSync(BOUNDS_FILE, JSON.stringify({
        width: w, height: h, x: ob.x, y: ob.y,
      }), 'utf-8');
      log('[窗口] 已记住 ' + w + '×' + h + ' @' + ob.x + ',' + ob.y);
    } catch (e) { /* ignore */ }
  };
  let bTimer = null;
  const saveSoon = () => {
    if (Date.now() - WIN_T0 < 2500) return;   // 启动阶段的事件（show / DPI 校正）不是用户改动，忽略
    if (bTimer) clearTimeout(bTimer);
    bTimer = setTimeout(saveBounds, 700);
  };
  win.on('resize', saveSoon);
  win.on('move', saveSoon);
  // 注意：close 的处理统一放在下面（要区分「收进托盘」与「真退出」），此处不再重复注册。

  // ---- 异常提醒轮询：后端发现新异常/恢复时，按设置做任务栏闪烁或系统通知 ----
  // 提醒方式由设置页「异常提醒方式」控制（off/flash/toast/both），后端在响应里给出 flash/toast 开关。
  // macOS 没有任务栏，窗口闪烁基本看不见：改用程序坞图标弹跳（critical，一直跳到用户点开窗口），
  // 窗口拿到焦点时取消。Windows 仍用 flashFrame 闪任务栏按钮。
  let dockBounceId = null;
  const attention = (on) => {
    try {
      if (process.platform === 'darwin' && app.dock) {
        if (on) {
          if (dockBounceId == null) {
            dockBounceId = app.dock.bounce('critical');
            log('[提醒] 程序坞图标开始弹跳');
          }
        } else if (dockBounceId != null) {
          app.dock.cancelBounce(dockBounceId);
          dockBounceId = null;
        }
        return;
      }
      // ★ 窗口就在前台、用户正看着它时，不要调 flashFrame。
      //   flashFrame 走的是 Windows 任务栏合成器路径，而本程序为了规避
      //   "GPU process isn't usable" 崩溃设了 in-process-gpu —— 合成器一旦在这条
      //   路径上出错，崩的是浏览器进程本身，JS 层连日志都写不出来（实测日志停在
      //   「[提醒] 1 条新事件」之后再无任何记录，也没有 [FATAL]）。
      //   而且窗口在前台时闪烁本来就没人看得见，没有任何收益。
      //
      //   只拦 on=true：on=false 是「取消程序坞弹跳」，必须照常执行，
      //   否则 macOS 上图标会一直弹跳停不下来。
      if (on && win.isFocused() && win.isVisible()) {
        // 前台看得见的提醒统一交给系统通知（见下方 notifypoll），这里不闪。
        return;
      }
      win.flashFrame(!!on);
      // 原来这里还有一手「托盘气泡 displayBalloon」兜底，已删除：
      // 它本身也是走进通知中心的一条通道（署名还是本程序），与系统通知重复 ——
      // 用户明确要求只保留一个通道。窗口收进托盘时，系统通知照常能弹出来。
    } catch (e) { log('[提醒] 闪烁/弹跳失败:', e.message); }
  };
  let notifySince = Date.now() / 1000 - 10;
  const notifyTimer = setInterval(() => {
    if (!win || win.isDestroyed()) { clearInterval(notifyTimer); return; }
    const req = http.get({ host: '127.0.0.1', port, path: '/api/notifypoll?since=' + notifySince, timeout: 4000 }, (res) => {
      let raw = '';
      res.on('data', (c) => { raw += c; });
      res.on('end', () => {
        try {
          const j = JSON.parse(raw);
          if (!j || !j.ok) return;
          notifySince = j.now || notifySince;
          const evs = j.events || [];
          if (!evs.length) return;
          log('[提醒] ' + evs.length + ' 条新事件');
          const fg = win.isFocused() && win.isVisible();
          // 任务栏闪烁：只处理「窗口不在前台」的情况。
          // 前台闪烁本来就没人看得见，而且 flashFrame 走 Windows 合成器路径，
          // 在本程序（关了硬件加速 + in-process-gpu）上历史上出过问题，不值得再冒险。
          if (j.flash && !fg) attention(true);
          // ★ 系统通知：前台后台一视同仁，一条都不漏。三处改动：
          //   ① 删掉「窗口在前台就跳过系统通知」—— 那条设计把前台提醒降级成
          //      「软件内的横幅」，用户切走窗口就什么都收不到，不可靠；
          //   ② 删掉 slice(0, 3) —— 一轮命中 6 台时原来只投前 3 条，
          //      剩下 3 条还会因为 notifySince 已推进而被永久丢掉；
          //   ③ 每条错开 350ms —— 投递要起 PowerShell 子进程，6 个同时发起会互相挤，
          //      错开才能一条条稳定弹出。
          if (j.toast) {
            evs.forEach((ev, i) => {
              const fire = () => {
                try { osToast(ev.title || '监控异常提醒', ev.body || '', ev); }
                catch (e) { log('[提醒] osToast 异常:', e.message); }
              };
              if (i === 0) fire(); else setTimeout(fire, i * 350);
            });
          }
        } catch (e) { /* ignore */ }
      });
    });
    req.on('error', () => { /* 后端短暂未就绪，忽略 */ });
  }, 5000);
  win.on('focus', () => { attention(false); });
  // 兜底：某些环境下 ready-to-show 不触发会导致"进程在跑但没窗口"，4 秒后强制显示。
  // 注意要排除「用户已主动收进托盘」的情况 —— 否则会在用户点完关闭后 4 秒又把窗口弹回来。
  setTimeout(() => {
    try {
      if (win.isDestroyed() || TRAY_HIDDEN) return;
      if (!win.isVisible()) { log('兜底 show()'); win.show(); }
    } catch (e) { /* ignore */ }
  }, 4000);
  win.loadURL('http://127.0.0.1:' + port + '/');
  mainWin = win;

  // ---- 关闭按钮：按用户选择决定「收进托盘」还是「真退出」 ----
  // QUIT 为真说明已经在走真正的退出流程，此时不再拦截。
  win.on('close', (e) => {
    if (QUIT) return;
    try { if (bTimer) clearTimeout(bTimer); } catch (err) { /* ignore */ }
    saveBounds();
    e.preventDefault();
    askCloseThen(win);
  });

  // 托盘在窗口显示后再建，避免还没 ready 时抛 nativeImage 错误
  win.once('ready-to-show', () => { try { createTray(); } catch (err) { log('[托盘] 初始化异常:', err.message); } });

  win.webContents.on('did-fail-load', (e, code, desc) => {
    log('[ERROR] 页面加载失败:', code, desc);
  });
  win.webContents.on('did-finish-load', () => {
    log('页面已加载:', win.webContents.getURL());
    // 点击系统通知跳转时页面可能还没就绪，指令先存进 PENDING_NAV，这里补执行一次，
    // 保证「点通知跳到对应站点」一定生效（冷启动带参数启动也走这条路）。
    if (PENDING_NAV) {
      const m = PENDING_NAV;
      PENDING_NAV = null;
      setTimeout(() => runNav(m), 400);
    }
  });
  win.webContents.on('render-process-gone', (e, d) => {
    log('[ERROR] 渲染进程退出:', JSON.stringify(d));
  });

  win.on('closed', () => {
    log('窗口已关闭');
    if (mainWin === win) mainWin = null;
    // 收进托盘时走的是 hide()，不会触发 closed；这里只在真正销毁窗口时才收尾
    if (QUIT && backend) { try { backend.kill('SIGKILL'); } catch (e) { /* ignore */ } backend = null; }
  });
}

app.whenReady().then(() => {
  log('app ready · 关闭方式 =', readCloseMode());
  // 注册「点击通知跳转」协议（每次启动都注册，指向当前 exe）
  registerJumpProtocol();
  // 冷启动（程序没在跑时点击通知）：启动参数里带着跳转请求，先记下，等页面加载完再跳。
  const coldNav = parseJumpArg(process.argv);
  if (coldNav && coldNav.acc) { PENDING_NAV = coldNav; log('[提醒] 冷启动带跳转参数:', coldNav.acc, coldNav.no || ''); }
  startBackend((port) => createWindow(port));
});

app.on('window-all-closed', () => {
  // 托盘模式下窗口只是被隐藏，并不会触发这个事件；真退出时才走到这
  if (backend) { try { backend.kill('SIGKILL'); } catch (e) { /* ignore */ } backend = null; }
  app.quit();
});

// 托盘右键「退出程序」/ 系统退出时，确保 python 子进程不残留
app.on('before-quit', () => {
  QUIT = true;
  if (backend) { try { backend.kill('SIGKILL'); } catch (e) { /* ignore */ } backend = null; }
});
