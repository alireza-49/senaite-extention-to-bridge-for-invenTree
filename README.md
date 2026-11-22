# پل SENAITE → InvenTree

سرویس مستقلی که دو کار انجام می‌دهد:

1. **سناریوی ۱:** وقتی آزمونی در SENAITE تأیید می‌شود (`verified`/`published`)، کالاهای مصرفی تعریف‌شده برای آن آزمون (مثلاً ۲ پلیت و ۵۰ میلی‌لیتر محیط کشت) از موجودی InvenTree کسر می‌شود.
2. **سناریوی ۲:** وقتی کالایی در InvenTree زیر حد موجودی می‌رود، هشدار تلگرام/ایمیل/وبهوک برای مسئول آزمایشگاه ارسال می‌شود.

n8n لازم نیست؛ سرویس خودش کار را انجام می‌دهد و اگر بخواهید هر رویداد را به یک وبهوک (مثلاً n8n) هم می‌فرستد.

---

## ۱) نتیجهٔ بررسی ادعاها (بر پایهٔ همین نسخهٔ نصب‌شده)

**درست بود:**

- SENAITE 2.6.0 خودش `senaite.jsonapi` را دارد و با `GET /SENAITE/@@API/senaite/v1/version` نسخه را برمی‌گرداند.
- فیلدهای لازم روی همین نسخه بررسی شد:
  - آزمون: `uid`، `getKeyword`، `getResult`، `getUnit`، `review_state`، `parent_uid`، `getRequestID`، `getServiceUID`، `getSampleTypeUID`.
  - نمونه: `getSampleTypeTitle`، `getBatchUID`، `getBatchID`، `getDateVerified`، `getDatePublished`، `review_state`.
- InvenTree: `Authorization: Token <token>`، `POST /api/stock/remove/` با بدنهٔ `{"items":[{"pk":<stock item>,"quantity":"2"}],"notes":"..."}` و فیلتر آمادهٔ `GET /api/part/?low_stock=true`.

**اصلاح شد:**

- **InvenTree وبهوک خروجی ندارد** (فقط وبهوک دریافتی)؛ پس تشخیص کم‌موجودی با Polling روی `low_stock=true` انجام می‌شود.
- **SENAITE هم وبهوک خروجی ندارد**؛ پس سرویس API را دوره‌ای می‌خواند. این روش به سرور SENAITE دست نمی‌زند و روی هر نسخه‌ای قابل استفاده است.
- آزمون پس از انتشار نمونه از `verified` به `published` می‌رود؛ به همین دلیل **هر دو وضعیت** خوانده و بر اساس UID آزمون dedupe می‌شود.
- «۲ پلیت و ۵۰ میلی‌لیتر» از نتیجهٔ آزمون قابل استنتاج نیست و در `config.yml` تعریف می‌شود.
- یک نکتهٔ مهم که در حین ساخت پیدا شد: SENAITE با **اعتبارنامهٔ غلط هم HTTP 200** برمی‌گرداند (فقط لیست خالی). بنابراین بررسی ورود با `users/current` و فیلد `authenticated` انجام می‌شود، نه با خالی‌بودن لیست.

---

## ۲) نصب

### الف) با docker compose (کنار همان SENAITE موجود)

```bash
cd integration/senaite-inventree-bridge
cp config.example.yml config.yml
cp .env.example .env          # مقادیر را پر کنید (این فایل نباید commit شود)
cd ../..
docker compose --profile bridge up -d --build bridge
docker compose --profile bridge logs -f bridge
```

سرویس `bridge` با profile تعریف شده است، بنابراین `docker compose up` فعلی شما رفتارش تغییر نمی‌کند.

### ب) اجرای مستقیم (هر سروری با Python 3.9+)

```bash
cd integration/senaite-inventree-bridge
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
export SENAITE_USER=... SENAITE_PASSWORD=... INVENTREE_TOKEN=...
./.venv/bin/python -m bridge --config config.yml check
./.venv/bin/python -m bridge --config config.yml --dry-run once
```

---

## ۳) پیکربندی و نقشه‌برداری

کپی `config.example.yml` به `config.yml`. اسرار به‌صورت `${VAR}` از محیط خوانده می‌شوند (`${VAR:-default}` هم پشتیبانی می‌شود).

