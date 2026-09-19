# ربات جست‌وجوی CSV در Telegram

این پروژه فایل‌های CSV بزرگ را به‌صورت مرحله‌ای وارد یک دیتابیس SQLite می‌کند و امکان جست‌وجوی سریع و محدودشده را از طریق ربات Telegram فراهم می‌سازد.

ربات از header واقعی هر فایل استفاده می‌کند. بنابراین فایل‌های مختلف می‌توانند ستون‌های متفاوتی داشته باشند؛ برای مثال:

```csv
id,name,phone,address,email
7,ثمین,09120000000,تهران میدان انقلاب,samin@example.com
```

در این حالت، نتیجه با نام واقعی ستون‌ها نمایش داده می‌شود:

```text
id: 7
name: ثمین
phone: 09120000000
address: تهران میدان انقلاب
email: samin@example.com
```

ستون‌هایی که مقدار خالی دارند نمایش داده نمی‌شوند. جست‌وجو نیز روی تمام ستون‌ها انجام می‌شود؛ بنابراین عبارت‌هایی مانند آدرس، ایمیل، نام کاربری یا هر مقدار دیگری قابل جست‌وجو هستند.

> این نرم‌افزار فقط باید برای داده‌هایی استفاده شود که اجازه قانونی و مدیریتی ذخیره‌سازی و جست‌وجوی آن‌ها را دارید.

---

## امکانات اصلی

- جست‌وجوی دقیق روی شناسه، نام، تاریخ، تلفن و سایر فیلدها
- جست‌وجوی prefix برای شناسه‌ها، تلفن‌ها و تاریخ‌ها
- جست‌وجوی متن در تمام ستون‌های CSV
- پشتیبانی از header متفاوت برای هر فایل
- نمایش نام واقعی ستون‌ها در نتایج
- حذف خودکار ستون‌های خالی از خروجی
- پشتیبانی از نام‌ها و متن فارسی
- نرمال‌سازی حروف عربی و فارسی و اعداد
- دریافت CSV از چت خصوصی Telegram
- پردازش streaming برای فایل‌های بزرگ
- جلوگیری از شکستن رکوردهای CSV هنگام تقسیم فایل
- نگهداری وضعیت import فایل‌ها
- حذف و import مجدد امن فایل‌های تغییرکرده
- پشتیبانی از Ubuntu/Debian، VPS، Railway، Docker و محیط‌های container
- ایجاد خودکار دیتابیس خالی در اولین اجرا، حتی بدون وجود CSV

---

## نصب سریع با یک دستور

پس از اینکه این پروژه را در GitHub خودتان قرار دادید و فایل `install.sh` در شاخه `main` قرار گرفت، روی سرور یا محیط موردنظر اجرا کنید:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/arefhere51/telegram_csv_search/main/install.sh)
```

برای Railway، Docker و محیط‌های بدون systemd، حالت foreground را صریحاً مشخص کنید:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/arefhere51/telegram_csv_search/main/install.sh) --mode foreground
```

اسکریپت نصب تک‌دستوری این کارها را انجام می‌دهد:

1. دریافت یا به‌روزرسانی پروژه از GitHub
2. تشخیص محیط اجرا
3. انتخاب systemd برای VPS واقعی یا foreground برای Railway/Docker
4. نصب وابستگی‌های Python
5. اجرای wizard تنظیمات
6. دریافت امن توکن ربات و آیدی عددی Telegram
7. ساخت فایل `.env`
8. ساخت دیتابیس خالی در صورت نبودن CSV
9. import فایل‌های موجود در صورت وجود
10. آماده‌کردن اجرای دائمی ربات

توکن در زمان ورود مخفی است و در خروجی چاپ نمی‌شود.

### گزینه‌های نصب

نمایش راهنما:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/arefhere51/telegram_csv_search/main/install.sh) --help
```

نصب بدون import اولیه:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/arefhere51/telegram_csv_search/main/install.sh) --skip-import
```

ساخت تنظیمات بدون اجرای ربات:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/arefhere51/telegram_csv_search/main/install.sh) --no-start
```

اجبار به اجرای systemd:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/arefhere51/telegram_csv_search/main/install.sh) --mode systemd
```

