"""Phases 2–4 on the hub side: permissions, elevation, relaying, backups, domains."""
import asyncio
import base64
import json
import time

import pytest
from conftest import login_with_2fa

from icsd_hub import alerts, backups, db, domains, live, rpc, security


def add_server(sid="s1", name="fp", mode="manage"):
    db.x("INSERT INTO servers(id, name, created, secret_hash, last_seen) VALUES(?,?,0,'h',?)",
         (sid, name, int(time.time())))
    lv = live.get(sid)
    lv.ws = object()
    lv.caps = {"mode": mode, "backup_dir": "/var/backups/icsd", "ssl": True}
    lv.ip = "1.2.3.4"
    return lv


@pytest.fixture
def fake_rpc(monkeypatch):
    calls = []
    handlers = {}

    async def call(sid, op, args=None, timeout=60):
        calls.append((sid, op, args or {}))
        h = handlers.get(op)
        if h is None:
            return {"ok": True}
        res = h(sid, args or {})
        if isinstance(res, Exception):
            raise res
        return res

    monkeypatch.setattr(rpc, "call", call)
    return calls, handlers


def elevate(client, user):
    """Already elevated by login_with_2fa unless it was told not to."""
    from icsd_hub import security as sec
    row = db.one("SELECT totp_secret, totp_last FROM users WHERE id=?", (user["id"],))
    step = int(time.time() // 30)
    if row["totp_last"] > step:   # the +1 step is used up: nothing left to elevate with
        return
    code = sec.totp_now(row["totp_secret"], time.time() + 30)
    r = client.post("/api/auth/elevate", json={"code": code})
    assert r.status_code == 200, r.text


def test_changes_need_a_fresh_2fa_code(client, fake_rpc):
    calls, _ = fake_rpc
    u = login_with_2fa(client, elevate=False)
    add_server()
    r = client.post("/api/servers/s1/fs/delete", json={"paths": ["/var/www/x"]})
    assert r.status_code == 403 and r.json()["detail"] == "elevation_required"
    assert calls == []                      # nothing reached the server
    elevate(client, u)
    r = client.post("/api/servers/s1/fs/delete", json={"paths": ["/var/www/x"]})
    assert r.status_code == 200
    assert calls == [("s1", "fs.delete", {"paths": ["/var/www/x"]})]
    assert db.one("SELECT 1 FROM audit WHERE action='fs.delete'")  # audited


def test_reading_needs_admin_not_elevation(client, fake_rpc):
    calls, _ = fake_rpc
    login_with_2fa(client)
    add_server()
    assert client.get("/api/servers/s1/fs/list?path=/var/www").status_code == 200
    assert calls[0][1] == "fs.list"


def test_viewers_cannot_browse_files(client, fake_rpc):
    login_with_2fa(client, "eve", "viewer-pass-1", role="viewer")
    add_server()
    assert client.get("/api/servers/s1/fs/list?path=/").status_code == 403
    assert client.get("/api/servers/s1/databases").status_code == 403


def test_monitor_only_server_says_how_to_enable(client):
    login_with_2fa(client)
    add_server(mode="monitor")
    r = client.get("/api/servers/s1/fs/list?path=/")
    assert r.status_code == 409 and "--upgrade" in r.json()["detail"]


def test_offline_server(client):
    login_with_2fa(client)
    add_server().ws = None
    assert client.get("/api/servers/s1/services").status_code == 503


def test_download_streams_in_chunks(client, fake_rpc):
    _, handlers = fake_rpc
    login_with_2fa(client)
    add_server()
    content = b"x" * (512 * 1024) + b"tail"

    def read(sid, a):
        part = content[a["offset"]:a["offset"] + a["length"]]
        return {"data": base64.b64encode(part).decode(), "size": len(content),
                "eof": a["offset"] + len(part) >= len(content), "mtime": 1}
    handlers["fs.read"] = read
    r = client.get("/api/servers/s1/fs/download?path=/var/www/big.bin")
    assert r.status_code == 200 and r.content == content
    assert "big.bin" in r.headers["content-disposition"]


def test_upload_marks_only_the_last_chunk_final(client, fake_rpc):
    calls, _ = fake_rpc
    u = login_with_2fa(client)
    add_server()
    elevate(client, u)
    data = b"a" * (512 * 1024 + 10)
    r = client.post("/api/servers/s1/fs/upload?dir=/var/www", files={"files": ("../../etc/x.txt", data)})
    assert r.status_code == 200, r.text
    writes = [c[2] for c in calls if c[1] == "fs.write"]
    assert [w["final"] for w in writes] == [False, True]
    assert all(w["path"] == "/var/www/x.txt" for w in writes)   # the name cannot climb directories
    assert b"".join(base64.b64decode(w["data"]) for w in writes) == data


def test_restore_needs_the_name_typed(client, fake_rpc):
    u = login_with_2fa(client)
    add_server()
    elevate(client, u)
    body = {"db": {"engine": "mysql", "where": "native", "name": "shop"}, "file": "/var/backups/icsd/a.sql.gz"}
    assert client.post("/api/servers/s1/databases/restore", json={**body, "confirm": "shp"}).status_code == 400
    assert client.post("/api/servers/s1/databases/restore", json={**body, "confirm": "shop"}).status_code == 200


# ── backups ────────────────────────────────────────────────────────────

def test_next_run_is_on_the_tehran_hour():
    from datetime import datetime
    from icsd_hub.notify import TEHRAN
    base = datetime(2026, 10, 10, 10, 0, tzinfo=TEHRAN).timestamp()
    t = backups.next_run(24, 3, base)
    assert datetime.fromtimestamp(t, TEHRAN).strftime("%d %H:%M") == "11 03:00"
    t = backups.next_run(6, 3, base)
    assert datetime.fromtimestamp(t, TEHRAN).strftime("%d %H:%M") == "10 15:00"


def make_plan(dest=None, delete_local=False, kind="db"):
    target = {"db": {"engine": "postgres", "where": "docker:fp-db", "name": "fp", "size": 10}} if kind == "db" \
        else {"path": "/opt/farshplus/media", "exclude": []}
    return db.x("INSERT INTO backup_plans(server_id, name, kind, target, every_hours, start_hour, keep, dest_server, "
                "dest_keep, delete_local, next_run, created) VALUES('s1','fp db',?,?,24,3,3,?,5,?,0,0)",
                (kind, json.dumps(target), dest, int(delete_local)))


def test_backup_run_copies_prunes_and_deletes_local(env, fake_rpc):
    calls, handlers = fake_rpc
    add_server("s1", "fp")
    add_server("s2", "iran")
    payload = b"dump" * 300000   # 1.2 MB: three chunks
    handlers["backup.run"] = lambda sid, a: {"job": "0123456789abcdef"}
    handlers["job.status"] = lambda sid, a: {"state": "done", "result": {"path": "/var/backups/icsd/p1-x.dump", "size": len(payload)}}

    def read(sid, a):
        part = payload[a["offset"]:a["offset"] + a["length"]]
        return {"data": base64.b64encode(part).decode(), "size": len(payload),
                "eof": a["offset"] + len(part) >= len(payload), "mtime": 1}
    received = bytearray()

    def write(sid, a):
        assert sid == "s2"
        received[a["offset"]:] = base64.b64decode(a["data"])
        return {"written": len(received)}
    handlers["fs.read"] = read
    handlers["fs.write"] = write
    pid = make_plan(dest="s2", delete_local=True)

    async def go():
        backups.start(pid, "manual")
        await asyncio.gather(*backups._running.values())
    asyncio.run(go())
    run = db.one("SELECT * FROM backup_runs")
    assert run["state"] == "done", run
    assert run["dest_path"] == "/var/backups/icsd/remote/s1/p1-x.dump"
    assert bytes(received) == payload
    ops = [(c[0], c[1]) for c in calls]
    assert ("s1", "backup.prune") in ops and ("s2", "backup.prune") in ops
    assert ops[-1] == ("s1", "fs.delete")                 # local copy removed only after a good copy
    prune_dest = [c[2] for c in calls if c[0] == "s2" and c[1] == "backup.prune"][0]
    assert prune_dest == {"dir": "remote/s1", "prefix": "p1", "keep": 5}


def test_backup_failure_alerts_once_and_recovers(env, fake_rpc):
    sent = env
    _, handlers = fake_rpc
    add_server()
    handlers["backup.run"] = lambda sid, a: rpc.RPCError("فضای کافی نیست")
    pid = make_plan(kind="files")

    async def go():
        backups.start(pid, "schedule")
        await asyncio.gather(*backups._running.values())
    asyncio.run(go())
    run = db.one("SELECT * FROM backup_runs")
    assert run["state"] == "error" and "فضای کافی" in run["error"]
    assert len(sent) == 1 and "بک‌آپ" in sent[0]
    handlers["backup.run"] = lambda sid, a: {"job": "0123456789abcdef"}
    handlers["job.status"] = lambda sid, a: {"state": "done", "result": {"path": "/x/p1-y.tar.gz", "size": 1}}
    asyncio.run(go())
    assert len(sent) == 2 and sent[1].startswith("🟢")


def test_plan_validation(client, fake_rpc):
    u = login_with_2fa(client)
    add_server("s1")
    elevate(client, u)
    bad = {"server_id": "s1", "name": "x", "kind": "files", "path": "/opt/media", "delete_local": True}
    assert client.post("/api/backups/plans", json=bad).status_code == 400   # delete local without a copy
    assert client.post("/api/backups/plans", json={**bad, "delete_local": False, "path": "relative"}).status_code == 400
    r = client.post("/api/backups/plans", json={**bad, "delete_local": False})
    assert r.status_code == 200 and r.json()["plans"][0]["next_run"] > time.time()


# ── domains ────────────────────────────────────────────────────────────

def test_base_domain():
    assert domains.base_domain("www.farshplus.ir") == "farshplus.ir"
    assert domains.base_domain("panel.icsd.ir") == "icsd.ir"
    assert domains.base_domain("shop.example.co.ir") == "example.co.ir"


def test_wildcard_cert_match():
    certs = [{"domains": ["*.icsd.ir"], "not_after": 10}, {"domains": ["icsd.ir"], "not_after": 5}]
    assert domains._cert_for("panel.icsd.ir", certs)["not_after"] == 10
    assert domains._cert_for("a.b.icsd.ir", certs) is None
    assert domains._cert_for("icsd.ir", certs)["not_after"] == 5


def test_domains_overview_and_cert_alert(env, fake_rpc, monkeypatch):
    sent = env
    _, handlers = fake_rpc
    add_server("s1", "rajyar")
    soon = int(time.time()) + 3 * 86400
    handlers["ssl.list"] = lambda sid, a: {
        "certs": [{"name": "panel.icsd.ir", "source": "certbot", "domains": ["panel.icsd.ir"], "not_after": soon}],
        "sites": [{"file": "/etc/nginx/sites-enabled/icsd-panel.conf", "names": ["panel.icsd.ir"], "ssl": True},
                  {"file": "/etc/nginx/sites-enabled/rajyar", "names": ["rajyar.ir", "_"], "ssl": False}],
        "certbot": True}

    async def fake_resolve(name, rtype):
        return {"panel.icsd.ir": ["1.2.3.4"], "rajyar.ir": ["9.9.9.9"]}.get(name, []) if rtype == "A" else []
    monkeypatch.setattr(domains, "_resolve", fake_resolve)
    domains._cache.clear()
    domains._dns.clear()
    out = asyncio.run(domains.overview(force=True))
    rows = {r["domain"]: r for r in out["domains"]}
    assert rows["panel.icsd.ir"]["dns_state"] == "ok" and rows["panel.icsd.ir"]["cert"]
    assert rows["rajyar.ir"]["dns_state"] == "elsewhere" and not rows["rajyar.ir"]["ssl"]
    assert set(out["mail"]) == {"icsd.ir", "rajyar.ir"}
    assert len(sent) == 1 and "panel.icsd.ir" in sent[0]          # 3 days left < 14


def test_user_admin_needs_elevation(client):
    login_with_2fa(client, elevate=False)
    r = client.post("/api/users", json={"username": "mallory", "password": "long-enough-pw", "role": "admin"})
    assert r.status_code == 403 and r.json()["detail"] == "elevation_required"


def test_failed_copy_removes_the_partial_file(env, fake_rpc):
    calls, handlers = fake_rpc
    add_server("s1")
    add_server("s2")
    payload = b"z" * (512 * 1024 * 2)

    def read(sid, a):
        if a["offset"] > 0:
            return rpc.RPCError("سرور مبدأ قطع شد", 503)
        part = payload[:a["length"]]
        return {"data": base64.b64encode(part).decode(), "size": len(payload), "eof": False, "mtime": 1}
    handlers["fs.read"] = read
    handlers["fs.write"] = lambda sid, a: {"written": 1, "upload": ".icsd-upload-42"}
    with pytest.raises(rpc.RPCError):
        asyncio.run(backups.copy_between("s1", "/var/backups/icsd/x.dump", "s2", len(payload)))
    assert ("s2", "fs.abort", {"path": "/var/backups/icsd/remote/s1/x.dump", "upload": ".icsd-upload-42"}) in calls


def test_each_plan_gets_its_own_prefix(client, fake_rpc):
    login_with_2fa(client)
    add_server("s1")
    body = {"server_id": "s1", "name": "x", "kind": "files", "path": "/opt/media"}
    a = client.post("/api/backups/plans", json=body).json()["id"]
    client.delete(f"/api/backups/plans/{a}")
    b = client.post("/api/backups/plans", json=body).json()["id"]
    prefixes = [p["prefix"] for p in db.q("SELECT prefix FROM backup_plans")]
    assert len(prefixes) == 1 and prefixes[0].startswith("p") and prefixes[0] != f"p{b}"
