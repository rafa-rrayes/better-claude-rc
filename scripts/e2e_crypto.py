"""E2E-crypto negative tests (M4): the agent must hard-refuse everything
that is not a properly paired, freshly handshaken, correctly sealed peer.

Through a real in-process relay + agent uplink (no Claude turns — cheap):
  1. baseline: QR pairing + handshake + sealed rpc works; key files 0600
  2. wrong pairing secret → bad_mac refused, window NOT burned
  3. correct pairing succeeds after the failed attempt
  4. pairing secret is single-use → second pair1 refused (no_pairing)
  5. unpaired device handshake → refused (unpaired) + hard close
  6. tampered DATA frame → channel closed (ctrl channel = whole client)
  7. replayed DATA frame → rejected, channel closed, ctrl unaffected
  8. plaintext DATA after handshake → channel closed
  9. plaintext DATA without any handshake → channel closed
 10. HS frame after establishment → hard close (fixed protocol, no renego)
 11. revoke_device rpc → live connection cut + re-handshake refused

Run: uv run python scripts/e2e_crypto.py
"""

import asyncio
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402

from app.main import app as platform_app  # noqa: E402
from relay_client import DeviceSim, FakeClient  # noqa: E402
from server import main as srv  # noqa: E402
from server import proto  # noqa: E402
from server.connect import Uplink  # noqa: E402
from server.identity import Devices, Identity, Pairing  # noqa: E402
from server.sessions import SessionManager  # noqa: E402

HOST, RPORT = "127.0.0.1", 8811
RELAY = f"ws://{HOST}:{RPORT}"
MACHINE = "crypto-mach"


async def start_relay():
    server = uvicorn.Server(uvicorn.Config(
        platform_app, host=HOST, port=RPORT, log_level="warning",
        ws_ping_interval=5, ws_ping_timeout=5, timeout_graceful_shutdown=1))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    return server, task


async def fresh_client(device=None):
    c = FakeClient(RELAY, MACHINE, device=device)
    await c.connect()
    await c.wait_status(True, timeout=20)
    return c


async def expect_refusal(c, ctrl, code):
    """A refused handshake = hs_err{code} followed by the hard close of the
    ctrl channel (relayed back to us as CTRL close)."""
    err = await c.next_hs(timeout=10)
    assert err.get("type") == "hs_err" and err.get("code") == code, err
    closed = await c.next_ctrl("close", timeout=10)
    assert closed.get("ch") == ctrl, closed
    return closed


