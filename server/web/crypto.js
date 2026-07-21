/* End-to-end crypto for platform mode (M4). Classic script, no bundler.
   Mirrors server/identity.py + server/codec.py byte-for-byte:

   - Device identity: Ed25519 keypair. The 32-byte seed lives in IndexedDB
     wrapped by a NON-EXTRACTABLE WebCrypto AES-GCM key (the CryptoKey
     itself is stored via structured clone and can never be exported, so
     exfiltrating IndexedDB alone yields nothing usable).
   - Pairing: the QR string `BCRC1.<agent_id>.<b64 agent_pk>.<b64 secret>`
     is scanned/pasted from the agent terminal, out-of-band of the relay.
     pair1 carries mac1 = keyed BLAKE2b(pairing transcript); the agent's
     pair2 reply is signed by its identity key, which we then PIN per
     machine — after that no other agent key is ever accepted.
   - Per-connection handshake (HS frames on the ctrl channel): fresh
     X25519 crypto_kx ephemerals, device key signs hs1, pinned agent key
     must sign hs2 → two direction keys.
   - Frame layer: per (channel, direction) crypto_secretstream, key =
     BLAKE2b(direction key, "BCRC1|ch|" + u32be(ch)); the first sealed
     frame carries the 24-byte stream header; AD = the 6-byte envelope
     header; TAG_REKEY every 2^16 msgs / 1 GiB.

   Crypto backend: vendored libsodium.js (see vendor/libsodium/VENDOR.md),
   loaded lazily so local mode never pays for it. */
"use strict";

