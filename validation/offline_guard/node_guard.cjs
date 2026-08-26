'use strict';

if (process.env.DSHQ_OFFLINE_GUARD === '1') {
  const blocked = function blockedNetwork() {
    const error = new Error('DSHQ_OFFLINE_NETWORK_BLOCKED');
    error.code = 'DSHQ_OFFLINE_NETWORK_BLOCKED';
    throw error;
  };
  const net = require('node:net');
  const tls = require('node:tls');
  const dgram = require('node:dgram');
  const dns = require('node:dns');
  const childProcess = require('node:child_process');

  net.Socket.prototype.connect = blocked;
  net.connect = blocked;
  net.createConnection = blocked;
  tls.connect = blocked;
  dgram.Socket.prototype.bind = blocked;
  dgram.Socket.prototype.connect = blocked;
  dgram.Socket.prototype.send = blocked;
  dns.lookup = blocked;
  dns.resolve = blocked;
  childProcess.spawn = blocked;
  childProcess.spawnSync = blocked;
  childProcess.exec = blocked;
  childProcess.execSync = blocked;
  childProcess.execFile = blocked;
  childProcess.execFileSync = blocked;
  childProcess.fork = blocked;
}
