'use strict';

// Purely local behavioral test: fake fetch only, no HTTP listener or network I/O.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(
  path.join(__dirname, '..', 'ui_v2', 'core', 'api.js'),
  'utf8'
);

let nextTimer = 0;
const timers = new Map();
const delays = [];
const cleared = [];
const calls = [];

function fakeSetTimeout(callback, delay) {
  const id = ++nextTimer;
  timers.set(id, callback);
  delays.push({ id, delay });
  return id;
}

function fakeClearTimeout(id) {
  cleared.push(id);
  timers.delete(id);
}

function fakeFetch(url, options) {
  calls.push({ url, options });
  if (url === '/ok') {
    return Promise.resolve({
      ok: true,
      status: 200,
      json: function () { return Promise.resolve({ ok: true }); }
    });
  }
  return new Promise(function (_resolve, reject) {
    options.signal.addEventListener('abort', function () {
      const error = new Error('aborted');
      error.name = 'AbortError';
      reject(error);
    }, { once: true });
  });
}

const sandbox = {
  window: {},
  document: { hidden: false },
  AbortController,
  fetch: fakeFetch,
  setTimeout: fakeSetTimeout,
  clearTimeout: fakeClearTimeout,
  console
};
vm.runInNewContext(source, sandbox, { filename: 'ui_v2/core/api.js' });

async function fireLatestTimerAndExpectTimeout(promise) {
  const record = delays[delays.length - 1];
  assert(record, 'request must install a timeout timer');
  const callback = timers.get(record.id);
  assert(callback, 'timeout timer must still be active before abort');
  callback();
  await assert.rejects(promise, function (error) {
    return error && error.name === 'TimeoutError' && error.code === 'REQUEST_TIMEOUT';
  });
  assert.strictEqual(calls[calls.length - 1].options.signal.aborted, true);
  return record;
}

async function main() {
  const getPromise = sandbox.window.LW.api.get('/slow', { retry: 0 });
  const getTimer = await fireLatestTimerAndExpectTimeout(getPromise);
  assert.strictEqual(getTimer.delay, 8000, 'GET default timeout must be 8 seconds');

  const postPromise = sandbox.window.LW.api.post('/post-slow', { value: 1 });
  const postTimer = await fireLatestTimerAndExpectTimeout(postPromise);
  assert.strictEqual(postTimer.delay, 8000, 'POST default timeout must be 8 seconds');
  assert.strictEqual(calls[calls.length - 1].options.method, 'POST');

  const ok = await sandbox.window.LW.api.get('/ok', { retry: 0, bypass: true, timeout: 25 });
  assert.strictEqual(ok.ok, true);
  const okTimer = delays[delays.length - 1];
  assert.strictEqual(okTimer.delay, 25);
  assert(cleared.includes(okTimer.id), 'successful request must clear its abort timer');
  assert.strictEqual(calls[calls.length - 1].options.signal.aborted, false);

  console.log('system_live api contract: PASS');
}

main().catch(function (error) {
  console.error(error);
  process.exitCode = 1;
});
