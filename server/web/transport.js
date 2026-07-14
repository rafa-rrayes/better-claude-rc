/* Transport abstraction — the app talks to exactly this interface:
     send(obj) -> bool          fire-and-forget control message
     rpc(method, params) -> Promise<result>
     openTerm(sid) -> WebSocket-like (binary out, JSON control in)
     onMessage(cb), onStatus(cb)
     connect(), close()
   LocalTransport wraps today's /ws + /ws/term (cookie auth, same origin).
   RelayTransport (platform mode: mux + E2E crypto) lands in a later
   milestone; the app never learns the difference. Classic script, no
   bundler — loaded before app.js. */
"use strict";

const RPC_TIMEOUT_MS = 30000;

class LocalTransport {
  constructor() {
    this.up = false;
    this.ws = null;
    this._retry = 0;
    this._msgCbs = [];
    this._statusCbs = [];
    this._pending = new Map(); // rpc id -> {resolve, reject, timer}
    this._sendQueue = [];      // frames waiting for the socket to open
    this._nextId = 1;
    this._retryTimer = null;
    setInterval(() => {
      if (this.ws && this.ws.readyState === 1) this.ws.send('{"type":"ping"}');
    }, 25000);
  }

  connect() {
    if (this.ws && (this.ws.readyState === 0 || this.ws.readyState === 1)) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    this.ws = ws;

    ws.onopen = () => {
      this.up = true;
      this._retry = 0;
      const q = this._sendQueue;
      this._sendQueue = [];
      for (const frame of q) ws.send(frame);
      this._emitStatus({ up: true });
    };
    ws.onmessage = (e) => {
      let msg; try { msg = JSON.parse(e.data); } catch { return; }
      if (msg.type === "rpc_result") { this._settle(msg); return; }
      for (const cb of this._msgCbs) cb(msg);
    };
    ws.onclose = (e) => {
      if (this.ws !== ws) return;
      this.up = false;
      this._failPending(new Error(e.code === 4401 ? "unauthorized" : "connection lost"));
      if (e.code === 4401) {
        this._emitStatus({ up: false, authRequired: true });
        return;
      }
      this._emitStatus({ up: false });
      clearTimeout(this._retryTimer);
      this._retryTimer = setTimeout(() => this.connect(),
        Math.min(8000, 500 * 2 ** this._retry++));
    };
    ws.onerror = () => ws.close();
  }

  send(obj) {
    if (this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify(obj));
      return true;
    }
    this.connect();
    return false;
  }

  rpc(method, params) {
    return new Promise((resolve, reject) => {
      const id = this._nextId++;
      const timer = setTimeout(() => {
        this._pending.delete(id);
        reject(new Error("rpc timeout: " + method));
      }, RPC_TIMEOUT_MS);
      this._pending.set(id, { resolve, reject, timer });
      const frame = JSON.stringify({ type: "rpc", id, method, params: params || {} });
      if (this.ws && this.ws.readyState === 1) this.ws.send(frame);
      else { this._sendQueue.push(frame); this.connect(); }
    });
  }

  _settle(msg) {
    const p = this._pending.get(msg.id);
    if (!p) return;
    this._pending.delete(msg.id);
    clearTimeout(p.timer);
    if (msg.ok) p.resolve(msg.result);
    else p.reject(new Error((msg.error && msg.error.message) || "rpc failed"));
  }

  _failPending(err) {
    for (const p of this._pending.values()) { clearTimeout(p.timer); p.reject(err); }
    this._pending.clear();
    this._sendQueue = [];
  }

  openTerm(sid) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return new WebSocket(`${proto}://${location.host}/ws/term/${encodeURIComponent(sid)}`);
  }

  onMessage(cb) { this._msgCbs.push(cb); }
  onStatus(cb) { this._statusCbs.push(cb); }
  _emitStatus(st) { for (const cb of this._statusCbs) cb(st); }

  close() {
    clearTimeout(this._retryTimer);
    const ws = this.ws;
    this.ws = null;
    this.up = false;
    if (ws) {
      ws.onclose = null; ws.onmessage = null; ws.onerror = null;
      try { ws.close(); } catch {}
    }
    this._failPending(new Error("transport closed"));
  }
}

/* Mode chosen at boot: GET /api/env -> {"mode":"local"|"platform"}. */
async function makeTransport() {
  let mode = "local";
  try {
    const env = await fetch("/api/env").then((r) => r.json());
    if (env && env.mode) mode = env.mode;
  } catch {}
  // "platform" -> RelayTransport in a later milestone; local is the default.
  return new LocalTransport();
}