نقشهٔ مصرف، تنها جایی است که باید ویرایش شود:

```yaml
rules:
  - id: microbiology-tpc
    senaite:
      keywords: ["Total Plate Count", "TPC"]   # زیررشته، بی‌توجه به حروف بزرگ/کوچک
      sample_types: ["water"]                  # تطابق کامل عنوان نوع نمونه
    consume:
      - {part_ipn: "PETRI-90", quantity: 2}
      - {part_ipn: "PCA-MEDIUM", quantity: 50, location: "Micro Lab"}
```

- `part_ipn` (یا `part_pk`) کالا را در InvenTree پیدا می‌کند.
- `location` اختیاری است و موجودی را فقط از آن انبار برمی‌دارد.
- هر دو فیلتر `keywords` و `sample_types` وقتی خالی باشند یعنی «هر».
- اگر یک قاعده چند کالا بخواهد و **موجودی یکی کافی نباشد، هیچ‌کدام کسر نمی‌شود**.

تخصیص موجودی بین چند stock item (مثلاً چند بچ با انقضای مختلف) به ترتیب **نزدیک‌ترین تاریخ انقضا** انجام می‌شود.

---

## ۴) حالت‌های اجرا

| دستور | کار |
| --- | --- |
| `python -m bridge check` | اعتبارسنجی config، ورود به SENAITE، اتصال InvenTree و **وجود و کفایت موجودی** همهٔ کالاهای قواعد (فقط‌خواندنی) |
| `python -m bridge --dry-run once` | یک چرخهٔ کامل بدون هیچ تغییری در InvenTree |
| `python -m bridge once` | یک چرخهٔ واقعی |
| `python -m bridge run` | حلقهٔ دائمی + سرور HTTP |
| `python -m bridge notify-test` | ارسال پیام آزمایشی از کانال‌های تنظیم‌شده |
| `python -m bridge events --limit 20` | مشاهدهٔ آخرین رویدادهای ژورنال |

گزینه‌های مفید: `--dry-run`، `--json`، `--since 2026-11-01T00:00:00+00:00` (فقط برای backfill آگاهانه).

### سرور HTTP

- `GET /healthz` → بدون نیاز به توکن: `{"status":"ok","version":"1.0.0","dry_run":false}`. سرور HTTP همیشه بالا می‌آید تا healthcheck داکر کار کند.
- `POST /jobs/poll`، `POST /jobs/low-stock`، `POST /jobs/once` → فقط با `Authorization: Bearer $BRIDGE_TOKEN`؛ اگر توکن خالی باشد این مسیرها با 503 بسته می‌مانند (فقط `/healthz` در دسترس است).

---

## ۵) ایمنی، تکرار و حسابرسی

- **اولین اجرا از «حالا» شروع می‌شود؛** آزمون‌های قدیمی‌تر به‌طور انبوه کسر نمی‌شوند. برای backfill عمدی، `senaite.initial_lookback_days` یا `--since` را تنظیم کنید.
- **Idempotency:** کلید `analysis_uid + rule_id` در SQLite. اجرای دوباره، ری‌استارت یا رترای HTTP باعث کسر دوباره نمی‌شود.
- ترتیب ایمن: ابتدا رکورد `pending` نوشته می‌شود، بعد درخواست کسر، بعد ثبت نتیجه. اگر سرویس دقیقاً بین این دو قطع شود، رکورد در استارت بعدی `unknown` می‌شود و **به‌صورت خودکار تکرار نمی‌شود** (هشدار برای بررسی دستی) تا کسری دوباره رخ ندهد.
- موجودی ناکافی یا خطای InvenTree باعث ثبت `insufficient`/`failed` و تلاش مجدد در چرخهٔ بعد می‌شود؛ هشدار فقط یک‌بار (تا تغییر وضعیت) ارسال می‌شود.
- اگر InvenTree نسخهٔ قدیمی باشد و کسر تک‌تک stock itemها وسط کار fail شود، رکورد `unknown` می‌شود و تکرار نمی‌شود (وگرنه آیتم‌های کسرشده دوباره کم می‌شدند)؛ هشدار مخصوص با فهرست آیتم‌های کسرشده ارسال می‌شود.
- هشدار کم‌موجودی فقط وقتی «ارسال‌شده» ثبت می‌شود که واقعاً یک کانال موفق بوده باشد؛ اگر تلگرام/ایمیل خطا بدهد، در چرخهٔ بعد دوباره تلاش می‌شود.
- در `--dry-run` نه موجودی تغییر می‌کند، نه رکورد مصرف نوشته می‌شود و نه وضعیت هشدار کم‌موجودی تغییر می‌کند؛ گزارش با `dry_run: true` و شمارندهٔ `would_consume` برمی‌گردد.
- `check` فقط‌خواندنی است و رکورد `pending` را دست نمی‌زند (حتی اگر سرویس اصلی هم‌زمان در حال اجرا باشد).
- هشدار کم‌موجودی هنگام عبور از حد یک‌بار می‌رود، بعد از `repeat_after_hours` تکرار می‌شود و با برگشت موجودی ریست می‌شود.
- اسرار فقط در `.env`؛ در لاگ‌ها و `check` ماسک می‌شوند. کاربر SENAITE را فقط‌خواندنی و توکن InvenTree را با دسترسی Stock Change بگیرید.

