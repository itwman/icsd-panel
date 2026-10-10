<div align="center">

<img src="hub/icsd_hub/web/assets/logo.svg" width="64" alt="" />

# پنل ICSD — نسخهٔ ۲

**مدیریت یکپارچهٔ چند سرور لینوکسی از یک پنل؛ فارسی، سبک، امن**

ساختهٔ شرکت توسعه هوشمند فرش ایرانیان (ICSD) · مجوز GPLv3

</div>

---

## این پنل چه می‌کند

یک **پنل مرکزی (hub)** روی یکی از سرورها نصب می‌شود. روی هر سرور دیگر (و خود همان سرور) یک **agent** کوچک
(یک فایل اجرایی ۶ مگابایتی، حدود ۱۵ مگابایت RAM) نصب می‌شود که خودش به پنل وصل می‌شود.

**فاز ۱ (این نسخه): مانیتورینگ و هشدار**

- داشبورد همهٔ سرورها: CPU، RAM، دیسک، شبکه، بار، مدت روشن بودن، نمودار نیم ساعت اخیر
- صفحهٔ هر سرور: نمودار از ۱ ساعت تا ۱ سال، دیسک‌ها و inodeها، **کانتینرهای Docker** (وضعیت، سلامت، CPU، حافظه)، پرمصرف‌ترین پردازه‌ها، CPU steal
- **پایش آدرس‌ها**: بالا بودن سایت، زمان پاسخ، خطای ۵xx و انقضای گواهی SSL
- **هشدار در بله و تلگرام**: دیسک پر، RAM/CPU بالا، سرور قطع، کانتینر خراب، سایت از دسترس خارج، SSL رو به انقضا؛ هر هشدار یک بار و هر «رفع شد» یک بار
- کاربران با نقش مدیر/مشاهده‌گر، رمز دوم اجباری، گزارش فعالیت

**فازهای بعد:** فایل منیجر، کنترل Docker و سرویس‌ها، ترمینال وب (فاز ۲) · بک‌آپ زمان‌بندی‌شدهٔ دیتابیس و فایل‌ها به سرور دیگر (فاز ۳) · مدیریت دامنه و SSL (فاز ۴).
سرور ایمیل عمداً در برنامه نیست؛ دلیلش در [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) آمده است.

## نصب

راهنمای قدم‌به‌قدم: **[docs/INSTALL.md](docs/INSTALL.md)**

خلاصه، روی سرور پنل (بدون Docker، کنار سایت‌های موجود؛ nginx و certbot خودکار):

```bash
cd /opt && git clone https://github.com/itwman/icsd-panel.git && cd icsd-panel
sudo bash deploy/install-hub.sh --domain panel.icsd.ir --email you@example.com
sudo icsd-hub create-admin
```

سپس در پنل «افزودن سرور» را بزنید و دستوری را که می‌دهد روی هر سرور اجرا کنید.

## ساختار مخزن

| مسیر | چیست |
|---|---|
| `agent/` | agent به زبان Go، بدون cgo و بدون وابستگی جز WebSocket (vendored) |
| `hub/` | پنل مرکزی: FastAPI + SQLite، رابط کاربری Preact بدون مرحلهٔ build و بدون CDN |
| `deploy/` | `install-hub.sh` (نصب بدون Docker با systemd و nginx)، و برای سرورهای Docker: Compose و Caddy |
| `docs/` | معماری و راهنمای نصب |
| `legacy/v1/` | کد نسخهٔ ۱ برای مرجع؛ نگه‌داری نمی‌شود |

## توسعه

```bash
# agent
cd agent && go test -race ./... && go run . collect

# hub
cd hub && pip install -r requirements-dev.txt && python -m pytest -q
ICSD_DATA_DIR=./data uvicorn icsd_hub.main:app --port 8088
```

---

## English

**ICSD Panel v2** — one hub, many Linux servers. A tiny Go agent on each server dials out to the hub over
HTTPS/WebSocket (no inbound port), the hub (FastAPI + SQLite) stores per-minute and per-hour history, evaluates
alert rules and notifies Telegram/Bale. Phase 1 is read-only monitoring: hosts, disks, Docker containers,
processes, URL/SSL checks. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/INSTALL.md](docs/INSTALL.md).
