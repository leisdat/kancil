# Kancil Browser (Android companion app)

Browser Android berbasis System WebView (Chromium beneran) dengan
**agent HTTP server** di `127.0.0.1:8080` — Kancil di Termux nyambung
lewat engine `webview` dan makai session yang user bangun manual
(login/captcha sekali di app, agent pakai terus).

## Fitur
- WebView penuh: JS, video, login, captcha (engine = System WebView HP)
- Agent API: `/status /navigate /back /forward /reload /dom /text /js`
  `/click /type /network /network/clear /cookies /screenshot`
  `/tabs /tabs/new /tabs/activate /tabs/close` (multi-tab)
  `/reader` (reader mode via agent)
- Network log ala DevTools (method/url/headers per request)
- Pill status agent + toast aktivitas di UI
- Multi-tab (tombol counter + dialog switcher, tiap tab punya network log sendiri)
- Pilihan search engine: Google / DuckDuckGo / Brave / Bing (pengaturan ⚙)
- Adblock ringan (pola URL iklan/tracker, tercatat di network log)
- Reader mode (artikel jadi teks bersih, tombol di pengaturan / `POST /reader`)
- Download manager beneran (via Android DownloadManager)
- Hemat data (blokir gambar), situs desktop (toggle UA), mode malam (CSS filter),
  cari di halaman
  — kalau SafeSearch Google dikunci jaringan, pindah ke DuckDuckGo/Brave

## Build (tanpa Gradle, tanpa Android Studio)
Butuh JDK 17 + Android SDK (platform-34, build-tools 34.0.0):

```bash
./build.sh   # -> kancil-browser.apk (~37 KB, signed debug)
```

APK kecil karena engine Chromium-nya numpang System WebView yang
sudah ada di HP — tidak di-bundle.

## Pakai dari Kancil (Termux, HP yang sama)
1. Install + buka aplikasi **Kancil Browser** di HP
2. `kancil open --engine webview https://example.com`
