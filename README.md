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

**مانیتورینگ و هشدار (فاز ۱)**

- داشبورد همهٔ سرورها: CPU، RAM، دیسک، شبکه، بار، نمودار از ۱ ساعت تا ۱ سال، کانتینرهای Docker، پرمصرف‌ترین پردازه‌ها
- پایش آدرس‌ها (خطای ۵xx، زمان پاسخ، انقضای SSL) و هشدار در بله و تلگرام

**مدیریت (فاز ۲)**

- **فایل منیجر:** مرور، ویرایش در مرورگر، آپلود تا ۵۱۲ مگابایت، دانلود، تغییر نام، دسترسی، حذف، فشرده‌سازی و باز کردن zip/tar.gz
- **سرویس‌ها و کانتینرها:** شروع، توقف، ری‌استارت و لاگ
- **کنسول اجرای دستور:** به‌طور پیش‌فرض خاموش است

**دیتابیس و بک‌آپ (فاز ۳)**

- MySQL/MariaDB و PostgreSQL، روی سرور یا داخل Docker: فهرست و حجم، بک‌آپ فوری، بازگردانی با نسخهٔ احتیاطی خودکار
- **برنامه‌های بک‌آپ زمان‌بندی‌شده** برای دیتابیس یا پوشه‌ها:
  - پیش از شروع بررسی می‌کند فضای دیسک کافی باشد.
  - چند نسخهٔ آخر را نگه می‌دارد و بقیه را پاک می‌کند.
  - هر نسخه را به سرور دیگر کپی می‌کند و می‌تواند نسخهٔ محلی را حذف کند (برای دیسک‌های کوچک).
  - اگر بک‌آپ شکست بخورد هشدار می‌دهد.

**دامنه‌ها و SSL (فاز ۴)**

- گواهی‌های certbot و Caddy با تاریخ انقضا و هشدار، تمدید و گرفتن گواهی برای سایت‌های nginx
- بررسی اینکه DNS هر دامنه به همان سرور اشاره کند
- بررسی رکوردهای ایمیل (MX، SPF، DMARC)؛ سرور ایمیل عمداً اجرا نمی‌شود

**امنیت**

- فقط مدیر به بخش‌های مدیریتی دسترسی دارد.
- هر تغییر روی سرورها کد تازهٔ رمز دوم می‌خواهد (۱۵ دقیقه معتبر) و در گزارش فعالیت ثبت می‌شود.
- آنچه پنل مجاز است روی هر سرور انجام دهد، **روی خود همان سرور** در `/etc/icsd-agent/policy.json` تعیین می‌شود؛ حتی پنلِ نفوذشده هم از آن فراتر نمی‌رود.

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