اجبار به اجرای container/foreground:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/arefhere51/telegram_csv_search/main/install.sh) --mode foreground
```

> در حالت `auto`، ویزارد فقط زمانی systemd را انتخاب می‌کند که `systemctl` وجود داشته باشد و پردازش شماره ۱ سیستم واقعاً `systemd` باشد. به همین دلیل در Railway یا Docker با خطای systemd متوقف نمی‌شود.

---

## سؤال‌های wizard

در زمان اجرای wizard، سؤال‌های زیر پرسیده می‌شوند:

```text
Paste your BotFather token:
Enter numeric Telegram user ID(s), comma-separated:
Do your CSV files normally contain a header row? [y]:
CSV delimiter (one character) [,]:
CSV encoding [utf-8-sig]:
Allow authorized private-chat CSV uploads? [y]:
Delete CSV after successful import? [n]:
```

### توکن ربات

توکن را از `@BotFather` دریافت کنید. نمونه شکل توکن:

```text
123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

توکن را در GitHub یا پیام عمومی قرار ندهید.

### آیدی عددی Telegram

آیدی باید عددی باشد، نه username. نمونه صحیح:

```text
149772217
```

برای چند کاربر:

```text
149772217,123456789
```

نمونه‌های نادرست:

```text
@my_username
ID: 149772217
```

### پاسخ پیشنهادی برای CSVهای معمولی

```text
Header row: y
Delimiter: ,
Encoding: utf-8-sig
Allow uploads: y
Delete after import: n
```

---

## نصب روی VPS واقعی Ubuntu یا Debian

روی VPS واقعی که systemd فعال دارد، اجرا کنید:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/arefhere51/telegram_csv_search/main/install.sh)
```

ویزارد به‌صورت خودکار:

- کاربر سیستمی `csvsearch` را می‌سازد.
- virtualenv ایجاد می‌کند.
- وابستگی‌ها را نصب می‌کند.
- فایل systemd را نصب می‌کند.
- سرویس را فعال می‌کند.
- ربات را اجرا می‌کند.

بررسی وضعیت سرویس:

```bash
systemctl status telegram-csv-search
```

مشاهده لاگ زنده:

```bash
journalctl -u telegram-csv-search -f
```

راه‌اندازی مجدد:

```bash
systemctl restart telegram-csv-search
```

---

## نصب و اجرا روی Railway

Railway معمولاً برنامه را داخل container اجرا می‌کند و systemd ندارد. بنابراین نباید از `systemctl` استفاده شود.

### تنظیمات Railway

در بخش **Variables** این متغیرها را اضافه کنید:

```env
TELEGRAM_BOT_TOKEN=توکن_واقعی_ربات
ALLOWED_USER_IDS=149772217

DATA_DIRECTORY=/data/csv
DATABASE_PATH=/data/database/search.db
LOG_DIRECTORY=/data/logs

CSV_DELIMITER=,
CSV_ENCODING=utf-8-sig
CSV_HAS_HEADER=true
CSV_COLUMN_NAMES=column_1,column_2,name,birth_date,phone

