// 渲染模式判定（硬件加速 / 软件渲染）—— 纯函数，不碰 Electron、不碰磁盘。
//
// 为什么单独成一个文件：这段逻辑决定「这次启动用不用硬件加速」，一旦判错就是
// 程序打不开或白屏，所以必须能脱离 Electron 单独跑断言（verify/test_gpu_mode.js）。
// main.js 只负责读盘、调用这里的 decide()、把返回的 state 写回盘。
//
// 状态字段（渲染模式.json）：
//   mode        'hw' 硬件加速 / 'sw' 软件渲染（缺省 = hw）
//   boot        本次硬件模式启动的时间戳；「启动成功」后清空。
//               下次启动若发现它还在 = 上次硬件模式死在半路（GPU 一起来就崩，
//               JS 连日志都写不出来），直接降级。
//   fail        连续硬件失败次数（仅用于记录/排查，不参与判定）
//   swSince     进入软件渲染的时间；自愈计时从这里算
//   retryDays   软件模式撑多久再试一次硬件（失败一次翻倍，有上限）
//   note        最近一次判定的原因原文，给人看
'use strict';

var DAY = 24 * 3600 * 1000;

var EMPTY = { mode: 'hw', boot: null, fail: 0, swSince: null, retryDays: null, note: '' };

// 把盘上读到的任何东西规整成合法状态：文件缺失、被改坏、写成字符串都当「首次启动」
function normState(raw) {
  var s = { mode: EMPTY.mode, boot: EMPTY.boot, fail: EMPTY.fail,
            swSince: EMPTY.swSince, retryDays: EMPTY.retryDays, note: EMPTY.note };
  if (!raw || typeof raw !== 'object') return s;
  if (raw.mode === 'sw') s.mode = 'sw';
  var boot = Number(raw.boot), fail = Number(raw.fail);
  var sw = Number(raw.swSince), rd = Number(raw.retryDays);
  if (isFinite(boot) && boot > 0) s.boot = boot;
  if (isFinite(fail) && fail > 0) s.fail = Math.floor(fail);
  if (isFinite(sw) && sw > 0) s.swSince = sw;
  if (isFinite(rd) && rd > 0) s.retryDays = Math.floor(rd);
  if (typeof raw.note === 'string') s.note = raw.note;
  return s;
}

// 判定这一次启动用哪种模式。
// opts: { now, argv, defaultRetryDays, maxRetryDays }
// 返回: { mode: 'hw'|'sw', event: 'boot-attempt'|'boot-failed'|'retry'|'sw-hold'|'sw-forced',
//         reason: '中文原因', state: 要写回盘的状态 }
function decide(raw, opts) {
  var o = opts || {};
  var now = isFinite(Number(o.now)) && Number(o.now) > 0 ? Number(o.now) : Date.now();
  var argv = o.argv || [];
  var base = isFinite(Number(o.defaultRetryDays)) && Number(o.defaultRetryDays) > 0
    ? Math.floor(Number(o.defaultRetryDays)) : 14;
  var max = isFinite(Number(o.maxRetryDays)) && Number(o.maxRetryDays) > 0
    ? Math.floor(Number(o.maxRetryDays)) : 60;
  var s = normState(raw);
  var next = Object.assign({}, s);

  // ① 命令行强制（本程序崩溃后自动重启时自己加的）—— 最高优先级
  if (argv.indexOf('--software-render') >= 0) {
    next.mode = 'sw';
    next.boot = null;
    if (!next.swSince) next.swSince = now;
    next.note = '启动参数要求软件渲染';
    return { mode: 'sw', event: 'sw-forced', reason: next.note, state: next };
  }

  // ② 本来就在软件模式：撑满 retryDays 才试一次硬件，否则继续软件
  if (s.mode === 'sw') {
    var days = Math.max(1, s.retryDays || base);
    if (s.swSince && now - s.swSince >= days * DAY) {
      next.mode = 'hw';
      next.boot = now;
      next.swSince = null;
      next.retryDays = Math.min(max, days * 2);
      next.note = '软件渲染已满 ' + days + ' 天，再试一次硬件加速（再失败就等 ' + next.retryDays + ' 天）';
      return { mode: 'hw', event: 'retry', reason: next.note, state: next };
    }
    next.mode = 'sw';
    next.boot = null;
    if (!next.swSince) next.swSince = now;
    next.note = '此前硬件加速崩溃过，继续软件渲染';
    return { mode: 'sw', event: 'sw-hold', reason: next.note, state: next };
  }

  // ③ 硬件模式，但上次的 boot 标记没被清掉 = 上次启动死在半路
  if (s.boot) {
    next.mode = 'sw';
    next.boot = null;
    next.swSince = now;
    next.fail = s.fail + 1;
    next.note = '上次硬件加速启动没走到成功标记就中断了，改为软件渲染';
    return { mode: 'sw', event: 'boot-failed', reason: next.note, state: next };
  }

  // ④ 正常：这次试硬件，先立 boot 标记
  next.mode = 'hw';
  next.boot = now;
  next.note = '默认硬件加速，等待启动确认';
  return { mode: 'hw', event: 'boot-attempt', reason: next.note, state: next };
}

// 启动成功确认：清掉 boot 标记，把失败计数与自愈期限复位
function afterHealthyBoot(raw, defaultRetryDays) {
  var base = isFinite(Number(defaultRetryDays)) && Number(defaultRetryDays) > 0
    ? Math.floor(Number(defaultRetryDays)) : 14;
  var s = normState(raw);
  s.mode = 'hw';
  s.boot = null;
  s.fail = 0;
  s.swSince = null;
  s.retryDays = base;
  s.note = '硬件加速启动正常';
  return s;
}

// 运行中反复崩 → 当场降级（下一次启动生效）
function afterCrashDowngrade(raw, now) {
  var s = normState(raw);
  var t = isFinite(Number(now)) && Number(now) > 0 ? Number(now) : Date.now();
  s.mode = 'sw';
  s.boot = null;
  s.swSince = t;
  s.fail = s.fail + 1;
  s.note = '运行中 GPU 进程反复崩溃，改为软件渲染';
  return s;
}

module.exports = {
  DAY: DAY,
  EMPTY: EMPTY,
  normState: normState,
  decide: decide,
  afterHealthyBoot: afterHealthyBoot,
  afterCrashDowngrade: afterCrashDowngrade,
};
