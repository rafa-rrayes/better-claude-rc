# Vendored: libsodium.js

- Library: libsodium.js (libsodium-wrappers, combined browser build:
  wasm with asm.js fallback, single file)
- Version: 0.7.15
- Source URL: https://raw.githubusercontent.com/jedisct1/libsodium.js/0.7.15/dist/browsers/sodium.js
- Retrieved: 2026-07-14
- SHA-256: 160fc8842737f6200248d04043bc7a2c7c947d5823cd8889d5465871370ee3ec
- License: ISC (see the libsodium.js repository)

Used by `web/crypto.js` for the end-to-end layer: Ed25519 identities
(crypto_sign), per-connection key exchange (crypto_kx),
crypto_secretstream_xchacha20poly1305 frame encryption, and keyed BLAKE2b
(crypto_generichash) for the pairing MAC and per-channel key derivation.

Do not modify this file. To upgrade, replace it with a newer pinned release
and update the version + hash here.