UPLOAD_ENABLED=true
DELETE_CSV_AFTER_IMPORT=false
```

> بهتر است توکن را در بخش Variables تنظیم کنید، نه داخل فایل `.env` در GitHub.

### Start Command

در تنظیمات Railway، مقدار Start Command را این قرار دهید:

```bash
./railway_start.sh
```

این فایل:

1. Python داخل `venv` را در صورت وجود انتخاب می‌کند.
2. اگر CSV موجود باشد، آن را import می‌کند.
3. اگر CSV وجود نداشته باشد، import را رد می‌کند.
4. دیتابیس خالی را توسط `bot.py` ایجاد می‌کند.
5. ربات را در foreground اجرا می‌کند.

بنابراین می‌توانید بدون قرار دادن هیچ CSV اولیه‌ای ربات را بالا بیاورید و اولین فایل را از Telegram ارسال کنید.

### Volume در Railway

فایل‌های داخل container ممکن است بعد از redeploy یا restart پاک شوند. برای حفظ دیتابیس، CSVها و لاگ‌ها یک Railway Volume بسازید و آن را روی مسیر زیر mount کنید:

```text
/data
```

سپس این متغیرها را تنظیم کنید:

```env
DATA_DIRECTORY=/data/csv
DATABASE_PATH=/data/database/search.db
LOG_DIRECTORY=/data/logs
```

بدون Volume، دیتابیس و فایل‌های آپلودی ممکن است بعد از redeploy از بین بروند.

---

## اجرای Docker

در Docker نیز نباید systemd اجرا شود. از entrypoint پروژه استفاده کنید:

```dockerfile
CMD ["./railway_start.sh"]
```

یا مستقیماً:

```bash
./railway_start.sh
```

پردازش اصلی container باید همان `bot.py` باشد تا Docker یا Railway بتواند وضعیت آن را مدیریت کند.

---

## اجرای دستی روی سرور

اگر virtualenv قبلاً ساخته شده است:

```bash
cd /telegram_csv_search
./venv/bin/python import_csv.py
./venv/bin/python bot.py
```

در محیط Railway/Docker، دستور بهتر این است:

```bash
cd /telegram_csv_search
./railway_start.sh
```

---

## اجرای بدون CSV و دریافت اولین فایل از Telegram

اگر پوشه `data` خالی باشد، این رفتار طبیعی است:

```text
No CSV/TXT source files found; starting with an empty database.
```

پس از آن ربات اجرا می‌شود و می‌توانید:

1. در چت خصوصی Telegram دستور `/start` را بفرستید.
2. فایل CSV را ارسال کنید.
3. ربات فایل را دانلود می‌کند.
4. فایل در صف import قرار می‌گیرد.
5. header همان فایل خوانده می‌شود.
6. دیتابیس و indexها به‌روزرسانی می‌شوند.
7. جست‌وجو روی اطلاعات فایل انجام می‌شود.

---

## فرستادن CSV از Telegram

### فایل‌های بزرگ تا حدود ۲ گیگابایت

سرویس ابری معمولی Telegram Bot API برای دانلود فایل با `getFile` محدودیت حدود ۲۰ مگابایت دارد. برای فایل‌های بزرگ‌تر باید **Local Bot API Server رسمی Telegram** را روی VPS یا همان محیط شبکه‌ای اجرا کنید و دو متغیر زیر را به آدرس آن تغییر دهید:

```env
TELEGRAM_BASE_URL=http://127.0.0.1:8081/bot
TELEGRAM_FILE_BASE_URL=http://127.0.0.1:8081/file/bot
UPLOAD_MAX_BYTES=2000000000
```

Local Bot API Server باید به همان bot token دسترسی داشته باشد. صرفاً زیادکردن `UPLOAD_MAX_BYTES` روی API ابری، محدودیت ۲۰ مگابایت Telegram را برطرف نمی‌کند. ربات از حساب کاربری، Telethon یا Pyrogram استفاده نمی‌کند؛ فقط از Bot API رسمی استفاده می‌شود.

برای دانلود فایل، ربات آن را به‌صورت streaming روی دیسک می‌نویسد و کل فایل را در RAM نگه نمی‌دارد. بنابراین باید فضای کافی برای فایل موقت، دیتابیس SQLite، WAL و indexها وجود داشته باشد.

### progress زنده دانلود و import

پس از ارسال فایل، ربات یک پیام وضعیت را مرتب ویرایش می‌کند و نوار گرافیکی، درصد، حجم دریافت‌شده، سرعت و مرحله فعلی را نشان می‌دهد:

```text
⬇️ دانلود فایل
bank.csv
[██████████░░░░░░░░░░]  50.0%
حجم: 950.0 MiB / 1.9 GiB
سرعت: 18.4 MiB/s
```

در مرحله import نیز همان پیام با تعداد رکوردهای خوانده‌شده، تعداد رکوردهای درج‌شده، سرعت و درصد تقریبی فایل به‌روزرسانی می‌شود. فاصله پیش‌فرض ویرایش پیام ۳ ثانیه است و با این متغیر قابل تغییر است:

```env
UPLOAD_PROGRESS_INTERVAL_SECONDS=3
```

برای آپلود موفق، شرایط زیر باید برقرار باشد:

- پیام در چت خصوصی ربات باشد.
- آیدی فرستنده در `ALLOWED_USER_IDS` باشد.
- `UPLOAD_ENABLED=true` باشد.
- پسوند فایل `.csv` یا `.txt` باشد.
- حجم فایل از `UPLOAD_MAX_BYTES` بیشتر نباشد.
- فضای ذخیره‌سازی کافی وجود داشته باشد.

روند پیام‌ها معمولاً چنین است:

```text
File received
Downloading to the server...
Download completed
Queued for streaming import
Import started
Import progress
Import completed
```

فایل اصلی بعد از import با توجه به مقدار `DELETE_CSV_AFTER_IMPORT` نگه داشته یا حذف می‌شود.

---

## تقسیم فایل CSV بزرگ

اسکریپت `split_csv_for_bot.py` فایل بزرگ را بدون شکستن رکوردهای CSV به قطعه‌های کوچک‌تر تقسیم می‌کند. این اسکریپت از RAM محدودی استفاده می‌کند و متن‌های چندخطی داخل فیلدهای quoted را نیز درست مدیریت می‌کند.

### فایل دارای header

```bash
cd /telegram_csv_search
mkdir -p data/parts
./venv/bin/python split_csv_for_bot.py \
  data/bank.csv \
  data/parts \
  --max-mb 19.0 \
  --has-header