async def main():
    keydir = Path(tempfile.mkdtemp(prefix="bcrc-crypto-keys-"))
    identity = Identity.load_or_create(keydir / "identity.json")
    devices = Devices.load(keydir / "devices.json")
    pairing = Pairing.generate(MACHINE, identity.public_key)
    qr = pairing.qr_payload()

    relay_server, relay_task = await start_relay()
    m = SessionManager()
    uplink = Uplink(m, srv.terms, srv.config, RELAY, MACHINE,
                    identity=identity, devices=devices, pairing=pairing,
                    ping_interval=5, ping_timeout=5)
    utask = asyncio.create_task(uplink.run())

    # ---- S2 first (before the legit pairing): wrong secret → bad_mac,
    # and crucially the window survives for the real user.
    evil = DeviceSim()
    evil.load_qr(qr)
    evil.qr["secret"] = b"\x42" * 32  # wrong pairing secret
    ce = await fresh_client(evil)
    ctrl_e = await ce.open("ctrl")
    await ce.send_hs(ctrl_e, evil.pair1())
    await expect_refusal(ce, ctrl_e, "bad_mac")
    assert pairing.active, "failed MAC must not burn the pairing window"
    assert len(devices) == 0
    await ce.close()
    print("S2 PASS — wrong pairing secret refused (bad_mac), window intact")

    # ---- S1/S3: the real device pairs + handshakes + does sealed rpc
    dev = DeviceSim()
    c = await fresh_client(dev)
    ctrl = await c.secure_ctrl(pair_qr=qr)
    r = await c.rpc(ctrl, "state")
    assert r["ok"] and r["result"]["ok"], r
    assert devices.is_paired(dev.device_pk_b64)
    assert dev.pinned_agent_pk == identity.public_key
    for f in ("identity.json", "devices.json"):
        mode = stat.S_IMODE((keydir / f).stat().st_mode)
        assert mode == 0o600, f"{f} mode {oct(mode)}"
    mode = stat.S_IMODE(keydir.stat().st_mode)
    print(f"S1/S3 PASS — paired after failed attempt, sealed rpc ok, "
          f"key files 0600 (dir {oct(mode)})")

    # ---- S4: the secret burned with the successful pairing
    dev2 = DeviceSim()
    dev2.load_qr(qr)
    c2 = await fresh_client(dev2)
    ctrl2 = await c2.open("ctrl")
    await c2.send_hs(ctrl2, dev2.pair1())
    await expect_refusal(c2, ctrl2, "no_pairing")
    await c2.close()
    print("S4 PASS — pairing secret single-use (second pair1 → no_pairing)")

    # ---- S5: unpaired device straight to hs1 → refused
    dev3 = DeviceSim()
    c3 = await fresh_client(dev3)
    ctrl3 = await c3.open("ctrl")
    await c3.send_hs(ctrl3, dev3.hs1())
    await expect_refusal(c3, ctrl3, "unpaired")
    await c3.close()
    print("S5 PASS — unpaired device handshake refused")

    # ---- S6: tampered DATA frame → ctrl channel closed (whole client)
    c4 = await fresh_client(dev)
    ctrl4 = await c4.secure_ctrl()
    header = proto.pack_header(proto.KIND_DATA, ctrl4)
    sealed = dev.codec.seal(header, bytes([proto.PTYPE_JSON])
                            + b'{"type":"ping"}')
    tampered = bytearray(header + sealed)
    tampered[-1] ^= 0x01  # flip one ciphertext bit
    await c4.ws.send(bytes(tampered))
    closed = await c4.next_ctrl("close", timeout=10)
    assert closed.get("ch") == ctrl4, closed
    await c4.close()
    print("S6 PASS — tampered DATA frame → channel closed")

    # ---- S7: replayed DATA frame → rejected; ctrl channel unaffected
    c5 = await fresh_client(dev)
    ctrl5 = await c5.secure_ctrl()
    term5 = await c5.open("term")
    h5 = proto.pack_header(proto.KIND_DATA, term5)
    wire = h5 + dev.codec.seal(h5, bytes([proto.PTYPE_JSON]) + b'{"x":1}')
    await c5.ws.send(wire)   # accepted (decodes, ignored: not term_attach)
    await c5.ws.send(wire)   # exact replay → secretstream rejects
    closed = await c5.next_ctrl("close", timeout=10)
    assert closed.get("ch") == term5, closed
    r = await c5.rpc(ctrl5, "state")   # other channels keep working
    assert r["ok"], r
    print("S7 PASS — replayed frame rejected (channel closed, ctrl fine)")

    # ---- S8: plaintext DATA after an established handshake → closed
    header5 = proto.pack_header(proto.KIND_DATA, ctrl5)
    await c5.ws.send(header5 + bytes([proto.PTYPE_JSON])
                     + b'{"type":"rpc","id":9,"method":"state","params":{}}')
    closed = await c5.next_ctrl("close", timeout=10)
    assert closed.get("ch") == ctrl5, closed
    await c5.close()
    print("S8 PASS — plaintext frame after handshake → hard close")

    # ---- S9: plaintext DATA with no handshake at all → closed
    c6 = await fresh_client()   # no DeviceSim: NullCodec plaintext
    ctrl6 = await c6.open("ctrl")
    await c6.send_json(ctrl6, {"type": "rpc", "id": 1, "method": "state",
                               "params": {}})
    closed = await c6.next_ctrl("close", timeout=10)
    assert closed.get("ch") == ctrl6, closed
    await c6.close()
    print("S9 PASS — ciphertext is mandatory (plaintext without handshake "
          "→ close)")

    # ---- S10: HS after establishment → hard close (no renegotiation)
    c7 = await fresh_client(dev)
    ctrl7 = await c7.secure_ctrl()
    await c7.send_hs(ctrl7, dev.hs1())
    await expect_refusal(c7, ctrl7, "protocol")
    await c7.close()
    print("S10 PASS — post-handshake HS frame → protocol refusal")

    # ---- S11: revoke_device cuts live connections + future handshakes
    c8 = await fresh_client(dev)
    ctrl8 = await c8.secure_ctrl()
    r = await c8.rpc(ctrl8, "devices.list")
    assert r["ok"] and any(d["device_pk"] == dev.device_pk_b64
                           for d in r["result"]["devices"]), r
    # self-revoke: the rpc_result may be raced away by the teardown — the
    # observable contract is the close + the registry change.
    await c8.send_json(ctrl8, {"type": "rpc", "id": 99,
                               "method": "revoke_device",
                               "params": {"device_pk": dev.device_pk_b64}})
    closed = await c8.next_ctrl("close", timeout=10)
    assert closed.get("ch") == ctrl8, closed
    assert not devices.is_paired(dev.device_pk_b64)
    await c8.close()
    c9 = await fresh_client(dev)
    ctrl9 = await c9.open("ctrl")
    await c9.send_hs(ctrl9, dev.hs1())
    await expect_refusal(c9, ctrl9, "unpaired")
    await c9.close()
    print("S11 PASS — revoke_device: live connection cut, re-handshake "
          "refused")

    # ---- teardown
    utask.cancel()
    try:
        await utask
    except asyncio.CancelledError:
        pass
    relay_server.should_exit = True
    await relay_task
    print("\nCRYPTO E2E PASS")


asyncio.run(main())
