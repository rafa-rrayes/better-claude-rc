/* Transport abstraction — the app talks to exactly this interface:
     send(obj) -> bool          fire-and-forget control message
     rpc(method, params) -> Promise<result>
     openTerm(sid) -> WebSocket-like (binary out, JSON control in)
     onMessage(cb), onStatus(cb)
     connect(), close()
   LocalTransport wraps today's /ws + /ws/term (cookie auth, same origin).
   RelayTransport (platform mode) multiplexes everything over one WSS to
   the relay: 6-byte envelope frames, CTRL JSON on channel 0, DATA frames
   [1B ptype][body] through a PayloadCodec — NullCodec (plaintext) until
   M4 swaps in the E2E secretstream codec. The app never learns the
   difference. Classic script, no bundler — loaded before app.js. */
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

/* ---------------- relay wire format (plan §wire format) ----------------
   byte 0 ver=1 | byte 1 kind (0 CTRL, 1 HS, 2 DATA) | bytes 2-5 ch u32 BE.
   DATA payload plaintext: [1B ptype: 0 raw | 1 json][body]. */

const MUX = { VER: 1, CTRL: 0, HS: 1, DATA: 2, PT_RAW: 0, PT_JSON: 1 };
const _enc = new TextEncoder();
const _dec = new TextDecoder();

function muxFrame(kind, ch, payload) {
  const buf = new Uint8Array(6 + payload.length);
  buf[0] = MUX.VER; buf[1] = kind;
  buf[2] = (ch >>> 24) & 255; buf[3] = (ch >>> 16) & 255;
  buf[4] = (ch >>> 8) & 255; buf[5] = ch & 255;
  buf.set(payload, 6);
  return buf;
}

function muxParse(data) { // ArrayBuffer -> {kind, ch, payload} | null
  const b = new Uint8Array(data);
  if (b.length < 6 || b[0] !== MUX.VER) return null;
  return {
    kind: b[1],
    ch: ((b[2] << 24) | (b[3] << 16) | (b[4] << 8) | b[5]) >>> 0,
    payload: b.subarray(6),
  };
}

/* NullCodec: plaintext passthrough. M4 replaces this with the secretstream
   codec (same seal/open interface; header is the AEAD associated data). */
const NullCodecJS = {
  seal(header, plaintext) { return plaintext; },
  open(header, ciphertext) { return ciphertext; },
};

function dataJsonStr(ch, str, codec) {
  const body = _enc.encode(str);
  const pl = new Uint8Array(1 + body.length);
  pl[0] = MUX.PT_JSON; pl.set(body, 1);
  const frame = muxFrame(MUX.DATA, ch, codec.seal(null, pl));
  return frame;
}

class RelayTransport {
  constructor(machineId, codec) {
    this.machine = machineId;
    this.codec = codec || NullCodecJS;
    this.up = false;          // ctrl channel open AND machine online
    this.online = false;      // last machine_status
    this.plan = null;
    this.ws = null;
    this.ctrlCh = null;
    this._ctrlPending = false;
    this._retry = 0;
    this._retryTimer = null;
    this._msgCbs = [];
    this._statusCbs = [];
    this._pending = new Map();  // rpc id -> {resolve, reject, timer}
    this._sendQueue = [];       // control objects awaiting the ctrl channel
    this._pendingOpens = [];    // FIFO of {kind, sock?, sid?} awaiting opened
    this._termSocks = new Map(); // ch -> fake WebSocket
    this._nextId = 1;
    setInterval(() => {
      if (this.up) this.send({ type: "ping" });
    }, 25000);
  }