const BcrcCrypto = (() => {
  const HS_VERSION = 1;
  const QR_PREFIX = "BCRC1";
  const REKEY_MSGS = 65536;        // 2^16
  const REKEY_BYTES = 1073741824;  // 1 GiB

  const _te = new TextEncoder();
  const CTX_PAIR1 = _te.encode("BCRC1|pair1|");
  const CTX_PAIR2 = _te.encode("BCRC1|pair2|");
  const CTX_HS1 = _te.encode("BCRC1|hs1|");
  const CTX_HS2 = _te.encode("BCRC1|hs2|");
  const CTX_CH = _te.encode("BCRC1|ch|");

  // ---------------- byte helpers ----------------

  function concat(...parts) {
    let n = 0;
    for (const p of parts) n += p.length;
    const out = new Uint8Array(n);
    let o = 0;
    for (const p of parts) { out.set(p, o); o += p.length; }
    return out;
  }

  function b64ToBytes(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function bytesToB64(bytes) {
    let bin = "";
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
  }

  function u32be(n) {
    return new Uint8Array([(n >>> 24) & 255, (n >>> 16) & 255,
                           (n >>> 8) & 255, n & 255]);
  }

  function chOf(header) {
    return ((header[2] << 24) | (header[3] << 16) | (header[4] << 8)
            | header[5]) >>> 0;
  }

  function bytesEq(a, b) {
    if (!a || !b || a.length !== b.length) return false;
    let d = 0;
    for (let i = 0; i < a.length; i++) d |= a[i] ^ b[i];
    return d === 0;
  }

  // ---------------- lazy vendored libs ----------------

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = src;
      s.onload = () => resolve();
      s.onerror = () => reject(new Error("failed to load " + src));
      document.head.appendChild(s);
    });
  }

  let _sodiumP = null;
  function sodiumReady() {
    if (!_sodiumP) {
      _sodiumP = new Promise((resolve, reject) => {
        if (window.sodium && window.sodium.ready) {
          window.sodium.ready.then(() => resolve(window.sodium), reject);
          return;
        }
        window.sodium = { onload: (s) => resolve(s) };
        loadScript("/vendor/libsodium/sodium.js").catch(reject);
      });
    }
    return _sodiumP;
  }

  let _jsqrP = null;
  function jsQRReady() {
    if (window.jsQR) return Promise.resolve(window.jsQR);
    if (!_jsqrP) _jsqrP = loadScript("/vendor/jsqr/jsQR.js").then(() => window.jsQR);
    return _jsqrP;
  }

  // ---------------- IndexedDB kv ----------------

  const DB_NAME = "bcrc-crypto", STORE = "kv";
  let _dbP = null;
  function db() {
    if (!_dbP) {
      _dbP = new Promise((resolve, reject) => {
        const req = indexedDB.open(DB_NAME, 1);
        req.onupgradeneeded = () => req.result.createObjectStore(STORE);
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      });
    }
    return _dbP;
  }

  async function kvGet(key) {
    const d = await db();
    return new Promise((resolve, reject) => {
      const req = d.transaction(STORE, "readonly").objectStore(STORE).get(key);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function kvPut(key, value) {
    const d = await db();
    return new Promise((resolve, reject) => {
      const tx = d.transaction(STORE, "readwrite");
      tx.objectStore(STORE).put(value, key);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  // ---------------- device identity (wrapped seed) ----------------

  async function deviceSeed(sodium) {
    const rec = await kvGet("device.seed");
    if (rec && rec.wrapped) {
      const wk = await kvGet("device.wrapkey");
      const raw = await crypto.subtle.decrypt(
        { name: "AES-GCM", iv: rec.iv }, wk, rec.ct);
      return new Uint8Array(raw);
    }
    if (rec && rec.plain) return rec.plain;
    const seed = sodium.randombytes_buf(sodium.crypto_sign_SEEDBYTES);
    if (window.isSecureContext && crypto.subtle) {
      const wk = await crypto.subtle.generateKey(
        { name: "AES-GCM", length: 256 }, false /* non-extractable */,
        ["encrypt", "decrypt"]);
      const iv = crypto.getRandomValues(new Uint8Array(12));
      const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, wk, seed);
      await kvPut("device.wrapkey", wk);
      await kvPut("device.seed", { wrapped: true, iv, ct });
    } else {
      console.warn("[bcrc] no secure context — device key stored unwrapped");
      await kvPut("device.seed", { plain: seed });
    }
    return seed;
  }

  // ---------------- pairing QR ----------------

  function parseQr(text) {
    let t = String(text || "").trim();
    // the terminal QR wraps the payload in <app-url>#pair=… so a phone-camera
    // scan opens the PWA; accept the bare payload and the URL form alike
    const m = t.match(/#pair=([^&\s]+)/);
    if (m) { try { t = decodeURIComponent(m[1]); } catch { t = m[1]; } }
    const parts = t.split(".");
    if (parts.length !== 4 || parts[0] !== QR_PREFIX) return null;
    try {
      const agentPk = b64ToBytes(parts[2]);
      const secret = b64ToBytes(parts[3]);
      if (agentPk.length !== 32 || secret.length !== 32) return null;
      return { agentId: parts[1], agentPk, secret };
    } catch {
      return null;
    }
  }

  // ---------------- the session (handshake + codec) ----------------

  class Session {
    constructor(sodium, keypair, machineId, pinnedPk, pairing) {
      this.s = sodium;
      this.kp = keypair;               // {publicKey, privateKey} Ed25519
      this.machineId = machineId;
      this.pinnedPk = pinnedPk;        // Uint8Array(32) | null
      this.pairing = pairing;          // {agentId, agentPk, secret} | null
      this.onPaired = null;            // (agentPk) => void
      this.txKey = null;
      this.rxKey = null;
      this._push = new Map();          // ch -> {state, hdr, msgs, bytes}
      this._pull = new Map();          // ch -> {state}
      this._eph = null;                // {pk, sk, nonce} while hs in flight
      this._mac1 = null;
      const self = this;
      this.codec = {
        seal(header, pt) { return self._seal(header, pt); },
        open(header, ct) { return self._open(header, ct); },
        dropChannel(ch) { self._push.delete(ch); self._pull.delete(ch); },
      };
    }

    get established() { return !!this.txKey; }
    get devicePkB64() { return bytesToB64(this.kp.publicKey); }

    reset() {
      this.txKey = null;
      this.rxKey = null;
      this._push.clear();
      this._pull.clear();
      this._eph = null;
    }

    /* First HS message for a fresh ctrl channel: pair1 if we still need to
       pair, hs1 otherwise. */
    begin() {
      this.reset();
      if (this.pairing && !this.pinnedPk) return this._pair1();
      return this._hs1();
    }

    /* Handle an HS frame from the agent.
       -> {reply?, established?, error?, pairRequired?} */
    handle(msg) {
      const t = msg && msg.type;
      if (t === "pair2") return this._pair2(msg);
      if (t === "hs2") return this._hs2(msg);
      if (t === "hs_err") {
        return {
          error: msg.code || "refused",
          pairRequired: msg.code === "unpaired" || msg.code === "no_pairing",
        };
      }
      return {};
    }

    // -------- pairing --------

    _pair1() {
      const s = this.s, p = this.pairing;
      this._mac1 = s.crypto_generichash(32, concat(
        CTX_PAIR1, _te.encode(p.agentId), p.agentPk, this.kp.publicKey),
        p.secret);
      let name = "phone";
      try { name = (navigator.platform || "phone").slice(0, 40); } catch {}
      return {
        type: "pair1", v: HS_VERSION,
        device_pk: this.devicePkB64,
        name,
        mac1: bytesToB64(this._mac1),
      };
    }

    _pair2(msg) {
      const s = this.s, p = this.pairing;
      let agentPk, sig;
      try { agentPk = b64ToBytes(msg.agent_pk); sig = b64ToBytes(msg.sig); }
      catch { return { error: "bad pair2" }; }
      if (!p || !bytesEq(agentPk, p.agentPk)) {
        return { error: "pair2 key does not match the QR" };
      }
      const transcript = concat(CTX_PAIR2, _te.encode(p.agentId), agentPk,
                                this.kp.publicKey, this._mac1);
      if (!s.crypto_sign_verify_detached(sig, transcript, agentPk)) {
        return { error: "pair2 signature invalid" };
      }
      this.pinnedPk = agentPk;  // PIN — only this key is ever trusted again
      if (this.onPaired) { try { this.onPaired(agentPk); } catch {} }
      return { reply: this._hs1() };  // proceed straight to the handshake
    }

    // -------- per-connection handshake --------

    _hs1() {
      const s = this.s;
      const eph = s.crypto_kx_keypair();
      const nonce = s.randombytes_buf(16);
      this._eph = { pk: eph.publicKey, sk: eph.privateKey, nonce };
      const sig = s.crypto_sign_detached(
        concat(CTX_HS1, this.kp.publicKey, eph.publicKey, nonce),
        this.kp.privateKey);
      return {
        type: "hs1", v: HS_VERSION,
        device_pk: this.devicePkB64,
        eph_pk: bytesToB64(eph.publicKey),
        nonce: bytesToB64(nonce),
        sig: bytesToB64(sig),
      };
    }

    _hs2(msg) {
      const s = this.s;
      if (!this._eph) return { error: "unexpected hs2" };
      if (!this.pinnedPk) return { error: "no pinned agent key" };
      let agentEph, sig;
      try { agentEph = b64ToBytes(msg.eph_pk); sig = b64ToBytes(msg.sig); }
      catch { return { error: "bad hs2" }; }
      const transcript = concat(CTX_HS2, this.kp.publicKey, this._eph.pk,
                                this._eph.nonce, agentEph);
      if (!s.crypto_sign_verify_detached(sig, transcript, this.pinnedPk)) {
        return { error: "agent signature invalid (pin mismatch?)" };
      }
      const keys = s.crypto_kx_client_session_keys(
        this._eph.pk, this._eph.sk, agentEph);
      this.txKey = keys.sharedTx;
      this.rxKey = keys.sharedRx;
      this._eph = null;
      return { established: true };
    }

    // -------- frame codec (per-channel secretstream) --------

    _chKey(dirKey, ch) {
      return this.s.crypto_generichash(32, concat(CTX_CH, u32be(ch)), dirKey);
    }

    _seal(header, pt) {
      const s = this.s;
      if (!this.txKey) throw new Error("codec not established");
      const ch = chOf(header);
      let st = this._push.get(ch);
      if (!st) {
        const r = s.crypto_secretstream_xchacha20poly1305_init_push(
          this._chKey(this.txKey, ch));
        st = { state: r.state, hdr: r.header, msgs: 0, bytes: 0 };
        this._push.set(ch, st);
      }
      let tag = s.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE;
      if (st.msgs + 1 >= REKEY_MSGS || st.bytes + pt.length >= REKEY_BYTES) {
        tag = s.crypto_secretstream_xchacha20poly1305_TAG_REKEY;
        st.msgs = 0; st.bytes = 0;
      } else {
        st.msgs++; st.bytes += pt.length;
      }
      const ct = s.crypto_secretstream_xchacha20poly1305_push(
        st.state, pt, header, tag);
      if (st.hdr) { const out = concat(st.hdr, ct); st.hdr = null; return out; }
      return ct;
    }

    _open(header, payload) {
      const s = this.s;
      if (!this.rxKey) throw new Error("codec not established");
      const ch = chOf(header);
      let st = this._pull.get(ch);
      if (!st) {
        const HB = s.crypto_secretstream_xchacha20poly1305_HEADERBYTES;
        if (payload.length < HB) throw new Error("short first frame");
        st = {
          state: s.crypto_secretstream_xchacha20poly1305_init_pull(
            payload.slice(0, HB), this._chKey(this.rxKey, ch)),
        };
        payload = payload.subarray(HB);
        this._pull.set(ch, st);
      }
      const r = s.crypto_secretstream_xchacha20poly1305_pull(
        st.state, payload, header);
      if (!r || !r.message) throw new Error("open failed");
      return r.message;
    }
  }

  // ---------------- public API ----------------

  return {
    parseQr,
    jsQR: jsQRReady,

    /* Session for a machine. Returns null when the device has no pinned
       agent key for it and no pairing payload was supplied (the UI must
       show the pairing screen). */
    async session(machineId, pairing) {
      const sodium = await sodiumReady();
      const seed = await deviceSeed(sodium);
      const kp = sodium.crypto_sign_seed_keypair(seed);
      let pinned = pairing ? null : (await kvGet("pin:" + machineId)) || null;
      if (!pinned && !pairing) return null;
      const sess = new Session(sodium, kp, machineId, pinned, pairing || null);
      sess.onPaired = (agentPk) => {
        kvPut("pin:" + machineId, agentPk).catch(() => {});
      };
      return sess;
    },

    /* Forget the pin for a machine (e.g. after the agent proves unpaired
       and the user wants to re-pair from scratch). */
    async forgetPin(machineId) {
      try { await kvPut("pin:" + machineId, null); } catch {}
    },
  };
})();
