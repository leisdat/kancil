#!/bin/bash
# Build Kancil Browser APK tanpa Gradle (aapt2 + d8 langsung).
set -e
cd "$(dirname "$0")"

# SDK lives in $HOME so it survives VM replacements (/opt is ephemeral).
# Override with ANDROID_SDK_ROOT env if needed.
SDK=${ANDROID_SDK_ROOT:-$HOME/android-sdk}
ANDROID_JAR=$SDK/platforms/android-34-ext12/android.jar
BT=$SDK/build-tools/34.0.0
# d8 (R8 8.2) crash di JDK 21 -> paksa JDK 17
export JAVA_HOME=$HOME/jdk17
export PATH=$JAVA_HOME/bin:$BT:$PATH

echo "== 1. compile resources =="
rm -rf compiled_res gen classes dex
mkdir -p compiled_res gen classes dex
$BT/aapt2 compile --dir res -o compiled_res/

echo "== 2. link =="
$BT/aapt2 link -o app-unsigned.apk \
  -I "$ANDROID_JAR" \
  --manifest AndroidManifest.xml \
  --java gen/ \
  compiled_res/*.flat

echo "== 3. javac =="
find java gen -name '*.java' > sources.txt
javac --release 8 -encoding UTF-8 -nowarn \
  -cp "$ANDROID_JAR" \
  -d classes/ @sources.txt > javac.log 2>&1
if [ $? -ne 0 ]; then echo "JAVAC FAILED:"; cat javac.log; exit 1; fi
grep -v "bootstrap\|deprecat" javac.log || true

echo "== 4. d8 =="
find classes -name '*.class' > classes.txt
$BT/d8 --lib "$ANDROID_JAR" --min-api 24 --output dex/ @classes.txt

echo "== 5. add classes.dex =="
python3 -c "
import zipfile
with zipfile.ZipFile('app-unsigned.apk', 'a', zipfile.ZIP_DEFLATED) as z:
    z.write('dex/classes.dex', 'classes.dex')
print('dex added')
"

echo "== 6. zipalign =="
$BT/zipalign -f 4 app-unsigned.apk app-aligned.apk

echo "== 7. sign =="
if [ ! -f debug.keystore ]; then
  keytool -genkeypair -keystore debug.keystore -alias kancil \
    -keyalg RSA -keysize 2048 -validity 10950 \
    -storepass kancil123 -keypass kancil123 \
    -dname "CN=Kancil Debug, OU=dev, O=kancil" 2>/dev/null
  echo "debug keystore created"
fi
$BT/apksigner sign --ks debug.keystore --ks-pass pass:kancil123 \
  --out kancil-browser.apk app-aligned.apk

echo "== done =="
ls -la kancil-browser.apk
$BT/apksigner verify --print-certs kancil-browser.apk | head -4
