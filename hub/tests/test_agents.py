"""Enrollment, the agent socket, live state and history."""
import time

import pytest
from conftest import login_with_2fa
from starlette.websockets import WebSocketDisconnect

from icsd_hub import agents, db, live, metrics, security


def sample(ts=None, cpu=12.5, disk_pct=40.0, containers=None, slow=True):
    s = {
        "ts": int((ts or time.time()) * 1000),
        "cpu": {"pct": cpu, "user": cpu, "system": 0, "iowait": 1, "steal": 0},
        "load": [0.5, 0.4, 0.3],
        "mem": {"total": 4e9, "used": 2e9, "available": 2e9, "cached": 1e9, "pct": 50,
                "swap_total": 0, "swap_used": 0, "swap_pct": 0},
        "io": {"read_bps": 100, "write_bps": 200}, "net": {"rx_bps": 1000, "tx_bps": 500},
        "tcp": 20, "procs": 100, "uptime": 3600,
    }
    if slow:
        s["disks"] = [{"mount": "/", "device": "/dev/vda1", "fs": "ext4", "total": 30e9,
                       "used": 30e9 * disk_pct / 100, "free": 30e9 * (1 - disk_pct / 100),
                       "pct": disk_pct, "inodes_pct": 5}]
        s["top"] = [{"pid": 1, "name": "postgres", "user": "999", "cpu": 3, "rss": 1e8}]
        if containers is not None:
            s["containers"] = containers
    return s


def enrolled(client):
    login_with_2fa(client)
    r = client.post("/api/servers", json={"name": "farshplus"})
    token = r.json()["command"].split("--token ")[1]
    r = client.post("/api/agent/enroll", json={"token": token, "host": {"hostname": "vps1", "cpus": 2}})
    assert r.status_code == 200, r.text
    return r.json()


def test_install_command_and_one_time_token(client):
    login_with_2fa(client)
    r = client.post("/api/servers", json={"name": "web-1"}).json()
    assert r["command"].startswith("curl -fsSL http://testserver/install.sh | sudo bash -s -- --token ")
    token = r["command"].split("--token ")[1]
    row = db.one("SELECT * FROM servers")
    assert row["enroll_hash"] == security.sha256(token)  # the token itself is not stored
    first = client.post("/api/agent/enroll", json={"token": token})
    assert first.status_code == 200
    again = client.post("/api/agent/enroll", json={"token": token})
    assert again.status_code == 403  # single use
    row = db.one("SELECT * FROM servers")
    assert row["secret_hash"] == security.sha256(first.json()["secret"])


def test_expired_token_is_refused(client):
    login_with_2fa(client)
    token = client.post("/api/servers", json={"name": "x"}).json()["command"].split("--token ")[1]
    db.x("UPDATE servers SET enroll_expires=?", (db.now() - 1,))
    assert client.post("/api/agent/enroll", json={"token": token}).status_code == 403


def test_enroll_guessing_is_limited(client):
    for _ in range(10):
        client.post("/api/agent/enroll", json={"token": "guess"})
    assert client.post("/api/agent/enroll", json={"token": "guess"}).status_code == 429


def test_socket_rejects_a_wrong_secret(client):
    ident = enrolled(client)
    with client.websocket_connect("/api/agent/ws",
                                  headers={"Authorization": f"Bearer {ident['server_id']}:wrong"}) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
    assert e.value.code == agents.CLOSE_UNAUTHORIZED