---

## ۶) تست

```bash
cd integration/senaite-inventree-bridge
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt pytest
./.venv/bin/python -m pytest
```

تست‌ها شامل دو سرور جعلی SENAITE/InvenTree هستند و این موارد را پوشش می‌دهند: کسر درست، عدم کسر دوباره، موجودی ناکافی، بازگشت پس از تأمین موجودی، dry-run (بدون هیچ نوشتنی)، آزمون قدیمی‌تر از نصب، وضعیت published، کسر نامشخص پس از crash، کسر نیمه‌کاره در fallback تک‌آیتمی، چرخهٔ هشدار کم‌موجودی (شامل خطای کانال) و توکن HTTP.

---

## ۷) محدودیت‌های شناخته‌شده

- **کسر واقعی از InvenTree شما آزمایش نشده است** (در زمان ساخت، نمونهٔ InvenTree در دسترس نبود). ابتدا `check` و سپس `--dry-run once` را اجرا کنید، بعد یک اجرای واقعی روی یک کالای آزمایشی.
- نسخهٔ InvenTree مهم است؛ برای نسخه‌های قدیمی‌تر که `POST /api/stock/remove/` ندارند، فراخوانی به endpoint تک‌تک stock item برمی‌گردد (تست نشده روی نسخهٔ قدیمی).
- **بازگشت خودکار وجود ندارد:** اگر تأیید آزمون باطل/retracted شود، موجودی برنمی‌گردد. اصلاح دستی در InvenTree لازم است.
- اگر آزمون مربوط به نمونه‌ای باشد که **قبل از نصب سرویس** ساخته شده و بعداً تأیید شود، به‌طور پیش‌فرض کسر نمی‌شود؛ برای پوشش آن `senaite.initial_lookback_days` را مقدار دهید.
- در هر poll حداکثر `batch_size` آزمون (پیش‌فرض ۲۰۰) خوانده می‌شود؛ در صورت پر شدن صفحه، `truncated: true` در گزارش ظاهر می‌شود.

---

## ۸) ساختار

```
bridge/
  __main__.py   CLI (check / once / run / notify-test / events)
  config.py     خواندن YAML، جای‌گذاری env، اعتبارسنجی و redaction
  senaite.py    کلاینت JSON API + بررسی واقعی ورود
  inventree.py  کالا، stock item، تخصیص موجودی، remove/add، low_stock
  rules.py      تطبیق keyword/نوع نمونه با قواعد
  journal.py    SQLite: dedupe، audit، وضعیت هشدار کم‌موجودی
  notifier.py   تلگرام / SMTP / وبهوک + متن پیام‌های فارسی
  service.py    دو چرخهٔ اصلی و منطق کسر
  http.py       /healthz و /jobs/* با Bearer token
tests/          تست‌های واحد و یکپارچه با سرورهای جعلی
n8n/            workflow نمونهٔ اختیاری برای مصرف رویدادها
```
# senaite-extention-to-bridge-for-invenTree
