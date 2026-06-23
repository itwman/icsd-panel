<div align="center">

# 🟢 ICSD Panel

**پنل متن‌باز مدیریت سرور لینوکس — رایگان، فارسی/انگلیسی، ساختهٔ شرکت توسعه هوشمند فرش ایرانیان**

**Open-source Linux server management panel — Free, Persian/English, by Iranian Carpet Smart Development (ICSD)**

[![License: GPLv3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
![Status](https://img.shields.io/badge/status-alpha-orange)
![Python](https://img.shields.io/badge/Python-3.10%2B-green)

</div>

---

## 🇮🇷 فارسی

### معرفی
**ICSD Panel** یک پنل وب مدرن و رایگان برای مدیریت کامل سرورهای لینوکسی است؛ با تمرکز ویژه بر نیازها و محدودیت‌های شبکه‌ای کاربران ایرانی. جایگزینی متن‌باز برای cPanel و Plesk، بدون قابلیت‌های پولی پنهان.

### قابلیت‌ها (نقشهٔ راه)
- 📊 **داشبورد مانیتورینگ زنده** — CPU، RAM، دیسک، پهنای باند و وضعیت سرویس‌ها به‌صورت Real-time *(در دسترس در فاز ۰)*
- 🌐 **مدیریت سایت و دامنه** — ساخت وب‌سایت، تولید خودکار کانفیگ Nginx/Apache
- 🔒 **SSL لتس‌انکریپت** — صدور و تمدید خودکار با راهکار مقاوم در برابر محدودیت‌های ایران (DNS-01 + چند CA)
- 🗄️ **دیتابیس و بک‌آپ** — مدیریت MySQL/MariaDB و بک‌آپ خودکار روی هاست دیگر از طریق FTP/SFTP
- 🇮🇷 **ساختهٔ ایران** — رابط کامل RTL فارسی، تقویم شمسی، نصب‌کنندهٔ آگاه از میرورهای داخلی، اعلان تلگرام/بله/پیامک

### نصب سریع
```bash
git clone https://github.com/ICSD/ICSDPanel.git
cd ICSDPanel
sudo bash install.sh
```
سپس پنل را در آدرس `http://<IP-سرور>:8088` باز کنید.

### سیستم‌عامل‌های پشتیبانی‌شده
| توزیع | وضعیت |
|---|---|
| Ubuntu 22.04 / 24.04 LTS | ✅ |
| Debian 12 | ✅ |
| AlmaLinux 9 | ✅ |
| Rocky Linux 9 | ✅ |
| CentOS Stream 9 | ⚠️ با احتیاط |
| CentOS Linux 7/8 | ❌ EOL — مهاجرت کنید |

> ⚠️ **CentOS Linux** در ژوئن ۲۰۲۴ به پایان عمر رسیده. به **AlmaLinux 9** یا **Rocky Linux 9** مهاجرت کنید.

### اجرای محیط توسعه
```bash
cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8088
# داشبورد:  http://localhost:8088    |    مستندات API:  http://localhost:8088/docs
```

---

## 🇬🇧 English

### Overview
**ICSD Panel** is a modern, free, open-source web panel for full Linux server management, purpose-built for the needs and network constraints of Iranian users. A genuine cPanel/Plesk alternative with no hidden paid features.

### Features (roadmap)
- 📊 **Live monitoring dashboard** — real-time CPU, RAM, disk, bandwidth, services *(available in Phase 0)*
- 🌐 **Site & domain management** — websites with auto-generated Nginx/Apache configs
- 🔒 **Let's Encrypt SSL** — auto issue/renew with Iran-resilient strategy (DNS-01 + multi-CA fallback)
- 🗄️ **Databases & backups** — MySQL/MariaDB management and scheduled FTP/SFTP backups to a remote host
- 🇮🇷 **Built for Iran** — full Persian RTL UI, Jalali calendar, mirror-aware installer, Telegram/Bale/SMS alerts

### Quick install
```bash
git clone https://github.com/ICSD/ICSDPanel.git
cd ICSDPanel
sudo bash install.sh
```
Then open `http://<server-ip>:8088`.

### Supported OS
Ubuntu 22.04/24.04, Debian 12, AlmaLinux 9, Rocky Linux 9. CentOS Stream 9 with caution; **CentOS Linux 7/8 are EOL — migrate to AlmaLinux/Rocky.**

### Architecture
A privilege-separated design: an unprivileged FastAPI web layer talks to a privileged system agent over a local socket. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full technical design, competitor analysis, and roadmap.

---

## 🤝 مشارکت / Contributing
این پروژه متن‌باز است و از مشارکت جامعهٔ ایرانی استقبال می‌کند. Issue و Pull Request بفرستید.
This is an open-source project welcoming contributions. Please open issues and pull requests.

## 📄 مجوز / License
GPLv3 — see [LICENSE](LICENSE). © Iranian Carpet Smart Development (ICSD).
