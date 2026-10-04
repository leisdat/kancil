#!/bin/bash
# build-termux.sh — Build Kancil Browser APK di Termux (tanpa SDK penuh).
# Path tool: aapt2/zipalign/apksigner di PATH Termux; android.jar di /usr/share/aapt;
# d8 = r8.jar via java (D8 9.5.20-dev).
set -e
cd "$(dirname "$0")"

ANDROID_JAR=$HOME/android-sdk/platforms/android.jar
R8_JAR=$HOME/android-sdk/build-tools/r8.jar
KEYSTORE=debug.keystore

echo "== 1. compile resources =="
rm -rf compiled_res gen classes dex
mkdir -p compiled_res gen classes dex
aapt2 compile --dir res -o compiled_res/

echo "== 2. link =="
aapt2 link -o app-unsigned.apk \
  -I "$ANDROID_JAR" \
  --manifest AndroidManifest.xml \
  --java gen/ \
  compiled_res/*.flat

echo "== 3. javac =="
find java gen -name '*.java' > sources.txt
javac --release 8 -encoding UTF-8 -nowarn \
  -cp "$ANDROID_JAR" \
  -d classes/ @sources.txt 2> javac.log || { echo "JAVAC FAILED:"; cat javac.log; exit 1; }
grep -v "bootstrap\|deprecat" javac.log || true

echo "== 4. d8 (via r8.jar) =="
find classes -name '*.class' > classes.txt
java -cp "$R8_JAR" com.android.tools.r8.D8 --lib "$ANDROID_JAR" --min-api 24 --output dex/ @classes.txt

echo "== 5. add classes.dex =="
python3 -c "
import zipfile
with zipfile.ZipFile('app-unsigned.apk', 'a', zipfile.ZIP_DEFLATED) as z:
    z.write('dex/classes.dex', 'classes.dex')
print('dex added')
"

echo "== 6. zipalign =="
zipalign -f 4 app-unsigned.apk app-aligned.apk

echo "== 7. sign =="
if [ ! -f "$KEYSTORE" ]; then
  keytool -genkeypair -keystore "$KEYSTORE" -alias kancil \
    -keyalg RSA -keysize 2048 -validity 10950 \
    -storepass kancil123 -keypass kancil123 \
    -dname "CN=Kancil Debug, OU=dev, O=kancil" 2>/dev/null
  echo "debug keystore created"
fi
apksigner sign --ks "$KEYSTORE" --ks-pass pass:kancil123 \
  --out kancil-browser.apk app-aligned.apk

echo "== done =="
ls -la kancil-browser.apk
apksigner verify --print-certs kancil-browser.apk | head -4