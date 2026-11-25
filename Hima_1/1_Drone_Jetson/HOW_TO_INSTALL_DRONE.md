# تثبيت حِمَى على الدرون (Jetson Orin)

الجهاز: Jetson Orin بنظام JetPack 6 (Ubuntu 22.04، بايثون 3.10). أول تثبيت يحتاج إنترنت مرة واحدة.

## 1. انسخ المجلد إلى الجيتسون

ركّب ذاكرة USB في الجيتسون، ثم انسخ مجلد `1_Drone_Jetson` كاملًا (فيه `system/` و`data_ext/` معًا):

```bash
cp -r "/media/$USER/<اسم الذاكرة>/Hima_1/1_Drone_Jetson" ~/hima
cd ~/hima/system
```

شغّل البرنامج من نسخة الجيتسون، لا من الذاكرة نفسها: الرحلة تكتب ملفاتها بجانب الكود.

## 2. تحقق من النسخ (اختياري)

```bash
sha256sum best_4_1.pt
# يجب أن يطابق: 712340651a57e15d41d2ec4621a3aa116d4188d36e6d8d2b9d33c74bd7286724
```

وبصمات بقية الملفات في `CHECKSUMS_SHA256.txt` في أعلى مجلد Hima_1.

## 3. التثبيت والفحص

```bash
bash launcher/start.sh
```

يطلب كلمة سر sudo مرة لحزم apt. يفحص الجهاز، وينشئ `.venv` ويثبّت المكتبات ونسخة torch الخاصة بالجيتسون، ويتحقق من الأوزان، ويثبّت Ollama ونماذجه، ويجرّب الكاميرا والبيكس هوك والمحطة، ثم يشغّل الفحص قبل الإقلاع. كل سطر يطبع `OK` أو `WARN` أو `FAIL` مع السبب.

## 4. اربطه بالمحطة

1. على حاسوب المحطة شغّل `START_STATION.bat` واكتب العنوان الذي يطبعه.
2. افتح `system/hima.toml` على الجيتسون وغيّر السطر:
   ```toml
   station_url = "http://STATION-IP:8765"
   ```
   وضع عنوان حاسوب المحطة مكان `STATION-IP`. ثبّت هذا العنوان في الراوتر (حجز DHCP) حتى لا يتغير.
3. احفظ شبكة واي فاي القاعدة مرة واحدة:
   ```bash
   sudo nmcli device wifi connect "NETWORK-NAME" password "PASSWORD"
   ```
4. تأكد أن المحطة ترد بالمفتاح نفسه:
   ```bash
   .venv/bin/python -m hima deliver --ping
   ```
   البصمة المطبوعة يجب أن تطابق بصمة المحطة.

## 5. البيكس هوك (TELEM2)

| المعامل في Mission Planner | القيمة |
|---|---|
| `SERIAL2_PROTOCOL` | 2 |
| `SERIAL2_BAUD` | 921 |
| `BRD_SER2_RTSCTS` | 0 |

التوصيل: TX في البيكس هوك إلى المسمار 10 في الجيتسون، وRX إلى المسمار 8، وGND إلى المسمار 6. **لا توصّل سلك 5V.** المنفذ غالبًا `/dev/ttyTHS1`، وتتأكد منه بالأمر `ls /dev/ttyTHS*`.

اختبار الاتصالات كلها قبل الطيران (لا يغيّر أي معامل ولا يسلّح):

```bash
bash launcher/test_links.sh
```

## 6. التشغيل التلقائي مع كل إقلاع

بعد نجاح الفحص المكتبي فقط:

```bash
bash launcher/start.sh --install-service
sudo systemctl enable --now hima-fly hima-deliver
```

- `hima-fly`: يبدأ برنامج الرحلة عند تشغيل الجيتسون، وينتظر التسليح.
- `hima-deliver`: بعد الهبوط يرسل حزمة المهمة إلى المحطة عبر واي فاي القاعدة، ويعيد المحاولة إذا لم تكن المحطة في المدى. الحزمة لا تُحذف من الدرون.

## أوامر مفيدة

```bash
bash launcher/start.sh --no-install   # فحص فقط بلا تثبيت
bash launcher/start.sh --fly          # فحص ثم رحلة (Ctrl+C يهبط بأمان)
bash launcher/start.sh --ai-off       # بلا نماذج الذكاء الاصطناعي
python3 launcher/hima_start.py --help # كل الخيارات
```

## ما في هذا المجلد

```
system/
  hima/                  كود النظام
  launcher/              المشغّل: تثبيت وفحص وتشغيل
  deploy/                خدمات systemd وسكربت الجيتسون
  hima.toml              كل الإعدادات
  best_4_1.pt            نموذج الحيوانين (المها العربي وغزال الريم)
  weights/general/       النموذج العام للأشياء الغريبة (yolo26s.pt)
  secrets/delivery.key   مفتاح التوصيل (نفسه على المحطة)
  requirements-jetson.txt مكتبات بايثون للجيتسون
data_ext/
  wdpa_555756385/        حدود المحمية (بدونها لا يقلع الدرون)
```