```

خروجی:

```text
bank_part_001.csv
bank_part_002.csv
bank_part_003.csv
```

با گزینه `--has-header`، header در ابتدای تمام قطعه‌ها تکرار می‌شود.

### فایل بدون header

```bash
./venv/bin/python split_csv_for_bot.py \
  data/bank.csv \
  data/parts \
  --max-mb 19.0
```

### تغییر prefix خروجی

```bash
./venv/bin/python split_csv_for_bot.py \
  data/bank.csv \
  data/parts \
  --max-mb 19.0 \
  --has-header \
  --prefix customer_data
```

برای محدودیت Telegram، مقدار `19.0` مگابایت انتخاب امنی است. قطعه‌ها را یکی‌یکی برای ربات ارسال کنید.

اگر یک رکورد به‌تنهایی از اندازه قطعه بزرگ‌تر باشد، ابزار خطا می‌دهد؛ زیرا رکورد CSV نباید نصف شود.

---

## رفتار header و ستون‌ها

وقتی `CSV_HAS_HEADER=true` باشد، ردیف اول هر فایل به‌عنوان header خوانده می‌شود و همان header در نتایج استفاده می‌شود.

مثال:

```csv
national_id,fullname,mobile,address
123,حسین,09120000000,تهران
```

ربات این فیلدها را همان‌طور نمایش می‌دهد:

```text
national_id: 123
fullname: حسین
mobile: 09120000000
address: تهران
```

اگر فایل دیگری چنین ساختاری داشته باشد:

```csv
id,name,email
456,ثمین,samin@example.com
```

فقط همان ستون‌های موجود نمایش داده می‌شوند. برای این رکورد چیزی به نام `address` ساخته نمی‌شود.

برای فایل‌های بدون header:

```env
CSV_HAS_HEADER=false
CSV_COLUMN_NAMES=column_1,column_2,name,birth_date,phone
```

در این حالت نام ستون‌ها از روی جایگاه آن‌ها تعیین می‌شود.

---

## import فایل‌های جدید

پس از قرار دادن CSV جدید در پوشه `data`:

```bash
sudo -u csvsearch /opt/telegram_csv_search/venv/bin/python \
  /opt/telegram_csv_search/import_csv.py
