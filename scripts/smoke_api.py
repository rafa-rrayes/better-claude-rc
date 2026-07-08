"""REST-layer smoke test — no Claude subprocess is spawned."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from server.config import Config  # noqa: E402
from server import main as srv  # noqa: E402

cfg = Config.load()

with TestClient(srv.app) as client:
    r = client.get("/api/state")
    assert r.status_code == 401, f"expected 401, got {r.status_code}"

    r = client.post("/api/login", json={"token": "wrong"})
    assert r.status_code == 401

    r = client.post("/api/login", json={"token": cfg.token})
    assert r.status_code == 200, r.text

    r = client.get("/api/state")
    assert r.status_code == 200, r.text
    state = r.json()
    assert "sessions" in state and "defaults" in state
    print(f"state ok — {len(state['sessions'])} stored sessions")

    r = client.get("/api/projects")
    assert r.status_code == 200, r.text
    projs = r.json()["projects"]
    print(f"projects ok — {len(projs)} recent projects; first 3:")
    for p in projs[:3]:
        print(f"   {p['name']:32s} {p['session_count']:3d} sessions  {p['path']}")

    if projs:
        cwd = projs[0]["path"]
        r = client.get("/api/projects/sessions", params={"cwd": cwd})
        assert r.status_code == 200, r.text
        sess = r.json()["sessions"]
        print(f"past sessions ok — {len(sess)} for {cwd}; first:")
        for s in sess[:2]:
            print(f"   {s['session_id'][:8]}  {s['summary'][:70]!r}")
        if sess:
            r = client.get("/api/projects/preview",
                           params={"session_id": sess[0]["session_id"], "cwd": cwd})
            assert r.status_code == 200, r.text
            msgs = r.json()["messages"]
            print(f"preview ok — {len(msgs)} messages in tail")

    r = client.get("/api/library")
    assert r.status_code == 200, r.text
    lib = r.json()
    print(f"library ok — {len(lib['skills'])} skills, {len(lib['commands'])} commands, "
          f"{len(lib['agents'])} agents, {len(lib['mcp_servers'])} mcp servers")
    for s in lib["skills"][:5]:
        print(f"   skill: {s['name']} [{s['scope']}{('/' + s['source']) if s['source'] else ''}]")
    for c in lib["commands"][:5]:
        print(f"   cmd:   /{c['name']} [{c['scope']}]  {c['description'][:40]}")
    for m in lib["mcp_servers"]:
        print(f"   mcp:   {m['name']} ({m['transport']}) [{m['scope']}]")

    r = client.get("/api/browse")
    assert r.status_code == 200, r.text
    b = r.json()
    print(f"browse ok — {len(b['dirs'])} dirs under {b['path']}")

    r = client.get("/api/browse", params={"path": "/etc"})
    assert r.json()["path"] == str(Path.home()), "browse escaped root!"
    print("browse root confinement ok")

print("\nALL REST SMOKE TESTS PASSED")
