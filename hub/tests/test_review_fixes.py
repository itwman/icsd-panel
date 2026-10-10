"""Regression tests for the issues found in review."""
import time

from conftest import login_with_2fa
from test_agents import sample

from icsd_hub import alerts, checks, db, live, notify, security


def test_replacing_active_2fa_needs_the_current_code(client):
    u = login_with_2fa(client)
    old_secret = u["totp_secret"]
    r = client.post("/api/auth/2fa/begin", json={})
    assert r.status_code == 400 and r.json()["detail"]["need_current"] is True
    row = db.one("SELECT totp_on, totp_secret FROM users WHERE id=?", (u["id"],))
    assert row["totp_on"] == 1 and row["totp_secret"] == old_secret   # nothing changed
    # With a valid current code: a *pending* secret, the old one still active.
    code = security.totp_now(old_secret, time.time() + 30)   # next step (the current one is used)
    r = client.post("/api/auth/2fa/begin", json={"code": code})
    assert r.status_code == 200, r.text
    row = db.one("SELECT totp_on, totp_secret, totp_pending FROM users WHERE id=?", (u["id"],))
    assert row["totp_on"] == 1 and row["totp_secret"] == old_secret and row["totp_pending"]
    r = client.post("/api/auth/2fa/confirm", json={"code": security.totp_now(row["totp_pending"])})
    assert r.status_code == 200
    row = db.one("SELECT totp_secret, totp_pending FROM users WHERE id=?", (u["id"],))
    assert row["totp_secret"] != old_secret and row["totp_pending"] is None


def setup_server():
    db.x("INSERT INTO servers(id, name, created, secret_hash, last_seen) VALUES('s1','fp',0,'h',?)",
         (int(time.time()),))


def test_disk_alert_survives_samples_without_disks(env):
    sent = env
    setup_server()
    t = time.time()
    alerts.evaluate_sample("s1", live.clean_sample(sample(disk_pct=90)), 2, t)
    assert len(sent) == 1
    alerts.seed()                       # hub restart
    lv = live.get("s1")                 # empty live state, mid-cycle sample: no disks
    s = live.ingest(lv, sample(disk_pct=90, slow=False))
    alerts.evaluate_sample("s1", s, 2, t + 5)
    assert len(sent) == 1, sent         # no false «resolved»


def test_removing_the_last_container_resolves_its_alert(env):
    sent = env
    setup_server()
    bad = [{"id": "1", "name": "api", "image": "x", "state": "restarting", "health": "", "status": "Restarting"}]
    t = time.time()
    alerts.evaluate_sample("s1", live.clean_sample(sample(containers=bad)), 2, t)
    alerts.evaluate_sample("s1", live.clean_sample(sample(containers=bad)), 2, t + 61)
    assert len(sent) == 1
    alerts.evaluate_sample("s1", live.clean_sample(sample(containers=[])), 2, t + 120)
    assert len(sent) == 2 and sent[1].startswith("🟢")


def test_minute_rows_merge_after_reconnect(env):
    setup_server()
    lv = live.get("s1")
    base = (int(time.time()) // 60) * 60 - 600
    for i in range(3):
        live.ingest(lv, sample(ts=base + i * 5, cpu=100), now=base + i * 5)
    live.flush(lv)                      # disconnect mid-minute
    for i in range(3, 6):
        live.ingest(lv, sample(ts=base + i * 5, cpu=0), now=base + i * 5)
    live.flush(lv)
    row = db.one("SELECT cpu, cpu_max, n FROM samples_1m WHERE ts=?", (base,))
    assert (row["cpu"], row["cpu_max"], row["n"]) == (50, 100, 6)


def test_check_failures_survive_restart_and_disable_closes_quietly(env):
    sent = env
    import asyncio

    import httpx
    cid = db.x("INSERT INTO checks(name, url, interval, created) VALUES('s','http://fp.test/',60,0)")

    async def run(times):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(502))) as c:
            for t in times:
                await checks.run_due(c, t)

    t = time.time()
    asyncio.run(run([t, t + 61]))
    assert len(sent) == 1
    checks._state.clear()
    alerts.seed()                       # hub restart
    asyncio.run(run([t + 200]))
    assert len(sent) == 1               # still down: no «resolved», no re-alert
    checks.edited(cid, enabled=False)
    assert len(sent) == 1 and alerts.open_events() == []


def test_api_host_change_needs_the_token_again(env):
    notify.save({"bale_on": True, "bale_token": "1:SECRET", "bale_chat": "5"})
    try:
        notify.save({"bale_api": "https://attacker.example"})
        raise AssertionError("must refuse")
    except ValueError:
        pass
    assert notify.load()["bale_api"] == "https://tapi.bale.ai"
    notify.save({"bale_api": "https://proxy.example", "bale_token": "1:NEW"})
    assert notify.load()["bale_api"] == "https://proxy.example"
