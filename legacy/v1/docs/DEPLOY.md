# راهنمای انتشار روی گیت‌هاب و تست روی سرور — ICSD Panel

> این فایل دو بخش دارد: ۱) فرستادن پروژه به گیت‌هاب از روی کامپیوتر خودتان،
> ۲) نصب و تست روی یک سرور لینوکس.

---

## ۱) فرستادن پروژه به گیت‌هاب (از کامپیوتر خودتان)

> چرا روی کامپیوتر خودتان؟ چون نسخهٔ کامل و درست فایل‌ها روی دیسک شماست و اعتبارنامهٔ
> گیت‌هاب شما هم همان‌جاست.

### الف) ساخت مخزن روی گیت‌هاب
یکی از دو روش:

- **وب‌سایت:** وارد github.com شوید → New repository → نام مثلاً `icsd-panel` →
  Private یا Public → بدون README/gitignore (چون خودمان داریم) → Create.
- **GitHub CLI:** اگر `gh` نصب است: `gh repo create icsd-panel --private --source . --remote origin`

### ب) دستورها (در PowerShell یا CMD، داخل پوشهٔ پروژه)

```powershell
cd D:\Project\ICSDPanel

git init
git add .
git commit -m "ICSD Panel — initial commit"
git branch -M main

# آدرس مخزن خودتان را جایگزین کنید:
git remote add origin https://github.com/<USERNAME>/icsd-panel.git

git push -u origin main
```

اگر هنگام push نام کاربری/رمز خواست: به‌جای رمز، یک **Personal Access Token**
(از GitHub → Settings → Developer settings → Tokens) بسازید و آن را وارد کنید.

> فایل‌های حساس (`backend/.env`، دیتابیس `*.db`، `__pycache__`، `venv/`) توسط
> `.gitignore` نادیده گرفته می‌شوند و به مخزن نمی‌روند. ✔

---

## ۲) نصب و تست روی سرور لینوکس

روی یک سرور تازهٔ **Ubuntu 22/24** یا **AlmaLinux/Rocky 9** (با دسترسی root):

```bash
# ۱) دریافت کد
sudo apt update && sudo apt install -y git        # روی RHEL: sudo dnf install -y git
git clone https://github.com/<USERNAME>/icsd-panel.git
cd icsd-panel

# ۲) نصب کامل (پنل + وب‌استک + sudo rules)
sudo bash install.sh
#   اگر روی سرور ایران هستید، در پرسش میرور pip گزینهٔ y را بزنید.
#   برای نصب کل استک (nginx/php/db/...) به پرسش «full web stack» y بدهید.
```

پس از پایان، آدرس و اطلاعات ورود نمایش داده می‌شود:

```
پنل در دسترس است:  http://<SERVER-IP>:8088
ورود پیش‌فرض:       admin / admin12345   ← حتماً بعد از ورود تغییر دهید
```

### بررسی سلامت
```bash
systemctl status icsdpanel       # باید active (running) باشد
journalctl -u icsdpanel -f       # دیدن لاگ زنده در صورت خطا
curl -s http://localhost:8088/api/health    # باید {"status":"ok",...} بدهد
```

### تست سریع قابلیت‌ها
1. ورود با `admin / admin12345` و تغییر فوری رمز (بخش امنیت/پروفایل).
2. ویزارد راه‌اندازی خودکار باز می‌شود — چک‌لیست را دنبال کنید.
3. افزودن یک سایت آزمایشی، سپس صدور گواهی SSL.
4. بخش «پایتون/جنگو» → «قالب آماده» → یک FastAPI بسازید و دامنه بدهید.
5. وب‌هوک همان اپ را در یک ریپوی گیت‌هاب تست کنید (Settings → Webhooks).

### نکات رایج
- اگر پورت 8088 از بیرون باز نمی‌شود: فایروال ابری/سرور را بررسی کنید
  (`ufw allow 8088/tcp` یا پنل ابری).
- برای دسترسی امن، یک دامنه به پنل بدهید و جلوی آن SSL بگذارید
  (به‌جای باز کردن مستقیم پورت).
- ساخت کاربر FTP و کرون‌جاب به قوانین sudo نیاز دارد که `install.sh` نصب می‌کند؛
  اگر پنل را دستی (نه با install.sh) اجرا کردید، آن قوانین را اضافه کنید.

---

## به‌روزرسانی پنل روی سرور (بعد از تغییرات گیت‌هاب)
```bash
cd icsd-panel
git pull
sudo bash install.sh        # فایل‌ها را به /opt/icsdpanel کپی و سرویس را ری‌استارت می‌کند
```