  connect() {
    if (this.ws && (this.ws.readyState === 0 || this.ws.readyState === 1)) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(
      `${proto}://${location.host}/client/ws?machine=${encodeURIComponent(this.machine)}`);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => {
      ws.send(muxFrame(MUX.CTRL, 0, _enc.encode(JSON.stringify({ type: "hello", proto: 1 }))));
    };
    ws.onmessage = (e) => this._onFrame(e.data);
    ws.onclose = (e) => {
      if (this.ws !== ws) return;
      this._teardown(e.code === 4401
        ? new Error("unauthorized") : new Error("connection lost"));
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

  _teardown(err) {
    this.up = false;
    this.ctrlCh = null;
    this._ctrlPending = false;
    this._failPending(err);
    for (const p of this._pendingOpens) {
      if (p.sock) this._termClosed(p.sock);
    }
    this._pendingOpens = [];
    for (const sock of this._termSocks.values()) this._termClosed(sock);
    this._termSocks.clear();
  }

  _ctrlSend(msg) {
    if (this.ws && this.ws.readyState === 1) {
      this.ws.send(muxFrame(MUX.CTRL, 0, _enc.encode(JSON.stringify(msg))));
      return true;
    }
    return false;
  }

  _onFrame(data) {
    const f = muxParse(data);
    if (!f) return;
    if (f.kind === MUX.CTRL) {
      let msg; try { msg = JSON.parse(_dec.decode(f.payload)); } catch { return; }
      this._onCtrl(msg);
      return;
    }
    if (f.kind !== MUX.DATA) return; // HS = M4 handshake seam
    const pl = this.codec.open(null, f.payload);
    if (!pl || !pl.length) return;
    const ptype = pl[0], body = pl.subarray(1);
    if (f.ch === this.ctrlCh) {
      if (ptype !== MUX.PT_JSON) return;
      let msg; try { msg = JSON.parse(_dec.decode(body)); } catch { return; }
      if (msg.type === "rpc_result") { this._settle(msg); return; }
      for (const cb of this._msgCbs) cb(msg);
      return;
    }
    const sock = this._termSocks.get(f.ch);
    if (!sock || !sock.onmessage) return;
    if (ptype === MUX.PT_RAW) {
      const copy = new Uint8Array(body).buffer; // detached view -> own buffer
      sock.onmessage({ data: copy });
    } else {
      sock.onmessage({ data: _dec.decode(body) });
    }
  }

  _onCtrl(msg) {
    switch (msg.type) {
      case "welcome":
        this.plan = msg.plan || null;
        break;
      case "machine_status":
        this.online = !!msg.online;
        if (this.online) this._ensureCtrl();
        else { this.up = false; this._emitStatus({ up: false, offline: true }); }
        break;
      case "opened": {
        const req = this._shiftOpen(msg.kind);
        if (!req) break;
        if (msg.kind === "ctrl") {
          this.ctrlCh = msg.ch;
          this._ctrlPending = false;
          this.up = true;
          this._retry = 0;
          const q = this._sendQueue; this._sendQueue = [];
          for (const obj of q) this._dataSend(obj);
          this._emitStatus({ up: true });
        } else {
          const sock = req.sock;
          sock.ch = msg.ch;
          this._termSocks.set(msg.ch, sock);
          this.ws.send(dataJsonStr(msg.ch,
            JSON.stringify({ type: "term_attach", sid: req.sid }), this.codec));
          sock.readyState = 1;
          if (sock.onopen) sock.onopen({});
        }
        break;
      }
      case "close": {
        const ch = msg.ch;
        if (ch === this.ctrlCh) {
          this.ctrlCh = null;
          this.up = false;
          this._failPending(new Error("channel closed"));
          this._emitStatus({ up: false });
          if (this.online) this._ensureCtrl(); // e.g. agent_replaced
        } else if (this._termSocks.has(ch)) {
          const sock = this._termSocks.get(ch);
          this._termSocks.delete(ch);
          this._termClosed(sock);
        }
        break;
      }
      case "error":
        if (msg.code === "machine_offline") {
          this.online = false; this.up = false;
          this._ctrlPending = false;
          for (const p of this._pendingOpens) {
            if (p.sock) this._termClosed(p.sock, "machine_offline");
          }
          this._pendingOpens = [];
          this._emitStatus({ up: false, offline: true });
        } else if (msg.code === "quota_term" || msg.code === "bad_open") {
          const req = this._shiftOpen("term");
          if (req && req.sock) this._termClosed(req.sock, msg.code);
        }
        break;
    }
  }

  _shiftOpen(kind) {
    const i = this._pendingOpens.findIndex((p) => p.kind === kind);
    return i < 0 ? null : this._pendingOpens.splice(i, 1)[0];
  }

  _ensureCtrl() {
    if (this.ctrlCh !== null || this._ctrlPending) return;
    if (!this._ctrlSend({ type: "open", kind: "ctrl" })) return;
    this._ctrlPending = true;
    this._pendingOpens.push({ kind: "ctrl" });
  }

  _dataSend(obj) {
    if (this.ctrlCh === null || !this.ws || this.ws.readyState !== 1) return false;
    this.ws.send(dataJsonStr(this.ctrlCh, JSON.stringify(obj), this.codec));
    return true;
  }

  send(obj) {
    if (this._dataSend(obj)) return true;
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
      const frame = { type: "rpc", id, method, params: params || {} };
      if (!this._dataSend(frame)) { this._sendQueue.push(frame); this.connect(); }
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

  /* WebSocket-shim for the True View terminal: binary out (PTY bytes as
     ArrayBuffer messages), JSON control in/out as strings — exactly what
     the local /ws/term socket gives the term UI. */
  openTerm(sid) {
    const t = this;
    const sock = {
      readyState: 0, binaryType: "arraybuffer", ch: null,
      onopen: null, onmessage: null, onclose: null, onerror: null,
      send(s) {
        if (this.readyState === 1 && this.ch !== null &&
            t.ws && t.ws.readyState === 1) {
          t.ws.send(dataJsonStr(this.ch, s, t.codec));
        }
      },
      close() {
        if (this.readyState === 3) return;
        if (this.ch !== null) {
          t._termSocks.delete(this.ch);
          t._ctrlSend({ type: "close", ch: this.ch });
        }
        t._termClosed(this);
      },
    };
    if (!this._ctrlSend({ type: "open", kind: "term" })) {
      setTimeout(() => this._termClosed(sock), 0);
      return sock;
    }
    this._pendingOpens.push({ kind: "term", sock, sid });
    return sock;
  }

  _termClosed(sock, reason) {
    if (sock.readyState === 3) return;
    sock.readyState = 3;
    if (sock.onclose) sock.onclose({ code: 1000, reason: reason || "" });
  }

  onMessage(cb) { this._msgCbs.push(cb); }
  onStatus(cb) { this._statusCbs.push(cb); }
  _emitStatus(st) { for (const cb of this._statusCbs) cb(st); }

  close() {
    clearTimeout(this._retryTimer);
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onclose = null; ws.onmessage = null; ws.onerror = null;
      try { ws.close(); } catch {}
    }
    this._teardown(new Error("transport closed"));
  }
}

/* Mode chosen at boot: GET /api/env -> {"mode":"local"|"platform"}. */
async function fetchEnvMode() {
  try {
    const env = await fetch("/api/env").then((r) => r.json());
    if (env && env.mode === "platform") return "platform";
  } catch {}
  return "local";
}
