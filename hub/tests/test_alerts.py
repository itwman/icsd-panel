"""Alert once, resolve once; checks; notifications."""
import asyncio
import time

import httpx
from test_agents import sample

from icsd_hub import alerts, checks, db, live, notify


def setup_server():
    db.x("INSERT INTO servers(id, name, created, secret_hash, last_seen) VALUES('s1','farshplus',0,'h',?)",
         (int(time.time()),))


def test_disk_alert_fires_once_and_resolves_once(env):
    sent = env
    setup_server()
    t = time.time()
    for i in range(5):
        alerts.evaluate_sample("s1", live.clean_sample(sample(disk_pct=97)), 2, t + i)
    # disk (85) and disk_full (95): one message each, not one per sample.
    assert len(sent) == 2
    assert all("farshplus" in m and "\u2066/\u2069: 97٪" in m for m in sent)
    assert len(alerts.open_events()) == 2
    alerts.evaluate_sample("s1", live.clean_sample(sample(disk_pct=50)), 2, t + 600)
    assert len(sent) == 4 and all(m.startswith("🟢") for m in sent[2:])
    assert "۱۰ دقیقه" in sent[2] or "10 دقیقه" in sent[2]
    assert alerts.open_events() == []


def test_cpu_must_last_before_alerting(env):
    sent = env
    setup_server()
    t = time.time()
    alerts.evaluate_sample("s1", live.clean_sample(sample(cpu=99)), 2, t)
    alerts.evaluate_sample("s1", live.clean_sample(sample(cpu=99)), 2, t + 300)
    assert sent == []                       # a 5-minute spike is not an incident
    alerts.evaluate_sample("s1", live.clean_sample(sample(cpu=20)), 2, t + 400)
    alerts.evaluate_sample("s1", live.clean_sample(sample(cpu=99)), 2, t + 500)
    alerts.evaluate_sample("s1", live.clean_sample(sample(cpu=99)), 2, t + 1101)
    assert len(sent) == 1 and "پردازنده" in sent[0]


def test_broken_container_alerts_but_a_finished_job_does_not(env):
    sent = env
    setup_server()
    cs = [
        {"id": "1", "name": "fp-api", "image": "a", "state": "restarting", "health": "", "status": "Restarting (1)"},
        {"id": "2", "name": "fp-migrate", "image": "a", "state": "exited", "health": "", "status": "Exited (0) 1 hour ago"},
        {"id": "3", "name": "fp-db", "image": "p", "state": "running", "health": "unhealthy", "status": "Up (unhealthy)"},
    ]
    t = time.time()
    alerts.evaluate_sample("s1", live.clean_sample(sample(containers=cs)), 2, t)
    alerts.evaluate_sample("s1", live.clean_sample(sample(containers=cs)), 2, t + 61)
    names = sorted(m.split("\n")[1].split(":")[0].replace("کانتینر ", "").strip("\u2066\u2069") for m in sent)
    assert names == ["fp-api", "fp-db"]
    # Container removed entirely → its alert resolves.
    alerts.evaluate_sample("s1", live.clean_sample(sample(containers=cs[1:])), 2, t + 120)
    assert sum(m.startswith("🟢") for m in sent) == 1


def test_offline_after_threshold(env):
    sent = env
    setup_server()
    db.x("UPDATE servers SET last_seen=?", (int(time.time()) - 300,))
    alerts.evaluate_offline()
    assert len(sent) == 1 and "در دسترس نیست" in sent[0]
    db.x("UPDATE servers SET last_seen=?", (int(time.time()),))
    alerts.evaluate_offline()
    assert len(sent) == 2 and sent[1].startswith("🟢")  # it reported again


def test_open_events_survive_a_restart(env):
    sent = env
    setup_server()
    t = time.time()
    alerts.evaluate_sample("s1", live.clean_sample(sample(disk_pct=90)), 2, t)
    assert len(sent) == 1
    alerts.seed()  # what a restart does
    alerts.evaluate_sample("s1", live.clean_sample(sample(disk_pct=90)), 2, t + 10)
    assert len(sent) == 1  # not re-sent
    alerts.evaluate_sample("s1", live.clean_sample(sample(disk_pct=10)), 2, t + 20)
    assert len(sent) == 2 and sent[1].startswith("🟢")


def test_disabled_rule_is_silent(env):
    sent = env
    setup_server()
    alerts.update_rule("disk", 85, 0, "warning", False)
    alerts.update_rule("disk_full", 95, 0, "critical", False)
    alerts.evaluate_sample("s1", live.clean_sample(sample(disk_pct=99)), 2)
    assert sent == []


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_url_check_500_counts_as_down(env):
    sent = env
    cid = db.x("INSERT INTO checks(name, url, interval, created) VALUES('site','http://fp.test/',60,0)")

    def handler(request):
        return httpx.Response(500, text="Server Error")

    async def go():
        async with _client(handler) as c:
            t = time.time()
            await checks.run_due(c, t)
            await checks.run_due(c, t + 61)
            await checks.run_due(c, t + 122)

    asyncio.run(go())
    v = checks.view()[0]
    assert v["ok"] is False and v["code"] == 500 and v["fails"] == 3
    assert len(sent) == 1 and "fp.test" in sent[0]           # alert at 2 failures, once
    assert db.one("SELECT COUNT(*) AS n FROM check_log WHERE check_id=?", (cid,))["n"] == 3


def test_url_check_keyword_and_expectations(env):
    assert checks._status_ok(301, "2xx,3xx") and not checks._status_ok(404, "2xx,3xx")
    assert checks._status_ok(404, "404")
    for bad in ({"url": "ftp://x"}, {"url": "http://x", "expect": "abc"}):
        try:
            checks.validate(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    db.x("INSERT INTO checks(name, url, interval, keyword, created) VALUES('s','http://fp.test/',60,'فرش',0)")

    async def go():
        async with _client(lambda r: httpx.Response(200, text="maintenance")) as c:
            await checks.run_due(c)

    asyncio.run(go())
    assert "فرش" in checks.view()[0]["error"]


def test_notify_tokens_never_leave_the_server(env):
    notify.save({"telegram_on": True, "telegram_token": "123:SECRET", "telegram_chat": "42",
                 "telegram_api": "http://evil"})
    v = notify.public_view()
    assert v["telegram_token"] == "" and v["telegram_token_set"] is True
    assert v["telegram_api"] == "https://api.telegram.org"   # only https endpoints
    notify.save({"telegram_token": ""})                         # empty field keeps the token
    assert notify.load()["telegram_token"] == "123:SECRET"


def test_jalali_dates():
    from datetime import datetime
    ts = datetime(2026, 10, 10, 8, 30).timestamp()  # host TZ is irrelevant to the date here
    assert notify.jalali(datetime(2026, 3, 21, 12, 0, tzinfo=notify.TEHRAN).timestamp()).startswith("۱۴۰۵/۰۱/۰۱")
    assert notify.jalali(datetime(2026, 10, 10, 12, 0, tzinfo=notify.TEHRAN).timestamp()).startswith("۱۴۰۵/۰۷/۱۸")
    assert notify.jalali(datetime(2025, 3, 20, 12, 0, tzinfo=notify.TEHRAN).timestamp()).startswith("۱۴۰۳/۱۲/۳۰")
    assert ts