```

در محیط container:

```bash
./venv/bin/python import_csv.py
```

برای پردازش دوباره همه فایل‌ها:

```bash
./venv/bin/python import_csv.py --force
```

برای ساخت مجدد indexها:

```bash
./venv/bin/python import_csv.py --rebuild-indexes
```

برای توقف در اولین خطا:

```bash
./venv/bin/python import_csv.py --stop-on-error
```

---

## مسیرهای مهم

| مورد | مسیر پیش‌فرض |
|---|---|
| پروژه | `/opt/telegram_csv_search` یا `/telegram_csv_search` |
| تنظیمات | `.env` |
| فایل‌های CSV | `data/` |
| دیتابیس | `database/search.db` |
| لاگ ربات | `logs/bot.log` |
| لاگ import | `logs/importer.log` |
| محیط Python | `venv/` |
| سرویس systemd | `telegram-csv-search` |

---

## خطاهای متداول

### خطای نبودن توکن

```text
Configuration error: TELEGRAM_BOT_TOKEN is required
```

در Railway متغیر زیر را اضافه کنید:

```env
TELEGRAM_BOT_TOKEN=توکن_واقعی_ربات
```

### خطای دسترسی کاربر

```text
Access denied.
```

بررسی کنید آیدی عددی شما در این متغیر باشد:

```env
ALLOWED_USER_IDS=149772217
```

### خطای نبودن دیتابیس

در نسخه جدید، `bot.py` باید دیتابیس خالی را خودکار بسازد. اگر نسخه قدیمی دارید، از نسخه جدید استفاده کنید یا این دستور را اجرا کنید:

```bash
./venv/bin/python -c "from config import load_settings; from database import connect, initialize_database; s=load_settings(); c=connect(s.database_path); initialize_database(c); c.close()"
```

### خطای `ModuleNotFoundError: dotenv`

یعنی Python اشتباه اجرا شده است. از Python virtualenv استفاده کنید:

```bash
./venv/bin/python bot.py
```

یا:

```bash
./venv/bin/python -m pip install -r requirements.txt
```

### خطای systemd در Railway

```text
System has not been booted with systemd as init system
```

در Railway نباید `systemctl` اجرا شود. Start Command را روی این مقدار بگذارید:

```bash
./railway_start.sh
```

### خطای import هنگام گزارش پیشرفت

نسخه‌های قدیمی ممکن است این خطا را نشان دهند:

```text
OSError: telling position disabled by next() call
```

از نسخه جدید `import_csv.py` استفاده کنید. این مشکل در نسخه فعلی اصلاح شده است.

### خطای کمبود فضا

بررسی فضا:

```bash
df -h
df -i
```

برای فایل بزرگ باید فضای فایل اصلی، قطعه‌ها، دیتابیس SQLite، WAL و indexها را در نظر بگیرید.

---

## نکات امنیتی

- توکن Telegram را در GitHub commit نکنید.
- فایل `.env` را عمومی نکنید.
- مقدار `ALLOWED_USER_IDS` را محدود نگه دارید.
- برای Railway از Variables استفاده کنید.
- برای داده‌های حساس از Volume خصوصی استفاده کنید.
- دیتابیس، CSVها و لاگ‌ها را در اختیار افراد غیرمجاز قرار ندهید.
- ربات فقط برای داده‌هایی استفاده شود که مجوز ذخیره و جست‌وجوی آن‌ها را دارید.

---

## بررسی نسخه نصب‌شده

برای اطمینان از اینکه نسخه جدید importer نصب شده است:

```bash
grep -n "file_handle.buffer.tell" import_csv.py
```

باید خطی مانند این نمایش داده شود:

```text
position = file_handle.buffer.tell()
```

برای بررسی syntax:

```bash
python3 -m py_compile *.py
bash -n install.sh
bash -n install_vps.sh
bash -n railway_start.sh
```

برای اجرای تست‌ها:

```bash
python3 -m unittest discover -s tests -v
```

---

## خلاصه نصب پیشنهادی روی Railway

```bash
bash <(curl -Ls https://raw.githubusercontent.com/arefhere51/telegram_csv_search/main/install.sh) --mode foreground
```

در Railway Variables:

```env
TELEGRAM_BOT_TOKEN=توکن_واقعی
ALLOWED_USER_IDS=149772217
CSV_HAS_HEADER=true
CSV_DELIMITER=,
CSV_ENCODING=utf-8-sig
UPLOAD_ENABLED=true
DELETE_CSV_AFTER_IMPORT=false
```

Start Command:

```bash
./railway_start.sh
```

اگر فایل CSV اولیه ندارید، مشکلی نیست. دیتابیس خالی ساخته می‌شود و اولین CSV را می‌توانید از Telegram برای ربات ارسال کنید.