def test_samples_flow_to_overview_and_detail(client):
    ident = enrolled(client)
    hdr = {"Authorization": f"Bearer {ident['server_id']}:{ident['secret']}", "X-Agent-Version": "2.0.0"}
    with client.websocket_connect("/api/agent/ws", headers=hdr) as ws:
        assert '"cfg"' in ws.receive_text()
        ws.send_json({"t": "hello", "host": {"hostname": "vps1", "cpus": 2, "os": "Ubuntu 24.04"}})
        ws.send_json({"t": "m", "d": sample(containers=[
            {"id": "a", "name": "fp-api", "image": "api", "state": "running", "health": "healthy",
             "status": "Up 1 hour (healthy)", "cpu": 5, "mem": 3e8}])})
        ws.send_json({"t": "m", "d": sample(cpu=30, slow=False)})
        time.sleep(0.2)
        ov = client.get("/api/overview").json()
        srv = ov["servers"][0]
        assert srv["status"] == "online" and srv["cpu"] == 30
        assert srv["disk"]["mount"] == "/"           # kept from the slow sample
        assert srv["containers"] == {"total": 1, "running": 1, "bad": 0}
        assert srv["host"]["os"] == "Ubuntu 24.04" and srv["agent_version"] == "2.0.0"
        d = client.get(f"/api/servers/{ident['server_id']}").json()
        assert d["last"]["top"][0]["name"] == "postgres"
        series = client.get(f"/api/servers/{ident['server_id']}/series?range=1h").json()
        assert series["cpu"][-2:] == [12.5, 30]
    time.sleep(0.2)
    # Disconnect flushes the minute to disk.
    assert db.one("SELECT COUNT(*) AS n FROM samples_1m")["n"] >= 1
    assert client.get("/api/overview").json()["servers"][0]["status"] == "offline"


def test_garbage_from_an_agent_is_tamed():
    s = live.clean_sample({"cpu": {"pct": "NaN"}, "load": [1], "disks": [{"mount": "x" * 5000}] * 1000,
                           "containers": "nope", "ts": "soon"})
    assert s["cpu"]["pct"] == 0.0
    assert s["load"] == [1.0, 0.0, 0.0]
    assert len(s["disks"]) == live.MAX_DISKS and len(s["disks"][0]["mount"]) == 200
    assert "containers" not in s


def test_wrong_agent_clock_uses_hub_time(env):
    db.x("INSERT INTO servers(id, name, created) VALUES('s1','a',0)")
    lv = live.get("s1")
    s = live.ingest(lv, sample(ts=time.time() + 86400 * 365))
    assert abs(s["ts"] / 1000 - time.time()) < 5


def test_minute_rollup_and_history(env):
    db.x("INSERT INTO servers(id, name, created) VALUES('s1','a',0)")
    lv = live.get("s1")
    base = int(time.time()) // 3600 * 3600 - 7200   # two hours ago, on the hour
    now = base + 7200
    for i in range(12):          # 12 samples over two minutes
        live.ingest(lv, sample(ts=base + i * 10, cpu=10 if i < 6 else 50), now=base + i * 10 + 1)
    live.flush(lv)
    rows = db.q("SELECT ts, cpu, cpu_max FROM samples_1m ORDER BY ts")
    assert [(r["ts"] - base, r["cpu"], r["cpu_max"]) for r in rows] == [(0, 10, 10), (60, 50, 50)]
    metrics.rollup(now)
    h = db.one("SELECT * FROM samples_1h")
    assert h["ts"] == base and h["cpu"] == 30 and h["cpu_max"] == 50
    live.forget("s1")
    assert metrics.series("s1", "24h")["cpu"] == [10, 50]


def test_deleting_a_server_removes_its_history(client):
    ident = enrolled(client)
    db.x("INSERT INTO samples_1m(server_id, ts, cpu) VALUES(?,?,?)", (ident["server_id"], 60, 1))
    assert client.delete(f"/api/servers/{ident['server_id']}").status_code == 200
    assert db.q("SELECT * FROM samples_1m") == [] and db.q("SELECT * FROM servers") == []


def test_install_script_is_served_with_the_hub_address(client):
    body = client.get("/install.sh").text
    assert 'HUB="http://testserver"' in body and "sha256sum" in body
    assert client.get("/dl/../../etc/passwd").status_code == 404
    assert client.get("/dl/icsd-agent-linux-amd64").status_code == 404  # not built in tests
