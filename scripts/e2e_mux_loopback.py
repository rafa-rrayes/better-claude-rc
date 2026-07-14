"""Mux loopback: verifies the agent-side drop policy and the envelope codec.

1. Flood — thousands of transient (seq:null) events interleaved with seq
   events through a slow consumer: every seq event survives in order, the
   transient backlog is capped at TRANSIENT_QUEUE_MAX (oldest dropped first),
   and the channel stays open.
2. Laggard — a fully stalled consumer: seq events are never dropped, so at
   CHANNEL_QUEUE_MAX queued the channel is closed (client recovers via
   attach{since_seq} replay) and further sends raise ChannelClosed.
3. Envelope — pack/unpack roundtrip, raw-vs-json ptype, bad frames rejected.

Run: uv run python scripts/e2e_mux_loopback.py
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import proto  # noqa: E402
from server.mux import ChannelClosed, MuxConn  # noqa: E402

CH = 7


def decode_all(mux, frames):
    out = []
    for frame in frames:
        kind, ch, ptype, body = mux.decode(frame)
        assert kind == proto.KIND_DATA and ch == CH, (kind, ch)
        out.append((ptype, body))
    return out


async def test_flood():
    print("-- flood: transients dropped, seq events survive --")
    received = []

    async def slow_send(frame):
        await asyncio.sleep(0.0002)  # slow consumer
        received.append(frame)

    mux = MuxConn(slow_send)
    mux.start()

    N_TRANSIENT, EVERY = 5000, 25
    n_seq = 0
    for i in range(N_TRANSIENT):
        mux.send_json(CH, {"kind": "delta", "seq": None, "i": i}, transient=True)
        if i % EVERY == 0:
            n_seq += 1
            mux.send_json(CH, {"kind": "text", "seq": n_seq})
        if i % 400 == 0:
            await asyncio.sleep(0)  # let the pump interleave

    while mux.pending(CH):
        await asyncio.sleep(0.01)
    await mux.close()

    bodies = [json.loads(body) for ptype, body in decode_all(mux, received)
              if ptype == proto.PTYPE_JSON]
    seqs = [b["seq"] for b in bodies if b.get("seq") is not None]
    transients = [b for b in bodies if b.get("seq") is None]
    assert seqs == list(range(1, n_seq + 1)), \
        f"seq events lost/reordered: got {len(seqs)}/{n_seq}"
    assert len(transients) < N_TRANSIENT, "no transient was dropped?!"
    assert mux.channel_open(CH), "channel should have survived the flood"
    print(f"FLOOD PASS — {n_seq}/{n_seq} seq events in order; "
          f"{N_TRANSIENT - len(transients)}/{N_TRANSIENT} transients dropped")


async def test_laggard():
    print("-- laggard: seq overflow closes the channel at "
          f"{proto.CHANNEL_QUEUE_MAX} --")
    stall = asyncio.Event()

    closed = []
    mux = MuxConn(lambda frame: stall.wait(),
                  on_channel_close=lambda ch, why: closed.append((ch, why)))
    mux.start()
    await asyncio.sleep(0)  # pump parks on the stalled send

    sent = 0
    try:
        for i in range(1, proto.CHANNEL_QUEUE_MAX * 2):
            mux.send_json(CH, {"kind": "text", "seq": i})
            sent = i
    except ChannelClosed:
        pass
    else:
        raise AssertionError("laggard channel never closed")

    # exactly CHANNEL_QUEUE_MAX frames were queued (+1 may sit in-flight)
    assert proto.CHANNEL_QUEUE_MAX <= sent <= proto.CHANNEL_QUEUE_MAX + 2, sent
    assert closed == [(CH, "overflow")], closed
    assert not mux.channel_open(CH) and mux.pending(CH) == 0
    for send in (lambda: mux.send_json(CH, {"seq": 1}),
                 lambda: mux.send_raw(CH, b"x")):
        try:
            send()
            raise AssertionError("send on closed channel succeeded")
        except ChannelClosed:
            pass
    stall.set()
    await mux.close()
    print(f"LAGGARD PASS — closed after {sent} un-droppable frames, "
          "sends now raise ChannelClosed")


async def test_envelope():
    print("-- envelope + ptype --")
    frame = proto.pack_frame(proto.KIND_CTRL, 0, b'{"type":"hello"}')
    kind, ch, payload = proto.unpack_frame(frame)
    assert (kind, ch, payload) == (proto.KIND_CTRL, 0, b'{"type":"hello"}')

    received = []

    async def send(frame):
        received.append(frame)

    mux = MuxConn(send)
    mux.start()
    mux.send_raw(CH, b"\x1b[2Jpty-bytes")
    mux.send_json(CH, {"kind": "status"})
    while mux.pending(CH):
        await asyncio.sleep(0.01)
    await mux.close()
    (pt_raw, raw), (pt_json, js) = decode_all(mux, received)
    assert pt_raw == proto.PTYPE_RAW and raw == b"\x1b[2Jpty-bytes"
    assert pt_json == proto.PTYPE_JSON and json.loads(js) == {"kind": "status"}

    for bad in (b"", b"\x01\x02", b"\x02" + frame[1:], bytes([proto.ENV_VER, 9, 0, 0, 0, 1])):
        try:
            proto.unpack_frame(bad)
            raise AssertionError(f"bad frame accepted: {bad!r}")
        except proto.FrameError:
            pass

    try:
        MuxConn(send).send_raw(CH, b"x" * (proto.FRAME_MAX_PLAINTEXT + 1))
        raise AssertionError("oversize payload accepted")
    except ValueError:
        pass
    print("ENVELOPE PASS — roundtrip, ptypes, bad frames rejected, size cap")


async def main():
    await test_flood()
    await test_laggard()
    await test_envelope()
    print("\nMUX LOOPBACK E2E PASS")


asyncio.run(main())
