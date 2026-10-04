package com.kancil.browser;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.Intent;
import android.content.SharedPreferences;
import android.app.PendingIntent;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.os.Message;
import android.view.PixelCopy;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.EditorInfo;
import android.webkit.ConsoleMessage;
import android.webkit.CookieManager;
import android.webkit.JsPromptResult;
import android.webkit.JsResult;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.view.MotionEvent;
import android.os.SystemClock;
import android.view.PixelCopy;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.EditorInfo;
import android.webkit.CookieManager;
import android.webkit.URLUtil;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageButton;
import android.widget.LinearLayout;
import android.widget.PopupMenu;
import android.widget.ProgressBar;
import android.widget.RadioButton;
import android.widget.RadioGroup;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

public class MainActivity extends Activity {

    /** One browser tab: its own WebView + network log. */
    private static class Tab {
        int id; // not final: restoreTabs reassigns the persisted ID
        final WebView web;
        final NetLog netlog = new NetLog();
        // JS console buffer, filled by WebChromeClient.onConsoleMessage
        // (UI thread). Synchronized: read from agent worker threads.
        final java.util.List<JSONObject> consoleBuf =
                java.util.Collections.synchronizedList(new java.util.ArrayList<>());
        String title = "";
        String defaultUA = "";
        /** Agent-set UA override (/ua/set); empty = use defaultUA. */
        String customUA = "";
        /** True if this tab was spawned by window.open / target=_blank. */
        boolean popup = false;
        Tab(int id, WebView web) { this.id = id; this.web = web; }

        void consoleAdd(String level, String text, String source, int line) {
            try {
                JSONObject o = new JSONObject();
                o.put("t", System.currentTimeMillis());
                o.put("level", level);
                o.put("text", text);
                if (source != null) o.put("source", source);
                o.put("line", line);
                consoleBuf.add(o);
                while (consoleBuf.size() > 200) consoleBuf.remove(0);
            } catch (Exception ignored) {}
        }
    }

    private FrameLayout webContainer;
    private EditText urlBar;
    private Button tabCountBtn;
    private ProgressBar progressBar;
    private TextView agentStatus;
    private TextView agentToast;    private final Handler ui = new Handler(Looper.getMainLooper());
    /** Fullscreen video (YouTube dsb): view yang lagi fullscreen + callback-nya. */
    private View customView;
    private WebChromeClient.CustomViewCallback customViewCallback;
    private final Handler toastHide = new Handler(Looper.getMainLooper());
    private final List<Tab> tabs = new ArrayList<>();
    private Tab active;
    private int tabSeq = 0;
    private AgentServer server;
    private boolean agentUp = false;
    private SharedPreferences prefs;
    /** True when the last screenshot() fell back to drawWebView()
     *  (backgrounded, no window surface): the capture is WebView-sized,
     *  so elementScreenshot() must not apply the window offset. */
    private volatile boolean lastShotWebViewOnly = false;

    // search engines: key -> {label, home, search url prefix}
    private static final String[][] ENGINES = {
            {"google", "Google", "https://www.google.com",
                    "https://www.google.com/search?q="},
            {"duckduckgo", "DuckDuckGo", "https://duckduckgo.com",
                    "https://duckduckgo.com/?q="},
            {"brave", "Brave", "https://search.brave.com",
                    "https://search.brave.com/search?q="},
            {"bing", "Bing", "https://www.bing.com",
                    "https://www.bing.com/search?q="},
    };

    // ad/tracker host snippets — matched with String.contains (fast, no regex)
    private static final String[] ADBLOCK = {
            "doubleclick.net", "googlesyndication.com", "googleadservices.com",
            "google-analytics.com", "googletagmanager.com",
            "connect.facebook.net", "facebook.net/tr",
            "amazon-adsystem.com", "ads.yahoo.com",
            "taboola.com", "outbrain.com", "criteo.com",
            "adnxs.com", "rubiconproject.com", "adsrvr.org",
            "moatads.com", "hotjar.com", "mixpanel.com",
            "scorecardresearch.com", "quantserve.com",
    };

    /** Crash recovery for agent-driven use: an uncaught exception (e.g. a
     *  WebView FC while the agent is driving) is recorded to prefs for
     *  GET /crashes, then the app auto-restarts via AlarmManager — unless
     *  we've crashed 3+ times in 5 minutes (loop guard). Without this, one
     *  FC leaves the agent talking to a dead server until Farul reopens
     *  the app by hand. */
    private void installCrashRecovery() {
        Thread.setDefaultUncaughtExceptionHandler((thread, err) -> {
            try {
                java.io.StringWriter sw = new java.io.StringWriter();
                err.printStackTrace(new java.io.PrintWriter(sw));
                long now = System.currentTimeMillis();
                long last = prefs.getLong("crash_last", 0);
                int count = prefs.getInt("crash_count", 0);
                if (now - last > 5 * 60 * 1000) count = 0;
                // commit() is synchronous — apply() might not finish
                // before the killProcess() below.
                prefs.edit().putString("crash_report", sw.toString())
                        .putLong("crash_last", now)
                        .putInt("crash_count", count + 1).commit();
                if (count < 3) {
                    Intent i = new Intent(getApplicationContext(),
                            MainActivity.class);
                    i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                            | Intent.FLAG_ACTIVITY_CLEAR_TASK);
                    PendingIntent pi = PendingIntent.getActivity(
                            getApplicationContext(), 0, i,
                            PendingIntent.FLAG_ONE_SHOT
                            | PendingIntent.FLAG_IMMUTABLE);
                    android.app.AlarmManager am =
                            (android.app.AlarmManager)
                            getSystemService(ALARM_SERVICE);
                    if (am != null)
                        am.set(android.app.AlarmManager.RTC_WAKEUP,
                                now + 2000, pi);
                }
            } catch (Exception ignored) {}
            android.os.Process.killProcess(android.os.Process.myPid());
        });
    }

    @Override
    protected void onCreate(Bundle b) {
        // Theme must be set before super.onCreate (base context is attached).
        SharedPreferences p0 = getSharedPreferences("kancil", MODE_PRIVATE);
        setTheme(p0.getBoolean("dark", false)
                ? R.style.Theme_Kancil_Dark : R.style.Theme_Kancil);
        super.onCreate(b);
        prefs = p0;
        installCrashRecovery();
        setContentView(R.layout.activity_main);
        webContainer = findViewById(R.id.web_container);
        urlBar = findViewById(R.id.url_bar);
        tabCountBtn = findViewById(R.id.btn_tabs);
        progressBar = findViewById(R.id.progress);
        agentStatus = findViewById(R.id.agent_status);
        agentToast = findViewById(R.id.agent_toast);
        applyUiTheme();

        // Safe-area: keep toolbar below status bar / notch.
        final View toolbar = findViewById(R.id.toolbar);
        final int pt = toolbar.getPaddingTop(), pb = toolbar.getPaddingBottom(),
                  pl = toolbar.getPaddingLeft(), pr = toolbar.getPaddingRight();
        toolbar.setOnApplyWindowInsetsListener((v, in) -> {
            v.setPadding(pl, pt + in.getSystemWindowInsetTop(), pr, pb);
            return in;
        });

        findViewById(R.id.btn_back).setOnClickListener(v -> goBack());
        findViewById(R.id.btn_fwd).setOnClickListener(v -> goForward());
        applyKeepAlive(); // foreground service anti-freeze (bisa dimatikan)
        findViewById(R.id.btn_menu).setOnClickListener(v -> showMenu(v));
        findViewById(R.id.btn_retry).setOnClickListener(v -> {
            findViewById(R.id.error_view).setVisibility(View.GONE);
            activeWeb().reload();
        });
        findViewById(R.id.btn_error_back).setOnClickListener(v -> {
            findViewById(R.id.error_view).setVisibility(View.GONE);
            WebView w = activeWeb();
            if (w.canGoBack()) w.goBack();
            else w.loadUrl(homeUrl());
        });
        tabCountBtn.setOnClickListener(v -> showTabSwitcher());
        urlBar.setOnEditorActionListener((v, actionId, ev) -> {
            if (actionId == EditorInfo.IME_ACTION_GO) {
                navigate(urlBar.getText().toString());
                return true;
            }
            return false;
        });

        startAgentServer();
        setAgentStatus(serverUp());
        if (!restoreTabs()) {
            newTab(homeUrl(), false);
        }
    }

    // ---------- tab persistence (survive process death) ----------
    // Android kills background apps aggressively on low-RAM phones; without
    // this, every kill wipes all tabs and the agent sees a fresh Google tab.

    private void saveTabs() {
        try {
            JSONArray a = new JSONArray();
            for (Tab t : tabs) {
                String u = t.web.getUrl();
                if (u == null || u.isEmpty()) continue;
                JSONObject o = new JSONObject();
                o.put("id", t.id);
                o.put("url", u);
                a.put(o);
            }
            prefs.edit()
                    .putString("tabs", a.toString())
                    .putInt("active_tab", active != null ? active.id : -1)
                    .putInt("tab_seq", tabSeq)
                    .apply();
        } catch (Exception ignored) {}
    }

    /** @return true if tabs were restored */
    private boolean restoreTabs() {
        try {
            String raw = prefs.getString("tabs", null);
            if (raw == null) return false;
            JSONArray a = new JSONArray(raw);
            if (a.length() == 0) return false;
            tabSeq = prefs.getInt("tab_seq", 0);
            int activeId = prefs.getInt("active_tab", -1);
            Tab toActivate = null;
            for (int i = 0; i < a.length(); i++) {
                JSONObject o = a.getJSONObject(i);
                String url = o.optString("url", "");
                if (url.isEmpty()) continue;
                Tab t = newTabUi(url, true);
                if (t == null) continue;
                // Preserve the original tab ID: without this, every OS
                // recreate reassigns IDs and the agent's saved IDs go stale.
                int savedId = o.optInt("id", -1);
                if (savedId > 0) {
                    t.id = savedId;
                    if (savedId > tabSeq) tabSeq = savedId;
                }
                if (savedId == activeId) toActivate = t;
            }
            if (toActivate != null) activateTabUi(toActivate.id);
            else if (!tabs.isEmpty()) activateTabUi(tabs.get(0).id);
            return !tabs.isEmpty();
        } catch (Exception ignored) {
            return false;
        }
    }

    // ---------- tabs ----------

    private WebView activeWeb() {
        return active.web;
    }

    private Tab findTab(int id) {
        for (Tab t : tabs) if (t.id == id) return t;
        return null;
    }

    private void setupWebView(WebView w) {
        WebSettings s = w.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        // While the agent blocklist is non-empty, bypass the HTTP cache:
        // cached/revalidated resources can skip shouldInterceptRequest,
        // which would let blocks leak.
        synchronized (agentBlock) {
            s.setCacheMode(agentBlock.isEmpty() ? WebSettings.LOAD_DEFAULT
                    : WebSettings.LOAD_NO_CACHE);
        }
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setBuiltInZoomControls(true);
        s.setDisplayZoomControls(false);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        s.setSupportMultipleWindows(true); // popups -> onCreateWindow -> tab
        s.setJavaScriptCanOpenWindowsAutomatically(true);
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(w, true);
        w.setDownloadListener((url, userAgent, contentDisposition,
                               mimeType, contentLength) -> {
            try {
                long id = enqueueDownload(url);
                String name = URLUtil.guessFileName(url, contentDisposition,
                        mimeType);
                Toast.makeText(MainActivity.this,
                        "Mengunduh: " + name, Toast.LENGTH_SHORT).show();
                agentNote("download #" + id + " " + name);
            } catch (Exception e) {
                Toast.makeText(MainActivity.this,
                        "Download gagal: " + e.getMessage(),
                        Toast.LENGTH_SHORT).show();
            }
        });
    }

    // ---------- toggles (global, persisted) ----------

    private boolean adblock() { return prefs.getBoolean("adblock", true); }
    private boolean dataSaver() { return prefs.getBoolean("datasaver", false); }

    // Foreground keep-alive: fights MIUI/EMUI task killers that freeze the
    // app (and its agent server) when the screen goes off.
    private boolean keepAlive() { return prefs.getBoolean("keepalive", true); }

    private void applyKeepAlive() {
        Intent i = new Intent(this, AgentKeepAliveService.class);
        try {
            if (keepAlive()) {
                if (android.os.Build.VERSION.SDK_INT >= 26)
                    startForegroundService(i);
                else
                    startService(i);
            } else {
                stopService(i);
            }
        } catch (Exception e) {
            agentNote("keepalive: " + e.getMessage());
        }
    }
    private boolean desktop() { return prefs.getBoolean("desktop", false); }
    private boolean dark() { return prefs.getBoolean("dark", false); }

    private static boolean isAd(String url) {
        String u = url.toLowerCase();
        for (String p : ADBLOCK) if (u.contains(p)) return true;
        return false;
    }

    /** Apply global toggles to one tab (call on UI thread). */
    private void applyToggles(Tab tab) {
        WebSettings s = tab.web.getSettings();
        s.setBlockNetworkImage(dataSaver());
        String ua = !tab.customUA.isEmpty() ? tab.customUA
                : (tab.defaultUA.isEmpty()
                        ? s.getUserAgentString() : tab.defaultUA);
        if (desktop()) {
            // turn the mobile UA into a desktop one, keeping the version
            ua = ua.replace("; Mobile", "").replace("Mobile ", "")
                   .replace("Android ", "");
        }
        if (stealth()) {
            // drop the WebView tell ("Version/4.0") -> looks like real Chrome
            ua = ua.replace("Version/4.0 ", "");
        }
        s.setUserAgentString(ua);
    }

    private boolean isUiThread() {
        return Looper.myLooper() == Looper.getMainLooper();
    }

    private Tab newTab(String url, boolean background) {
        // Called from UI thread (onCreate, menu) AND agent worker threads.
        // Never block the UI thread waiting for itself -> deadlock.
        if (isUiThread()) return newTabUi(url, background);
        final AtomicReference<Tab> ref = new AtomicReference<>();
        final CountDownLatch latch = new CountDownLatch(1);
        ui.post(() -> {
            ref.set(newTabUi(url, background));
            latch.countDown();
        });
        try { latch.await(15, TimeUnit.SECONDS); } catch (Exception ignored) {}
        return ref.get();
    }

    /** Must run on the UI thread. */
    private Tab newTabUi(String url, boolean background) {
        WebView w = new WebView(MainActivity.this);
        setupWebView(w);
        Tab tab = new Tab(++tabSeq, w);
        tab.defaultUA = w.getSettings().getUserAgentString();
        applyToggles(tab);
        w.setWebViewClient(makeClient(tab));
        w.setWebChromeClient(makeChrome(tab));
        tabs.add(tab);
        webContainer.addView(w, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT));
        if (!background) activateTabUi(tab.id);
        else w.setVisibility(View.GONE);
        if (url != null) w.loadUrl(url);
        updateTabCount();
        saveTabs();
        // Cap tabs at 12: recycle the oldest non-active tab so a stale
        // restored session can't pile up dozens of tabs.
        if (tabs.size() > 12) {
            for (Tab t : new java.util.ArrayList<>(tabs)) {
                if (t.id != tab.id && t != active) {
                    closeTabUi(t.id);
                    break;
                }
            }
        }
        return tab;
    }

    private void activateTab(int id) {
        if (isUiThread()) activateTabUi(id);
        else ui.post(() -> activateTabUi(id));
    }

    /** Must run on the UI thread. */
    private void activateTabUi(int id) {
        Tab t = findTab(id);
        if (t == null) return;
        if (active != null && active != t)
            active.web.setVisibility(View.GONE);
        active = t;
        t.web.setVisibility(View.VISIBLE);
        t.web.bringToFront();
        urlBar.setText(t.web.getUrl());
        if (!t.title.isEmpty()) setTitle(t.title);
        updateTabCount();
        saveTabs();
    }

    /** @return error message or null on success */
    private String closeTab(int id) {
        if (isUiThread()) return closeTabUi(id);
        final AtomicReference<String> err = new AtomicReference<>();
        final CountDownLatch latch = new CountDownLatch(1);
        ui.post(() -> {
            err.set(closeTabUi(id));
            latch.countDown();
        });
        try { latch.await(15, TimeUnit.SECONDS); } catch (Exception ignored) {}
        return err.get();
    }

    /** Must run on the UI thread. @return error or null */
    private String closeTabUi(int id) {
        Tab t = findTab(id);
        if (t == null) return "no such tab " + id;
        if (tabs.size() <= 1) return "cannot close the last tab";
        tabs.remove(t);
        webContainer.removeView(t.web);
        t.web.destroy();
        if (active == t && !tabs.isEmpty())
            activateTabUi(tabs.get(tabs.size() - 1).id);
        updateTabCount();
        saveTabs();
        return null;
    }

    private String engineKey() {
        return prefs.getString("search_engine", "google");
    }

    private String[] engineRow(String key) {
        for (String[] e : ENGINES) if (e[0].equals(key)) return e;
        return ENGINES[0];
    }

    private String homeUrl() {
        return engineRow(engineKey())[2];
    }

    private String searchUrl(String q) {
        try {
            return engineRow(engineKey())[3]
                    + java.net.URLEncoder.encode(q, "UTF-8");
        } catch (Exception e) {
            return engineRow(engineKey())[3] + q.replace(" ", "+");
        }
    }

    private void updateTabCount() {
        tabCountBtn.setText("▣ " + tabs.size());
        tabCountBtn.setContentDescription(tabs.size() + " tab");
    }

    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density);
    }

    // Emerald & Gold palette. Light: warm ivory + deep emerald + gold
    // accents. Dark: noir green-tinted + champagne gold.
    private void applyUiTheme() {
        boolean dk = dark();
        urlBar.setBackgroundResource(
                dk ? R.drawable.url_bg_dark : R.drawable.url_bg);
        urlBar.setTextColor(dk ? 0xFFEDE8DC : 0xFF1F2A24);
        urlBar.setHintTextColor(dk ? 0xFF8A948E : 0xFF9A917E);
        int tint = dk ? 0xFFEDE8DC : 0xFF0E5C43;
        ((ImageButton) findViewById(R.id.btn_back)).setColorFilter(tint);
        ((ImageButton) findViewById(R.id.btn_fwd)).setColorFilter(tint);
        ((ImageButton) findViewById(R.id.btn_menu)).setColorFilter(tint);
        findViewById(R.id.toolbar).setBackgroundColor(
                dk ? 0xFF12140F : 0xFFFDFCF8);
        tabCountBtn.setTextColor(dk ? 0xFFD4B978 : 0xFF0E5C43);
        // Agent pill is deep emerald in both modes; status text stays light.
        agentStatus.setTextColor(0xFFEDE8DC);
        View pill = (View) findViewById(R.id.agent_status).getParent();
        pill.setBackgroundResource(
                dk ? R.drawable.pill_bg_dark : R.drawable.pill_bg);
        getWindow().setStatusBarColor(dk ? 0xFF0C0E0D : 0xFF0B3D2E);
    }

    private void setAgentStatus(final boolean up) {
        ui.post(() -> {
            agentStatus.setText(up ? "● Agent aktif" : "○ Agent terputus");
            agentStatus.setTextColor(up ? 0xFFEDE8DC : 0xFFF5C6A8);
            android.graphics.drawable.GradientDrawable dot =
                    new android.graphics.drawable.GradientDrawable();
            dot.setShape(android.graphics.drawable.GradientDrawable.OVAL);
            dot.setColor(up ? 0xFFD4B978 : 0xFFB3261E);
            findViewById(R.id.agent_dot).setBackground(dot);
            agentStatus.setContentDescription(up
                    ? "Agent aktif di port 8080" : "Agent terputus");
        });
    }

    /** Tint emerald (light) / champagne gold (dark) buat CheckBox & RadioButton. */
    private android.content.res.ColorStateList accentTint() {
        boolean dk = dark();
        int checked = dk ? 0xFFD4B978 : 0xFF0E5C43;
        int unchecked = dk ? 0xFF6B7280 : 0xFF9AA0A6;
        return new android.content.res.ColorStateList(
                new int[][]{
                        new int[]{android.R.attr.state_checked},
                        new int[]{}},
                new int[]{checked, unchecked});
    }

    private int themeText() { return dark() ? 0xFFEDE8DC : 0xFF1F2A24; }
    private int themeGold() { return dark() ? 0xFFD4B978 : 0xFF8A7440; }
    private int themeAccent() { return dark() ? 0xFFD4B978 : 0xFF0E5C43; }

    /** Label seksi kecil caps gold ala Emerald & Gold. */
    private TextView sectionHeader(String t) {
        TextView v = new TextView(this);
        v.setText(t);
        v.setTextSize(11);
        v.setTypeface(null, android.graphics.Typeface.BOLD);
        v.setTextColor(themeGold());
        v.setLetterSpacing(0.12f);
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
        p.topMargin = dp(14); p.bottomMargin = dp(4);
        v.setLayoutParams(p);
        return v;
    }

    private View thinDivider() {
        View v = new View(this);
        v.setBackgroundColor(dark() ? 0xFF2A2E2A : 0xFFE8E0D0);
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(1));
        p.topMargin = dp(10); p.bottomMargin = dp(2);
        v.setLayoutParams(p);
        return v;
    }

    private CheckBox themedCheckBox(String label, boolean checked) {
        CheckBox cb = new CheckBox(this);
        cb.setText(label);
        cb.setChecked(checked);
        cb.setButtonTintList(accentTint());
        cb.setTextColor(themeText());
        cb.setTextSize(14);
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
        p.topMargin = dp(4); p.bottomMargin = dp(4);
        cb.setLayoutParams(p);
        return cb;
    }

    private void showSettings() {
        String cur = engineKey();
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        root.setPadding(pad, pad / 2, pad, pad / 2);

        TextView title = new TextView(this);
        title.setText("Pengaturan");
        title.setTextSize(20);
        title.setTypeface(null, android.graphics.Typeface.BOLD);
        title.setTextColor(themeAccent());
        LinearLayout.LayoutParams tp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
        tp.bottomMargin = dp(4);
        title.setLayoutParams(tp);
        root.addView(title);

        root.addView(sectionHeader("MESIN PENCARI"));

        RadioGroup rg = new RadioGroup(this);
        int checkedId = 0;
        for (int i = 0; i < ENGINES.length; i++) {
            RadioButton rb = new RadioButton(this);
            rb.setText(ENGINES[i][1]);
            rb.setId(1000 + i);
            rb.setButtonTintList(accentTint());
            rb.setTextColor(themeText());
            rb.setTextSize(14);
            if (ENGINES[i][0].equals(cur)) checkedId = rb.getId();
            rg.addView(rb);
        }
        rg.check(checkedId);
        root.addView(rg);

        root.addView(thinDivider());
        root.addView(sectionHeader("FITUR"));

        final CheckBox cbAd = themedCheckBox("Adblock (blokir iklan & tracker)", adblock());
        final CheckBox cbData = themedCheckBox("Hemat data (tanpa gambar)", dataSaver());
        final CheckBox cbDesk = themedCheckBox("Situs desktop", desktop());
        final CheckBox cbDark = themedCheckBox("Mode malam", dark());
        final CheckBox cbKeep = themedCheckBox("Jaga agent tetap hidup (anti-freeze MIUI)", keepAlive());
        final CheckBox cbStealth = themedCheckBox("Mode stealth (sembunyikan jejak WebView)", stealth());
        root.addView(cbAd);
        root.addView(cbData);
        root.addView(cbDesk);
        root.addView(cbDark);
        root.addView(cbKeep);
        root.addView(cbStealth);

        ScrollView sv = new ScrollView(this);
        sv.addView(root);

        final int[] selEng = {0};
        for (int i = 0; i < ENGINES.length; i++)
            if (ENGINES[i][0].equals(cur)) selEng[0] = i;
        rg.setOnCheckedChangeListener((g, id) -> selEng[0] = id - 1000);

        AlertDialog dlg = new AlertDialog.Builder(this)
                .setView(sv)
                .setPositiveButton("OK", (d, which) -> {
                    boolean darkChanged = cbDark.isChecked() != dark();
                    boolean needReload = cbData.isChecked() != dataSaver()
                            || cbDesk.isChecked() != desktop();
                    prefs.edit()
                            .putString("search_engine", ENGINES[selEng[0]][0])
                            .putBoolean("adblock", cbAd.isChecked())
                            .putBoolean("datasaver", cbData.isChecked())
                            .putBoolean("desktop", cbDesk.isChecked())
                            .putBoolean("dark", cbDark.isChecked())
                            .putBoolean("keepalive", cbKeep.isChecked())
                            .putBoolean("stealth", cbStealth.isChecked())
                            .apply();
                    applyKeepAlive();
                    if (darkChanged) {
                        recreate(); // theme native diganti
                        return;
                    }
                    ui.post(() -> {
                        for (Tab t : new ArrayList<>(tabs)) applyToggles(t);
                        setDarkAll(dark());
                        if (needReload) activeWeb().reload();
                    });
                    Toast.makeText(this, "Pengaturan disimpan",
                            Toast.LENGTH_SHORT).show();
                })
                .setNegativeButton("Batal", null)
                .show();
        // Tombol dialog ikut tema (default-nya teal polos).
        try {
            dlg.getButton(AlertDialog.BUTTON_POSITIVE).setTextColor(themeAccent());
            dlg.getButton(AlertDialog.BUTTON_NEGATIVE).setTextColor(themeGold());
        } catch (Exception ignored) {}
        roundDialog(dlg);
    }

    // ---------- reader mode ----------

    private static final String READER_JS =
            "(function(){"
            + "var r=document.getElementById('kancil-reader');"
            + "if(r){var o=document.getElementById('kancil-orig');"
            + "if(o){document.body.innerHTML=o.innerHTML;o.remove();}else{r.remove();}"
            + "return 'off';}"
            + "var orig=document.createElement('div');orig.id='kancil-orig';"
            + "orig.style.display='none';orig.innerHTML=document.body.innerHTML;"
            + "document.body.appendChild(orig);"
            + "var best=null,bestScore=0;"
            + "var cands=document.querySelectorAll('article,main,[role=main]');"
            + "if(!cands.length)cands=document.querySelectorAll('div,section');"
            + "for(var el of cands){var ps=el.querySelectorAll('p'),len=0;"
            + "for(var p of ps)len+=p.innerText.length;"
            + "if(ps.length>=2&&len>bestScore){bestScore=len;best=el;}}"
            + "var tmp=document.createElement('div');"
            + "tmp.innerHTML=best?best.innerHTML:orig.innerHTML;"
            + "tmp.querySelectorAll('script,style,iframe,nav,header,footer,aside').forEach(function(e){e.remove();});"
            + "document.body.innerHTML='';"
            + "var wrap=document.createElement('div');wrap.id='kancil-reader';"
            + "wrap.setAttribute('style','max-width:700px;margin:0 auto;padding:16px;font-size:18px;line-height:1.7;font-family:sans-serif');"
            + "var h=document.createElement('h1');h.innerText=document.title;wrap.appendChild(h);"
            + "wrap.appendChild(tmp);document.body.appendChild(wrap);"
            + "return 'on';})()";

    private void toggleReader() {
        ui.post(() -> activeWeb().evaluateJavascript(READER_JS, v ->
                agentNote("reader " + v)));
    }

    // ---------- dark mode (CSS filter, works on all API levels) ----------
    // Invert + hue-rotate buat gelapin halaman; gambar/video dibalikin
    // normal biar nggak jadi negatif. Shadow DOM (thumbnail YouTube dsb)
    // nggak ketembus CSS dokumen utama, jadi fix-nya disuntik ke tiap
    // shadow root + MutationObserver (debounce) buat yang dibuat belakangan.

    private static final String DARK_ON =
            "(function(){"
            + "if(document.getElementById('kancil-dark'))return;"
            + "var s=document.createElement('style');s.id='kancil-dark';"
            + "s.textContent='html{filter:invert(1) hue-rotate(180deg);background:#111 !important}"
            + "img,video,picture,canvas{filter:invert(1) hue-rotate(180deg)}';"
            + "(document.head||document.documentElement).appendChild(s);"
            + "var fix='img,video,picture,canvas{filter:invert(1) hue-rotate(180deg) !important}';"
            + "function fixRoot(root,isShadow){"
            + "if(isShadow){try{if(!root.querySelector('[data-kdf]')){"
            + "var st=document.createElement('style');st.setAttribute('data-kdf','1');"
            + "st.textContent=fix;root.appendChild(st);}}catch(e){}}"
            + "var els=root.querySelectorAll?root.querySelectorAll('*'):[];"
            + "for(var i=0;i<els.length;i++){try{if(els[i].shadowRoot)fixRoot(els[i].shadowRoot,true);}catch(e){}}}"
            + "fixRoot(document,false);"
            + "try{"
            + "if(window.__kdfObs)window.__kdfObs.disconnect();"
            + "var pend=false;"
            + "window.__kdfObs=new MutationObserver(function(){"
            + "if(pend)return;pend=true;"
            + "setTimeout(function(){pend=false;try{fixRoot(document,false);}catch(e){}},800);"
            + "});"
            + "window.__kdfObs.observe(document.documentElement,{childList:true,subtree:true});"
            + "}catch(e){}"
            + "})()";
    private static final String DARK_OFF =
            "(function(){"
            + "var s=document.getElementById('kancil-dark');if(s)s.remove();"
            + "try{if(window.__kdfObs){window.__kdfObs.disconnect();window.__kdfObs=null;}}catch(e){}"
            + "function clean(root){"
            + "try{"
            + "var f=root.querySelectorAll?root.querySelectorAll('[data-kdf]'):[];"
            + "for(var i=0;i<f.length;i++)f[i].remove();"
            + "var els=root.querySelectorAll?root.querySelectorAll('*'):[];"
            + "for(var j=0;j<els.length;j++){try{if(els[j].shadowRoot)clean(els[j].shadowRoot);}catch(e){}}"
            + "}catch(e){}}"
            + "try{clean(document);}catch(e){}"
            + "})()";

    // Agent-controlled request blocklist (pattern = URL substring).
    // Written from agent worker threads, read from WebView threads.
    private final java.util.List<String> agentBlock =
            java.util.Collections.synchronizedList(new java.util.ArrayList<>());

    private boolean isAgentBlocked(String url) {
        String u = url.toLowerCase();
        synchronized (agentBlock) {
            for (String p : agentBlock) if (u.contains(p)) return true;
        }
        return false;
    }

    private void setDarkAll(boolean on) {
        ui.post(() -> {
            for (Tab t : new ArrayList<>(tabs))
                t.web.evaluateJavascript(on ? DARK_ON : DARK_OFF, null);
        });
    }

    // Stealth: hide the small JS tells that mark a WebView as automation.
    // Principle (ala Camoufox): patch only what's fake-or-missing, never
    // randomize genuine values — consistency beats randomness. Real phone =
    // real hardware/fonts/touch, so only 3 things need fixing.
    private static final String STEALTH_JS =
            "(function(){"
            + "try{Object.defineProperty(navigator,'webdriver',"
            + "{get:function(){return undefined;},configurable:true});}"
            + "catch(e){}"
            + "try{if(!window.chrome)window.chrome={};"
            + "if(!window.chrome.csi)window.chrome.csi=function(){};"
            + "if(!window.chrome.loadTimes)"
            + "window.chrome.loadTimes=function(){};"
            + "if(!window.chrome.runtime)window.chrome.runtime={};}"
            + "catch(e){}"
            + "})();";

    // Deep query: pierce shadow DOM + same-origin iframes (recursive).
    // Cross-origin iframes are unreachable by design (same-origin policy).
    // Fast path first (plain querySelector), deep scan only on miss —
    // keeps the common case cheap on huge pages (FB feed).
    // __kancilQ(sel) -> first Element or null
    // __kancilQA(sel) -> Array of Elements
    // __kancilQX(xpath) -> first Node or null
    // __kancilR(el) -> top-window rect {x,y,w,h}, crossing iframe bounds
    private static final String DEEP_QUERY_JS =
            "(function(){"
            + "function docs(){var ds=[document];"
            + "function scan(root){var els=root.querySelectorAll('*');"
            + "for(var i=0;i<els.length;i++){var el=els[i];"
            + "if(el.shadowRoot&&ds.indexOf(el.shadowRoot)<0)"
            + "{ds.push(el.shadowRoot);scan(el.shadowRoot);}"
            + "if(el.tagName==='IFRAME'){try{var d=el.contentDocument;"
            + "if(d&&ds.indexOf(d)<0){ds.push(d);scan(d);}}catch(e){}}}}"
            + "scan(document);return ds;}"
            + "window.__kancilQ=function(sel){"
            + "try{var el=document.querySelector(sel);if(el)return el;}"
            + "catch(e){return null;}"
            + "var ds=docs();"
            + "for(var i=0;i<ds.length;i++){try{"
            + "var m=ds[i].querySelector(sel);if(m)return m;}catch(e){}}"
            + "return null;};"
            + "window.__kancilQA=function(sel){var out=[];"
            + "try{var nl=document.querySelectorAll(sel);"
            + "for(var j=0;j<nl.length;j++)out.push(nl[j]);}"
            + "catch(e){return out;}"
            + "var ds=docs();"
            + "for(var i=0;i<ds.length;i++){if(ds[i]===document)continue;"
            + "try{var q=ds[i].querySelectorAll(sel);"
            + "for(var k=0;k<q.length;k++)out.push(q[k]);}catch(e){}}"
            + "return out;};"
            + "window.__kancilQX=function(xpath){var ds=docs();"
            + "for(var i=0;i<ds.length;i++){try{"
            + "var ev=ds[i].evaluate?ds[i]:document;"
            + "var r=ev.evaluate(xpath,ds[i],null,"
            + "XPathResult.FIRST_ORDERED_NODE_TYPE,null);"
            + "if(r.singleNodeValue)return r.singleNodeValue;}catch(e){}}"
            + "return null;};"
            + "window.__kancilQXA=function(xpath){var out=[];var ds=docs();"
            + "for(var i=0;i<ds.length;i++){try{"
            + "var ev=ds[i].evaluate?ds[i]:document;"
            + "var r=ev.evaluate(xpath,ds[i],null,"
            + "XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,null);"
            + "for(var j=0;j<r.snapshotLength;j++)"
            + "out.push(r.snapshotItem(j));}catch(e){}}"
            + "return out;};"
            + "window.__kancilR=function(el){"
            + "var b=el.getBoundingClientRect();var x=b.left,y=b.top;"
            + "try{var w=el.ownerDocument.defaultView;"
            + "while(w&&w!==w.top){var fe=w.frameElement;if(!fe)break;"
            + "var fr=fe.getBoundingClientRect();x+=fr.left;y+=fr.top;"
            + "w=fe.ownerDocument.defaultView;}}catch(e){}"
            + "return {x:x,y:y,w:b.width,h:b.height};};"
            + "})();";

    // Shorthands with graceful fallback if the helpers aren't injected yet.
    private static final String DQ =
            "(window.__kancilQ||function(s){return document.querySelector(s)})";
    private static final String DQA =
            "(window.__kancilQA||function(s){return document.querySelectorAll(s)})";
    private static final String DQR =
            "(window.__kancilR||function(el){var b=el.getBoundingClientRect();"
            + "return {x:b.left,y:b.top,w:b.width,h:b.height}})";

    private boolean stealth() { return prefs.getBoolean("stealth", true); }

    private static WebResourceResponse emptyResponse() {        try {
            return new WebResourceResponse("text/plain", "utf-8",
                    200, "OK", new HashMap<String, String>(),
                    new java.io.ByteArrayInputStream(new byte[0]));
        } catch (Exception ignored) {}
        return null;
    }

    // Download history (agent-visible). Guarded by its own lock:
    // written on UI thread (DownloadListener), read on agent threads.
    private final java.util.List<JSONObject> downloadList =
            java.util.Collections.synchronizedList(new java.util.ArrayList<>());

    private void trackDownload(String url, String name, long id) {
        try {
            JSONObject o = new JSONObject();
            o.put("t", System.currentTimeMillis());
            o.put("url", url);
            o.put("name", name);
            o.put("id", id);
            downloadList.add(o);
            while (downloadList.size() > 50) downloadList.remove(0);
        } catch (Exception ignored) {}
    }

    private long enqueueDownload(String url) {
        DownloadManager.Request req =
                new DownloadManager.Request(Uri.parse(url));
        String name = URLUtil.guessFileName(url, null, null);
        req.setTitle(name);
        req.setDescription("Kancil Browser");
        req.setNotificationVisibility(DownloadManager.Request
                .VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
        req.setDestinationInExternalPublicDir(
                Environment.DIRECTORY_DOWNLOADS, name);
        DownloadManager dm = (DownloadManager)
                getSystemService(DOWNLOAD_SERVICE);
        long id = dm.enqueue(req);
        trackDownload(url, name, id);
        return id;
    }

    private void showFindDialog() {
        final EditText input = new EditText(this);
        input.setHint("Cari di halaman…");
        input.setSingleLine(true);
        LinearLayout ll = new LinearLayout(this);
        ll.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        ll.setPadding(pad, pad / 2, pad, pad / 2);
        ll.addView(input);
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        Button prev = new Button(this);
        prev.setText("▲");
        prev.setOnClickListener(v ->
                activeWeb().findNext(false));
        Button next = new Button(this);
        next.setText("▼");
        next.setOnClickListener(v ->
                activeWeb().findNext(true));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1);
        row.addView(prev, lp);
        row.addView(next, lp);
        ll.addView(row);
        AlertDialog d = new AlertDialog.Builder(this)
                .setTitle("Cari")
                .setView(ll)
                .setPositiveButton("Tutup", null)
                .create();
        input.setOnEditorActionListener((v, actionId, ev) -> {
            if (actionId == EditorInfo.IME_ACTION_SEARCH
                    || actionId == EditorInfo.IME_ACTION_DONE) {
                activeWeb().findAllAsync(input.getText().toString());
                return true;
            }
            return false;
        });
        d.setOnDismissListener(dlg -> ui.post(() ->
                activeWeb().clearMatches()));
        d.show();
        roundDialog(d);
    }

    private void showMenu(View anchor) {
        PopupMenu pm = new PopupMenu(this, anchor);
        pm.getMenu().add("Reload");
        pm.getMenu().add("Tab baru");
        pm.getMenu().add("Reader mode");
        pm.getMenu().add("Cari di halaman");
        pm.getMenu().add("Download");
        pm.getMenu().add("Agent API");
        pm.getMenu().add("DevTools");
        pm.getMenu().add("Pengaturan");
        pm.setOnMenuItemClickListener(item -> {
            String t = String.valueOf(item.getTitle());
            switch (t) {
                case "Reload":
                    activeWeb().reload();
                    break;
                case "Tab baru":
                    newTab(homeUrl(), false);
                    break;
                case "Reader mode":
                    toggleReader();
                    break;
                case "Cari di halaman":
                    showFindDialog();
                    break;
                case "Download":
                    try {
                        startActivity(new android.content.Intent(
                                DownloadManager.ACTION_VIEW_DOWNLOADS));
                    } catch (Exception e) {
                        Toast.makeText(this, "Tidak ada app download",
                                Toast.LENGTH_SHORT).show();
                    }
                    break;
                case "Agent API":
                    showAgentDialog();
                    break;
                case "DevTools":
                    showDevTools();
                    break;
                case "Pengaturan":
                    showSettings();
                    break;
            }
            return true;
        });
        pm.show();
    }

    /** DevTools manual: console JS, eksekutor JavaScript, dan network log
     *  tab aktif. Tanpa perlu agent. */
    private void showDevTools() {
        final Tab tab = active;
        if (tab == null) return;

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(12);
        root.setPadding(pad, pad, pad, pad);

        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        Button bCon = new Button(this); bCon.setText("Console");
        Button bJs = new Button(this); bJs.setText("JavaScript");
        Button bNet = new Button(this); bNet.setText("Network");
        LinearLayout.LayoutParams wp = new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        bar.addView(bCon, wp); bar.addView(bJs, wp); bar.addView(bNet, wp);
        root.addView(bar);

        final ScrollView sv = new ScrollView(this);
        final TextView out = new TextView(this);
        out.setTypeface(android.graphics.Typeface.MONOSPACE);
        out.setTextSize(11);
        out.setTextIsSelectable(true);
        sv.addView(out);
        LinearLayout.LayoutParams svp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(320));
        svp.topMargin = dp(8);
        root.addView(sv, svp);

        // Bar eksekusi JS (cuma tampil di mode JavaScript)
        final LinearLayout jsBar = new LinearLayout(this);
        jsBar.setOrientation(LinearLayout.HORIZONTAL);
        jsBar.setVisibility(View.GONE);
        final EditText jsIn = new EditText(this);
        jsIn.setHint("document.title");
        jsIn.setSingleLine(true);
        Button jsRun = new Button(this);
        jsRun.setText("▶");
        LinearLayout.LayoutParams ip = new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        jsBar.addView(jsIn, ip);
        jsBar.addView(jsRun);
        LinearLayout.LayoutParams jbp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
        jbp.topMargin = dp(8);
        root.addView(jsBar, jbp);

        final String[] mode = {"con"};
        Runnable render = new Runnable() {
            @Override public void run() {
                StringBuilder sb = new StringBuilder();
                if (mode[0].equals("con")) {
                    jsBar.setVisibility(View.GONE);
                    java.util.List<JSONObject> buf;
                    synchronized (tab.consoleBuf) {
                        buf = new java.util.ArrayList<>(tab.consoleBuf);
                    }
                    if (buf.isEmpty()) sb.append("(console kosong)");
                    for (JSONObject o : buf) {
                        sb.append('[').append(o.optString("level", "?")).append("] ")
                          .append(o.optString("text", "")).append('\n');
                        String src = o.optString("source", "");
                        if (!src.isEmpty())
                            sb.append("    @ ").append(src).append(':')
                              .append(o.optInt("line", 0)).append('\n');
                    }
                } else if (mode[0].equals("js")) {
                    jsBar.setVisibility(View.VISIBLE);
                    sb.append("// ketik JS di bawah, tap ▶\n// contoh: document.title");
                    if (out.getTag() != null) {
                        sb.append("\n\n── hasil ──\n").append(out.getTag());
                        out.setTag(null);
                    }
                } else {
                    jsBar.setVisibility(View.GONE);
                    try {
                        JSONArray arr = tab.netlog.toJson();
                        if (arr.length() == 0) sb.append("(network kosong)");
                        for (int i = Math.max(0, arr.length() - 80); i < arr.length(); i++) {
                            JSONObject e = arr.getJSONObject(i);
                            sb.append(e.optString("method", "?")).append(' ')
                              .append(e.isNull("status") ? "-" : e.optInt("status"))
                              .append(' ').append(e.optString("url", "")).append('\n');
                            if (!e.isNull("error"))
                                sb.append("    ! ").append(e.optString("error")).append('\n');
                        }
                    } catch (Exception e) {
                        sb.append("gagal baca netlog: ").append(e.getMessage());
                    }
                }
                out.setText(sb.toString());
                sv.post(() -> sv.fullScroll(View.FOCUS_DOWN));
            }
        };

        bCon.setOnClickListener(v -> { mode[0] = "con"; render.run(); });
        bJs.setOnClickListener(v -> { mode[0] = "js"; render.run(); });
        bNet.setOnClickListener(v -> { mode[0] = "net"; render.run(); });
        jsRun.setOnClickListener(v -> {
            String code = jsIn.getText().toString();
            if (code.trim().isEmpty()) return;
            try {
                tab.web.evaluateJavascript(code, value -> {
                    out.setTag("› " + code + "\n‹ " + value);
                    render.run();
                });
            } catch (Exception e) {
                out.setTag("error: " + e.getMessage());
                render.run();
            }
        });

        render.run();
        roundDialog(new AlertDialog.Builder(this)
                .setTitle("DevTools — " + tab.web.getUrl())
                .setView(root)
                .setPositiveButton("Tutup", null)
                .show());
    }

    private void showAgentDialog() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(16);
        root.setPadding(pad, dp(8), pad, dp(8));

        TextView st = new TextView(this);
        boolean up = serverUp();
        st.setText(up ? "● Agent aktif — 127.0.0.1:8080"
                     : "○ Agent terputus");
        st.setTextSize(15);
        root.addView(st);

        TextView tab = new TextView(this);
        String u = null;
        try { u = activeWeb().getUrl(); } catch (Exception ignored) {}
        tab.setText("Tab #" + (active == null ? "-" : active.id) + ": "
                + (u == null ? "" : u));
        tab.setTextSize(13);
        root.addView(tab);

        TextView lbl = new TextView(this);
        lbl.setText("\nDari Termux:");
        root.addView(lbl);
        final String cmd = "kancil --engine webview open https://example.com";
        TextView cmdv = new TextView(this);
        cmdv.setText(cmd);
        cmdv.setTypeface(android.graphics.Typeface.MONOSPACE);
        cmdv.setTextIsSelectable(true);
        cmdv.setTextSize(13);
        root.addView(cmdv);
        Button copy = new Button(this);
        copy.setText("Salin perintah");
        copy.setOnClickListener(v -> {
            android.content.ClipboardManager cm =
                    (android.content.ClipboardManager)
                            getSystemService(CLIPBOARD_SERVICE);
            cm.setPrimaryClip(android.content.ClipData.newPlainText(
                    "kancil", cmd));
            Toast.makeText(this, "Disalin", Toast.LENGTH_SHORT).show();
        });
        root.addView(copy);

        TextView logLbl = new TextView(this);
        logLbl.setText("\nLog agent:");
        root.addView(logLbl);
        TextView log = new TextView(this);
        StringBuilder sb = new StringBuilder();
        for (int i = Math.max(0, agentLogBuf.size() - 10);
                i < agentLogBuf.size(); i++)
            sb.append(agentLogBuf.get(i)).append("\n");
        log.setText(sb.length() == 0 ? "(belum ada aktivitas)"
                                     : sb.toString().trim());
        log.setTypeface(android.graphics.Typeface.MONOSPACE);
        log.setTextSize(11);
        ScrollView sv = new ScrollView(this);
        sv.addView(log);
        sv.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(120)));
        root.addView(sv);

        roundDialog(new AlertDialog.Builder(this)
                .setTitle("Agent API")
                .setView(root)
                .setPositiveButton("Tutup", null)
                .show());
    }

    /** Rounded dialog shell for the luxury theme. */
    private void roundDialog(android.app.AlertDialog d) {
        if (d.getWindow() != null)
            d.getWindow().setBackgroundDrawableResource(
                    dark() ? R.drawable.dialog_bg_dark : R.drawable.dialog_bg);
    }

    private boolean serverUp() {
        return agentUp;
    }

    private void showTabSwitcher() {
        final List<Tab> copy = new ArrayList<>(tabs);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        final int pad = dp(12);
        final AlertDialog dlg = new AlertDialog.Builder(this)
                .setTitle("Tab (" + copy.size() + ")")
                .setNegativeButton("Tutup", null)
                .create();
        for (final Tab t : copy) {
            boolean isActive = (t == active);
            LinearLayout row = new LinearLayout(this);
            row.setOrientation(LinearLayout.HORIZONTAL);
            row.setGravity(android.view.Gravity.CENTER_VERTICAL);
            row.setPadding(pad, dp(10), pad, dp(10));
            // Card look: rounded, active tab gets a gold-ringed card.
            android.graphics.drawable.GradientDrawable card =
                    new android.graphics.drawable.GradientDrawable();
            card.setCornerRadius(dp(14));
            if (isActive) {
                card.setColor(dark() ? 0xFF1B241F : 0xFFEAF3EB);
                card.setStroke(dp(1), dark() ? 0xFF8A7440 : 0xFFC6A15B);
            } else {
                card.setColor(dark() ? 0xFF161916 : 0xFFF8F5ED);
            }
            row.setBackground(card);
            LinearLayout.LayoutParams rlp = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT);
            rlp.setMargins(0, 0, 0, dp(8));
            row.setLayoutParams(rlp);

            TextView tv = new TextView(this);
            String title = t.title.isEmpty() ? "Tab baru" : t.title;
            String url = t.web.getUrl();
            if (title.length() > 36) title = title.substring(0, 36) + "…";
            if (url != null && url.length() > 48)
                url = url.substring(0, 48) + "…";
            tv.setText((isActive ? "◆ " : "◇ ") + title + "\n"
                    + (url == null ? "" : url));
            tv.setTextSize(14);
            tv.setTextColor(dark() ? 0xFFEDE8DC : 0xFF1F2A24);
            tv.setMaxLines(2);
            tv.setEllipsize(android.text.TextUtils.TruncateAt.END);
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                    0, ViewGroup.LayoutParams.WRAP_CONTENT, 1);
            tv.setLayoutParams(lp);

            ImageButton x = new ImageButton(this);
            x.setImageResource(R.drawable.ic_close);
            x.setBackgroundColor(0x00000000);
            x.setColorFilter(dark() ? 0xFFE8EAED : 0xFF5F6368);
            x.setContentDescription("Tutup tab " + title);
            LinearLayout.LayoutParams xlp = new LinearLayout.LayoutParams(
                    dp(36), dp(36));
            x.setLayoutParams(xlp);

            row.addView(tv);
            row.addView(x);
            row.setOnClickListener(v -> {
                dlg.dismiss();
                activateTab(t.id);
            });
            x.setOnClickListener(v -> {
                dlg.dismiss();
                closeTabUi(t.id);
            });
            root.addView(row);
        }
        TextView add = new TextView(this);
        add.setText("＋ Tab baru");
        add.setTextSize(15);
        add.setPadding(pad, dp(12), pad, dp(12));
        add.setTextColor(dark() ? 0xFFD4B978 : 0xFF0E5C43);
        add.setOnClickListener(v -> {
            dlg.dismiss();
            newTab(homeUrl(), false);
        });
        root.addView(add);
        ScrollView sv = new ScrollView(this);
        sv.addView(root);
        dlg.setView(sv);
        dlg.show();
        roundDialog(dlg);
    }

    private void goBack() {
        WebView w = activeWeb();
        if (w.canGoBack()) w.goBack();
    }

    private void goForward() {
        WebView w = activeWeb();
        if (w.canGoForward()) w.goForward();
    }

    // ---------- per-tab web clients ----------

    private WebViewClient makeClient(final Tab tab) {
        return new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v,
                                                   WebResourceRequest r) {
                String url = String.valueOf(r.getUrl());
                // Skema non-http(s) (intent://, tel:, mailto:, ...) tidak
                // bisa dirender WebView — tanpa handling, user dapat halaman
                // net::ERR_UNKNOWN_URL_SCHEME yang jelek (kasus: iklan
                // intent://pyppo.com nyangkut di address bar).
                if (!url.startsWith("http://")
                        && !url.startsWith("https://")) {
                    handleExternalScheme(v, url);
                    return true; // dikonsumsi — jangan tampilkan error page
                }
                return false;
            }

            @Override
            public WebResourceResponse shouldInterceptRequest(WebView v,
                                                             WebResourceRequest r) {
                String url = String.valueOf(r.getUrl());
                if (isAgentBlocked(url)) {
                    NetLog.Entry e = tab.netlog.add(r.getMethod(), url, null);
                    tab.netlog.fail(e, "blocked:agent");
                    return emptyResponse();
                }
                if (adblock() && isAd(url)) {
                    NetLog.Entry e = tab.netlog.add(r.getMethod(), url, null);
                    tab.netlog.fail(e, "blocked:adblock");
                    return emptyResponse();
                }
                Map<String, String> h = new HashMap<>();
                try {
                    Map<String, String> rh = r.getRequestHeaders();
                    if (rh != null) h.putAll(rh);
                } catch (Exception ignored) {}
                tab.netlog.add(r.getMethod(), url, h);
                return super.shouldInterceptRequest(v, r);
            }

            @Override
            public void onPageStarted(WebView v, String url, Bitmap favicon) {
                // Tab popup yang navigasi ke URL iklan: tutup langsung,
                // jangan jadi sampah tab.
                if (tab.popup && adblock() && isAd(url)) {
                    tab.netlog.add("GET", url, null);
                    final int pid = tab.id;
                    ui.post(() -> closeTabUi(pid));
                    agentNote("popup iklan diblokir");
                    return;
                }
                // Stealth first: patch the JS tells before page scripts run
                // (best effort — the context may still be warming up).
                if (stealth()) v.evaluateJavascript(STEALTH_JS, null);
                // Deep-query helpers for the agent (also re-injected on finish).
                v.evaluateJavascript(DEEP_QUERY_JS, null);
                if (tab == active) ui.post(() -> {
                    urlBar.setText(url);
                    findViewById(R.id.error_view).setVisibility(View.GONE);
                    progressBar.setVisibility(View.VISIBLE);
                    progressBar.setProgress(10);
                    if (favicon != null) {
                        android.graphics.drawable.BitmapDrawable d =
                                new android.graphics.drawable.BitmapDrawable(
                                        getResources(), favicon);
                        urlBar.setCompoundDrawablesWithIntrinsicBounds(
                                d, null, null, null);
                    } else {
                        urlBar.setCompoundDrawablesWithIntrinsicBounds(
                                0, 0, 0, 0);
                    }
                });
            }

            @Override
            public void onPageFinished(WebView v, String url) {
                // Deep-query helpers for the agent (idempotent re-inject).
                v.evaluateJavascript(DEEP_QUERY_JS, null);
                if (tab == active) ui.post(() -> {
                    urlBar.setText(url);
                    if (!tab.title.isEmpty()) setTitle(tab.title);
                    progressBar.setVisibility(View.GONE);
                });
                if (dark()) {
                    v.evaluateJavascript(DARK_ON, null);
                }
                saveTabs();
                CookieManager.getInstance().flush();
            }

            @Override
            public void onReceivedError(WebView v, WebResourceRequest r,
                                        android.webkit.WebResourceError e) {
                if (r.isForMainFrame() && tab == active) {
                    showError(String.valueOf(r.getUrl()),
                            String.valueOf(e.getDescription()));
                }
            }
        };
    }

    /** Tangani skema non-http(s): blokir intent iklan diam-diam, ikuti
     *  fallback https di dalam intent, atau lempar ke aplikasi eksternal.
     *  Tidak pernah membiarkan WebView mencoba me-render-nya
     *  (ERR_UNKNOWN_URL_SCHEME). Dipanggil dari
     *  shouldOverrideUrlLoading; return-nya selalu "sudah ditangani". */
    private void handleExternalScheme(WebView v, String url) {
        try {
            if (url.startsWith("intent://")) {
                Intent intent = Intent.parseUri(url,
                        Intent.URI_INTENT_SCHEME);
                String data = String.valueOf(intent.getData());
                // Intent iklan/tracker: blokir diam-diam (toast popup
                // killer biasanya sudah muncul duluan).
                if (adblock() && (isAd(url) || isAd(data))) {
                    agentNote("intent iklan diblokir");
                    return;
                }
                // Fallback https di dalam intent — buka di tab ini.
                String fallback =
                        intent.getStringExtra("browser_fallback_url");
                if (fallback != null && (fallback.startsWith("http://")
                        || fallback.startsWith("https://"))) {
                    v.loadUrl(fallback);
                    return;
                }
                // Serahkan ke aplikasi eksternal kalau ada yang bisa.
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                if (intent.resolveActivity(getPackageManager()) != null) {
                    startActivity(intent);
                }
                // else: diam — tidak ada aplikasi penangan, jangan
                // tampilkan error page.
                return;
            }
            // tel:, mailto:, dsb — lempar ke Android kalau ada penangan.
            Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse(url));
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            if (intent.resolveActivity(getPackageManager()) != null) {
                startActivity(intent);
            }
        } catch (Exception e) {
            agentNote("skema eksternal gagal: " + e.getMessage());
        }
    }

    private void showError(final String url, final String desc) {
        ui.post(() -> {
            TextView d = findViewById(R.id.error_detail);
            d.setText((desc == null || desc.isEmpty() ? "" : desc + "\n")
                    + (url == null ? "" : url));
            findViewById(R.id.error_view).setVisibility(View.VISIBLE);
            progressBar.setVisibility(View.GONE);
        });
    }

    // Agent-driven file upload: /upload sets a path, the next
    // onShowFileChooser feeds it to the page automatically.
    private volatile String pendingUploadPath = null;
    private ValueCallback<Uri[]> manualFileCb = null;
    private static final int FILE_PICKER_REQ = 4101;

    private WebChromeClient makeChrome(final Tab tab) {
        return new WebChromeClient() {
            @Override
            public void onReceivedTitle(WebView v, String title) {
                tab.title = title == null ? "" : title;
                if (tab == active) setTitle(tab.title);
            }

            @Override
            public void onProgressChanged(WebView v, int progress) {
                if (tab == active) ui.post(() -> {
                    if (progress >= 100) {
                        progressBar.setVisibility(View.GONE);
                    } else {
                        progressBar.setVisibility(View.VISIBLE);
                        progressBar.setProgress(progress);
                    }
                });
            }

            // Fullscreen video (tombol fullscreen YouTube dsb): tanpa ini
            // diklik nggak ngefek. Tampilkan custom view fullscreen di atas
            // segalanya; back / onHideCustomView mengembalikannya.
            @Override
            public void onShowCustomView(View view, CustomViewCallback cb) {
                if (customView != null) { cb.onCustomViewHidden(); return; }
                customView = view;
                customViewCallback = cb;
                webContainer.addView(view, new FrameLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        ViewGroup.LayoutParams.MATCH_PARENT));
                view.bringToFront();
            }

            @Override
            public void onHideCustomView() { hideCustomView(); }

            // Real console capture: every console.* + page error lands
            // here, including messages fired before page load.
            @Override
            public boolean onConsoleMessage(ConsoleMessage m) {
                String lvl = "log";
                try {
                    switch (m.messageLevel()) {
                        case ERROR: lvl = "error"; break;
                        case WARNING: lvl = "warn"; break;
                        case DEBUG: lvl = "debug"; break;
                        case LOG: case TIP: default: lvl = "log"; break;
                    }
                } catch (Exception ignored) {}
                String src = null;
                int line = 0;
                try { src = m.sourceId(); line = m.lineNumber(); }
                catch (Exception ignored) {}
                tab.consoleAdd(lvl, m.message(), src, line);
                return true;
            }

            // JS dialogs: auto-accept + log for the agent (a hanging
            // dialog would freeze the page otherwise).
            @Override
            public boolean onJsAlert(WebView v, String url, String msg,
                                     JsResult r) {
                tab.consoleAdd("warn", "js alert: " + msg, url, 0);
                agentNote("js alert dismissed");
                r.confirm();
                return true;
            }

            @Override
            public boolean onJsConfirm(WebView v, String url, String msg,
                                       JsResult r) {
                tab.consoleAdd("warn", "js confirm (accepted): " + msg,
                        url, 0);
                r.confirm();
                return true;
            }

            @Override
            public boolean onJsPrompt(WebView v, String url, String msg,
                                      String def, JsPromptResult r) {
                tab.consoleAdd("warn", "js prompt (default): " + msg, url, 0);
                r.confirm(def != null ? def : "");
                return true;
            }

            // Popup (window.open / target=_blank).
            // Runs on the UI thread already: create the tab synchronously.
            // Anti-iklan: popup TANPA user gesture (auto-popup) langsung
            // diblokir — 99% iklan. Popup DENGAN gesture tetap dibuka tapi
            // ditandai; kalau URL tujuannya kena pola adblock, tab-nya
            // langsung ditutup di onPageStarted.
            @Override
            public boolean onCreateWindow(WebView view, boolean isDialog,
                                          boolean isUserGesture,
                                          Message resultMsg) {
                try {
                    if (!isUserGesture) {
                        agentNote("popup diblokir");
                        return false;
                    }
                    Tab t = newTabUi(null, true);
                    t.popup = true;
                    WebView.WebViewTransport transport =
                            (WebView.WebViewTransport) resultMsg.obj;
                    transport.setWebView(t.web);
                    resultMsg.sendToTarget();
                    agentNote("popup -> tab " + t.id);
                    return true;
                } catch (Exception e) {
                    agentNote("popup failed: " + e.getMessage());
                    return false;
                }
            }

            @Override
            public void onCloseWindow(WebView window) {
                ui.post(() -> {
                    for (Tab t : new java.util.ArrayList<>(tabs)) {
                        if (t.web == window) { closeTab(t.id); break; }
                    }
                });
            }

            // File upload: agent path (/upload) wins; otherwise the
            // manual system picker (fixes file inputs for humans too).
            @Override
            public boolean onShowFileChooser(WebView v,
                                             ValueCallback<Uri[]> cb,
                                             FileChooserParams p) {
                String pend = pendingUploadPath;
                pendingUploadPath = null;
                if (pend != null) {
                    java.io.File f = new java.io.File(pend);
                    if (f.exists()) {
                        tab.consoleAdd("log", "upload: " + pend, null, 0);
                        cb.onReceiveValue(
                                new Uri[]{Uri.fromFile(f)});
                        return true;
                    }
                }
                if (manualFileCb != null) manualFileCb.onReceiveValue(null);
                manualFileCb = cb;
                try {
                    Intent i = new Intent(Intent.ACTION_GET_CONTENT);
                    i.addCategory(Intent.CATEGORY_OPENABLE);
                    i.setType("*/*");
                    startActivityForResult(
                            Intent.createChooser(i, "Pilih file"),
                            FILE_PICKER_REQ);
                } catch (Exception e) {
                    manualFileCb = null;
                    cb.onReceiveValue(null);
                }
                return true;
            }
        };
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);
        if (req == FILE_PICKER_REQ && manualFileCb != null) {
            ValueCallback<Uri[]> cb = manualFileCb;
            manualFileCb = null;
            if (res == Activity.RESULT_OK && data != null
                    && data.getData() != null) {
                cb.onReceiveValue(new Uri[]{data.getData()});
            } else {
                cb.onReceiveValue(null);
            }
        }
    }

    private void navigate(String input) {
        String u = input.trim();
        if (!u.matches("^[a-zA-Z][a-zA-Z0-9+.-]*:.*")) {
            if (u.matches(
                    "^(localhost|\\d{1,3}(\\.\\d{1,3}){3})(:\\d+)?(/.*)?$")) {
                u = "http://" + u; // dev server / IP lokal
            } else if (u.contains(".") && !u.contains(" ")) {
                u = "https://" + u;
            } else {
                u = searchUrl(u);
            }
        }
        final String url = u;
        ui.post(() -> {
            urlBar.setText(url);
            activeWeb().loadUrl(url);
        });
    }

    // ---------- JS bridge (worker thread -> UI thread) ----------

    private String evalJs(String expr) throws Exception {
        // Never block the UI thread waiting for itself -> ANR. All current
        // callers run on agent worker threads; fail fast if that changes.
        if (isUiThread())
            throw new IllegalStateException("evalJs called on UI thread");
        final AtomicReference<String> out = new AtomicReference<>();
        final CountDownLatch latch = new CountDownLatch(1);
        ui.post(() -> activeWeb().evaluateJavascript(expr, v -> {
            out.set(v);
            latch.countDown();
        }));
        if (!latch.await(30, TimeUnit.SECONDS)) throw new Exception("js timeout");
        String v = out.get();
        if (v != null && v.length() >= 2 && v.startsWith("\"") && v.endsWith("\"")) {
            try {
                v = new JSONObject("{\"v\":" + v + "}").optString("v", v);
            } catch (Exception ignored) {}
        }
        return v;
    }

    private final ArrayList<String> agentLogBuf = new ArrayList<>();

    private void agentNote(final String msg) {
        agentLog(msg, 0);
    }

    /** Persistent agent activity log + colored toast.
     *  level: 0 info, 1 ok, 2 warn, 3 error. */
    private void agentLog(final String msg, final int level) {
        ui.post(() -> {
            java.text.SimpleDateFormat f = new java.text.SimpleDateFormat(
                    "HH:mm:ss", java.util.Locale.US);
            agentLogBuf.add(f.format(new java.util.Date()) + " " + msg);
            if (agentLogBuf.size() > 50) agentLogBuf.remove(0);
            agentToast.setText(msg);
            int bg = level == 3 ? 0xDDB3261E
                    : level == 2 ? 0xDDF9AB00
                    : level == 1 ? 0xDD0E5C43 : 0xCC202124;
            android.graphics.drawable.GradientDrawable tbg =
                    (android.graphics.drawable.GradientDrawable)
                    getResources().getDrawable(R.drawable.toast_bg).mutate();
            tbg.setColor(bg);
            agentToast.setBackground(tbg);
            agentToast.setVisibility(View.VISIBLE);
            toastHide.removeCallbacksAndMessages(null);
            toastHide.postDelayed(() -> agentToast.setVisibility(View.GONE),
                    4000);
        });
    }

    // ---------- agent server ----------

    // All WebView access MUST happen on the UI thread. AgentServer handlers
    // run on worker threads; reading t.web.getUrl() etc. directly returns
    // null/stale (the "empty tabs" live bug).

    private JSONObject tabJson(Tab t) throws Exception {
        return uiGet(() -> tabJsonUi(t));
    }

    /** Must run on the UI thread (no latch). */
    private JSONObject tabJsonUi(Tab t) {
        JSONObject o = new JSONObject();
        try {
            String url = t.web.getUrl();
            o.put("id", t.id);
            o.put("url", url == null ? "" : url);
            o.put("title", t.title);
            o.put("active", t == active);
        } catch (Exception ignored) {}
        return o;
    }

    private void startAgentServer() {        server = new AgentServer((method, path, query, body) -> {
            switch (path) {
                case "/status": {
                    JSONObject o = uiGet(() -> {
                        JSONObject oo = new JSONObject();
                        try {
                            String url = activeWeb().getUrl();
                            oo.put("ok", true);
                            oo.put("agent", "kancil-browser/1.20");
                            oo.put("url", url == null ? "" : url);
                            oo.put("title", active.title);
                            oo.put("tab", active.id);
                            oo.put("tab_count", tabs.size());
                            oo.put("network_count", active.netlog.size());
                        } catch (Exception ignored) {}
                        return oo;
                    });
                    return AgentServer.Response.json(o);
                }
                case "/crashes": {
                    if ("POST".equals(method) || "DELETE".equals(method)) {
                        prefs.edit().remove("crash_report")
                                .putInt("crash_count", 0).apply();
                        JSONObject o = new JSONObject();
                        o.put("ok", true);
                        return AgentServer.Response.json(o);
                    }
                    JSONObject o = new JSONObject();
                    o.put("ok", true);
                    o.put("crash_count", prefs.getInt("crash_count", 0));
                    o.put("crash_last", prefs.getLong("crash_last", 0));
                    String rep = prefs.getString("crash_report", null);
                    o.put("last_crash",
                            rep == null ? JSONObject.NULL : rep);
                    return AgentServer.Response.json(o);
                }
                case "/wait/idle": {
                    // Human-like settle: document complete + network quiet.
                    // Runs on the agent worker thread; evalJs hops to UI.
                    int timeoutMs = body.optInt("timeout", 15000);
                    int quietMs = body.optInt("quiet_ms", 800);
                    if (timeoutMs < 1000) timeoutMs = 1000;
                    if (quietMs < 200) quietMs = 200;
                    long start = System.currentTimeMillis();
                    boolean idle = false;
                    String rs = "?";
                    while (System.currentTimeMillis() - start < timeoutMs) {
                        try {
                            rs = evalJs("document.readyState");
                        } catch (Exception e) { rs = "?"; }
                        long lastNet = active.netlog.lastT0();
                        long quietFor = System.currentTimeMillis() - lastNet;
                        if ("complete".equals(rs)
                                && (lastNet == 0 || quietFor >= quietMs)) {
                            idle = true;
                            break;
                        }
                        try { Thread.sleep(250); }
                        catch (InterruptedException ie) { break; }
                    }
                    JSONObject o = new JSONObject();
                    o.put("ok", true);
                    o.put("idle", idle);
                    o.put("ready_state", rs);
                    o.put("waited_ms",
                            System.currentTimeMillis() - start);
                    return AgentServer.Response.json(o);
                }
                case "/tabs": {
                    JSONArray a = uiGet(() -> {
                        JSONArray aa = new JSONArray();
                        try {
                            for (Tab t : new ArrayList<>(tabs))
                                aa.put(tabJsonUi(t));
                        } catch (Exception ignored) {}
                        return aa;
                    });
                    int activeId = uiGet(() -> active.id);
                    JSONObject o = new JSONObject();
                    o.put("ok", true);
                    o.put("tabs", a);
                    o.put("active", activeId);
                    return AgentServer.Response.json(o);
                }
                case "/tabs/new": {
                    String url = body.optString("url", query.get("url"));
                    Tab t = newTab(url == null || url.isEmpty()
                            ? homeUrl() : url, false);
                    if (t == null)
                        return AgentServer.Response.err(500, "new tab failed");
                    agentNote("new tab #" + t.id);
                    JSONObject o = new JSONObject();
                    o.put("ok", true);
                    o.put("tab", tabJson(t));
                    return AgentServer.Response.json(o);
                }
                case "/tabs/activate": {
                    String sid = body.optString("id", query.get("id"));
                    int id;
                    try { id = Integer.parseInt(sid); }
                    catch (Exception e) {
                        return AgentServer.Response.err(400, "missing id");
                    }
                    // findTab touches the tabs list: must run on UI thread
                    boolean exists = uiGet(() -> findTab(id) != null);
                    if (!exists)
                        return AgentServer.Response.err(404, "no such tab " + id);
                    activateTab(id);
                    agentNote("tab #" + id);
                    return ok();
                }
                case "/tabs/close": {
                    String sid = body.optString("id", query.get("id"));
                    int id;
                    try { id = Integer.parseInt(sid); }
                    catch (Exception e) {
                        return AgentServer.Response.err(400, "missing id");
                    }
                    String err = closeTab(id);
                    if (err != null)
                        return AgentServer.Response.err(400, err);
                    agentNote("close tab #" + id);
                    return ok();
                }
                case "/navigate": {
                    String url = body.optString("url", query.get("url"));
                    if (url == null || url.isEmpty())
                        return AgentServer.Response.err(400, "missing url");
                    navigate(url);
                    agentNote("navigate " + url);
                    JSONObject o = new JSONObject();
                    o.put("ok", true); o.put("url", url);
                    return AgentServer.Response.json(o);
                }
                case "/back":
                    ui.post(this::goBack);
                    agentNote("back");
                    return ok();
                case "/forward":
                    ui.post(this::goForward);
                    agentNote("forward");
                    return ok();
                case "/reload":
                    ui.post(() -> activeWeb().reload());
                    agentNote("reload");
                    return ok();
                case "/dom": {
                    String html = evalJs("(function(){return document.documentElement.outerHTML})()");
                    JSONObject o = new JSONObject();
                    o.put("ok", true); o.put("html", html);
                    agentNote("dom (" + (html == null ? 0 : html.length()) + " chars)");
                    return AgentServer.Response.json(o);
                }
                case "/text": {
                    String t = evalJs("(function(){return document.body.innerText})()");
                    JSONObject o = new JSONObject();
                    o.put("ok", true); o.put("text", t);
                    return AgentServer.Response.json(o);
                }
                case "/reader": {
                    String r = evalJs(READER_JS);
                    JSONObject o = new JSONObject();
                    o.put("ok", true); o.put("result", r);
                    agentNote("reader " + r);
                    return AgentServer.Response.json(o);
                }
                case "/js": {
                    String expr = body.optString("expr", query.get("expr"));
                    if (expr == null) return AgentServer.Response.err(400, "missing expr");
                    String r = evalJs(expr);
                    JSONObject o = new JSONObject();
                    o.put("ok", true); o.put("result", r);
                    agentNote("js");
                    return AgentServer.Response.json(o);
                }
                case "/console": {
                    Tab ct = active;
                    String tabQ = query.get("tab");
                    if (tabQ != null) {
                        try {
                            Tab ft = findTab(Integer.parseInt(tabQ));
                            if (ft != null) ct = ft;
                        } catch (Exception ignored) {}
                    }
                    final Tab fct = ct;
                    JSONArray logs = new JSONArray();
                    // synchronizedList: iterate under its lock, no UI hop
                    // needed (onConsoleMessage writes from the UI thread).
                    synchronized (fct.consoleBuf) {
                        for (JSONObject e : fct.consoleBuf) logs.put(e);
                    }
                    JSONObject o = new JSONObject();
                    o.put("ok", true); o.put("logs", logs);
                    return AgentServer.Response.json(o);
                }
                case "/console/clear": {
                    ui.post(() -> active.consoleBuf.clear());
                    return ok();
                }
                case "/blocklist": {
                    if ("POST".equals(method)) {
                        JSONArray a = body.optJSONArray("patterns");
                        synchronized (agentBlock) {
                            agentBlock.clear();
                            if (a != null) for (int i = 0; i < a.length(); i++) {
                                String p = a.optString(i, "").toLowerCase().trim();
                                if (!p.isEmpty()) agentBlock.add(p);
                            }
                        }
                        JSONObject o = new JSONObject();
                        o.put("ok", true);
                        o.put("patterns", agentBlock.size());
                        // Cached resources bypass shouldInterceptRequest, so a
                        // new block would miss them: scrub the HTTP cache
                        // (app-wide) and force LOAD_NO_CACHE on every tab
                        // while any pattern is active — block means block.
                        final boolean noCache;
                        synchronized (agentBlock) {
                            noCache = !agentBlock.isEmpty();
                        }
                        ui.post(() -> {
                            try {
                                for (Tab t : new java.util.ArrayList<>(tabs)) {
                                    t.web.clearCache(true);
                                    t.web.getSettings().setCacheMode(noCache
                                            ? WebSettings.LOAD_NO_CACHE
                                            : WebSettings.LOAD_DEFAULT);
                                }
                            } catch (Exception ignored) {}
                        });
                        o.put("cache_cleared", true);
                        o.put("cache_mode",
                                noCache ? "LOAD_NO_CACHE" : "LOAD_DEFAULT");
                        agentNote("blocklist " + agentBlock.size() + " patterns");
                        return AgentServer.Response.json(o);
                    }
                    JSONObject o = new JSONObject();
                    o.put("ok", true);
                    synchronized (agentBlock) {
                        o.put("patterns", new JSONArray(agentBlock));
                    }
                    return AgentServer.Response.json(o);
                }
                case "/screenshot/full": {
                    byte[] png = fullScreenshot();
                    agentNote("screenshot full (" + png.length + " bytes)");
                    return new AgentServer.Response(200, "image/png", png);
                }
                case "/screenshot/element": {
                    String sel = body.optString("selector",
                            query.get("selector"));
                    if (sel == null || sel.isEmpty())
                        return AgentServer.Response.err(400,
                                "missing selector");
                    byte[] png = elementScreenshot(sel);
                    agentNote("screenshot element (" + png.length
                            + " bytes)");
                    return new AgentServer.Response(200, "image/png", png);
                }
                case "/videos": {
                    String r = evalJs("(function(){var v=[];"
                            + DQA + "('video')"
                            + ".forEach(function(el,i){"
                            + "var src=el.currentSrc||el.src||'';"
                            + "if(!src){var s=el.querySelector('source');"
                            + "if(s)src=s.src||'';}"
                            + "v.push({index:i,src:src,duration:el.duration||0,"
                            + "currentTime:el.currentTime||0,paused:!!el.paused,"
                            + "width:el.videoWidth||0,height:el.videoHeight||0});"
                            + "});return JSON.stringify(v)})()");
                    JSONArray arr = new JSONArray();
                    try {
                        if (r != null && !r.isEmpty() && !"null".equals(r))
                            arr = new JSONArray(r);
                    } catch (Exception ignored) {}
                    JSONObject o = new JSONObject();
                    o.put("ok", true); o.put("videos", arr);
                    return AgentServer.Response.json(o);
                }
                case "/form/fill": {
                    int fi = body.optInt("form", 0);
                    JSONObject fields = body.optJSONObject("fields");
                    boolean submit = body.optBoolean("submit", false);
                    JSONObject res = fillForm(fi,
                            fields != null ? fields : new JSONObject(),
                            submit);
                    JSONArray done = res.optJSONArray("filled");
                    agentNote("form fill +" + (done != null
                            ? done.length() : 0));
                    return AgentServer.Response.json(res);
                }
                case "/upload": {
                    String p = body.optString("path", query.get("path"));
                    String data = body.optString("data", "");
                    if (!data.isEmpty()) {
                        // base64 mode: bytes come from the agent (Termux
                        // private dirs are unreadable to the app), stage to
                        // our own cache dir.
                        String fn = body.optString("filename", "upload.bin")
                                .replaceAll("[^A-Za-z0-9._-]", "_");
                        if (fn.isEmpty()) fn = "upload.bin";
                        byte[] raw;
                        try {
                            raw = android.util.Base64.decode(
                                    data, android.util.Base64.DEFAULT);
                        } catch (Exception e) {
                            return AgentServer.Response.err(400, "bad base64");
                        }
                        if (raw.length > 6 * 1024 * 1024)
                            return AgentServer.Response.err(400,
                                    "file too large (max 6MB)");
                        try {
                            java.io.File dir =
                                    new java.io.File(getCacheDir(), "upload");
                            dir.mkdirs();
                            java.io.File f = new java.io.File(dir, fn);
                            java.io.FileOutputStream fos =
                                    new java.io.FileOutputStream(f);
                            fos.write(raw);
                            fos.close();
                            p = f.getAbsolutePath();
                        } catch (Exception e) {
                            return AgentServer.Response.err(500,
                                    "stage failed: " + e.getMessage());
                        }
                    }
                    if (p == null || p.isEmpty())
                        return AgentServer.Response.err(400, "missing path");
                    java.io.File f = new java.io.File(p);
                    if (!f.exists())
                        return AgentServer.Response.err(400,
                                "file not found: " + p);
                    pendingUploadPath = p;
                    JSONObject o = new JSONObject();
                    o.put("ok", true); o.put("path", p);
                    o.put("hint", "click a file input next; the chooser "
                            + "is fed automatically");
                    agentNote("upload staged: " + p);
                    return AgentServer.Response.json(o);
                }
                case "/download": {
                    String u = body.optString("url", query.get("url"));
                    if (u == null || u.isEmpty())
                        return AgentServer.Response.err(400, "missing url");
                    long id = enqueueDownload(u);
                    JSONObject o = new JSONObject();
                    o.put("ok", true); o.put("id", id);
                    agentNote("agent download #" + id);
                    return AgentServer.Response.json(o);
                }
                case "/downloads": {
                    JSONArray arr = new JSONArray();
                    DownloadManager dm = (DownloadManager)
                            getSystemService(DOWNLOAD_SERVICE);
                    synchronized (downloadList) {
                        for (JSONObject e : downloadList) {
                            JSONObject c;
                            try { c = new JSONObject(e.toString()); }
                            catch (Exception ignored) { c = e; }
                            long id = e.optLong("id", -1);
                            if (id >= 0 && dm != null) {
                                // Live progress from DownloadManager.
                                android.database.Cursor cur = null;
                                try {
                                    cur = dm.query(new DownloadManager.Query()
                                            .setFilterById(id));
                                    if (cur != null && cur.moveToFirst()) {
                                        int st = cur.getInt(cur.getColumnIndex(
                                            DownloadManager.COLUMN_STATUS));
                                        long done = cur.getLong(cur.getColumnIndex(
                                            DownloadManager.COLUMN_BYTES_DOWNLOADED_SO_FAR));
                                        long total = cur.getLong(cur.getColumnIndex(
                                            DownloadManager.COLUMN_TOTAL_SIZE_BYTES));
                                        String s;
                                        switch (st) {
                                            case DownloadManager.STATUS_SUCCESSFUL:
                                                s = "done"; break;
                                            case DownloadManager.STATUS_FAILED:
                                                s = "failed"; break;
                                            case DownloadManager.STATUS_RUNNING:
                                                s = "running"; break;
                                            case DownloadManager.STATUS_PAUSED:
                                                s = "paused"; break;
                                            default: s = "pending";
                                        }
                                        c.put("dl_status", s);
                                        c.put("bytes_done", done);
                                        c.put("bytes_total", total);
                                    }
                                } catch (Exception ignored) {
                                } finally {
                                    if (cur != null) cur.close();
                                }
                            }
                            arr.put(c);
                        }
                    }
                    JSONObject o = new JSONObject();
                    o.put("ok", true); o.put("downloads", arr);
                    return AgentServer.Response.json(o);
                }
                case "/click": {
                    String sel = body.optString("selector", query.get("selector"));
                    if (sel == null) return AgentServer.Response.err(400, "missing selector");
                    String r = evalJs("(function(){var el=" + DQ + "("
                            + JSONObject.quote(sel) + ");"
                            + "if(!el) return 'not-found';"
                            + "el.scrollIntoView({block:'center'});el.click();return 'clicked'})()");
                    JSONObject o = new JSONObject();
                    o.put("ok", true); o.put("result", r);
                    agentNote("click " + sel);
                    return AgentServer.Response.json(o);
                }
                case "/type": {
                    String sel = body.optString("selector", query.get("selector"));
                    String text = body.optString("text", query.get("text"));
                    if (sel == null || text == null)
                        return AgentServer.Response.err(400, "missing selector/text");
                    // Native setter (not execCommand): works with React/Vue
                    // controlled inputs. selectAll first to replace content.
                    String r = evalJs("(function(){var el=" + DQ + "("
                            + JSONObject.quote(sel) + ");if(!el) return 'not-found';"
                            + "el.focus();"
                            + "try{el.select();}catch(e){}"
                            + "var proto=el.tagName==='TEXTAREA'"
                            + "?HTMLTextAreaElement.prototype"
                            + ":HTMLInputElement.prototype;"
                            + "var d=Object.getOwnPropertyDescriptor(proto,'value');"
                            + "var t=" + JSONObject.quote(text) + ";"
                            + "if(d&&d.set)d.set.call(el,t);else el.value=t;"
                            + "el.dispatchEvent(new Event('input',{bubbles:true}));"
                            + "el.dispatchEvent(new Event('change',{bubbles:true}));"
                            + "return 'typed'})()");
                    JSONObject o = new JSONObject();
                    o.put("ok", true); o.put("result", r);
                    agentNote("type " + sel);
                    return AgentServer.Response.json(o);
                }
                case "/press": {
                    // Human-like key press: dispatches real KeyboardEvents
                    // (keydown/keypress/keyup) on the selector or the focused
                    // element. Note: synthetic Enter does NOT trigger native
                    // form submission — click the submit button instead.
                    String sel = body.optString("selector", query.get("selector"));
                    String key = body.optString("key", query.get("key"));
                    if (key == null) key = "Enter";
                    int code;
                    switch (key) {
                        case "Enter": code = 13; break;
                        case "Escape": code = 27; break;
                        case "Tab": code = 9; break;
                        case "Backspace": code = 8; break;
                        case " ": case "Space": code = 32; key = " "; break;
                        case "ArrowUp": code = 38; break;
                        case "ArrowDown": code = 40; break;
                        case "ArrowLeft": code = 37; break;
                        case "ArrowRight": code = 39; break;
                        default: code = 0;
                    }
                    final int kc = code;
                    String target = (sel != null && !sel.isEmpty())
                            ? DQ + "(" + JSONObject.quote(sel) + ")"
                            : "document.activeElement||document.body";
                    String r = evalJs("(function(){var el=" + target + ";"
                            + "if(!el)return 'not-found';"
                            + "var k=" + JSONObject.quote(key) + ";"
                            + "var init={key:k,code:k,keyCode:" + kc
                            + ",which:" + kc + ",bubbles:true,cancelable:true};"
                            + "el.dispatchEvent(new KeyboardEvent('keydown',init));"
                            + "if(" + kc + ">=32)"
                            + "el.dispatchEvent(new KeyboardEvent('keypress',init));"
                            + "el.dispatchEvent(new KeyboardEvent('keyup',init));"
                            + "return 'pressed:'+k})()");
                    JSONObject o = new JSONObject();
                    o.put("ok", true); o.put("result", r);
                    agentNote("press " + key);
                    return AgentServer.Response.json(o);
                }
                case "/longpress": {
                    // Mobile long-press ~= context menu. Dispatches
                    // touchstart, a hold, then contextmenu on the element.
                    String sel = body.optString("selector", query.get("selector"));
                    if (sel == null)
                        return AgentServer.Response.err(400, "missing selector");
                    String r = evalJs("(function(){var el=" + DQ + "("
                            + JSONObject.quote(sel) + ");"
                            + "if(!el)return 'not-found';"
                            + "var b=" + DQR + "(el);"
                            + "var t={touches:[{clientX:b.x+b.w/2,"
                            + "clientY:b.y+b.h/2}],bubbles:true,cancelable:true};"
                            + "el.dispatchEvent(new TouchEvent('touchstart',t));"
                            + "el.dispatchEvent(new MouseEvent('contextmenu',"
                            + "{bubbles:true,cancelable:true,"
                            + "clientX:b.x+b.w/2,"
                            + "clientY:b.y+b.h/2}));"
                            + "el.dispatchEvent(new TouchEvent('touchend',"
                            + "{bubbles:true,cancelable:true}));"
                            + "return 'longpressed'})()");
                    JSONObject o = new JSONObject();
                    o.put("ok", true); o.put("result", r);
                    agentNote("longpress " + sel);
                    return AgentServer.Response.json(o);
                }
                case "/network": {
                    JSONObject o = new JSONObject();
                    o.put("ok", true);
                    o.put("tab", active.id);
                    o.put("requests", active.netlog.toJson());
                    return AgentServer.Response.json(o);
                }
                case "/network/clear":
                    active.netlog.clear();
                    return ok();
                case "/cookies": {
                    String url = uiGet(() -> activeWeb().getUrl());
                    String raw = CookieManager.getInstance().getCookie(url == null ? "" : url);
                    JSONArray a = new JSONArray();
                    if (raw != null) {
                        for (String part : raw.split(";")) {
                            int ei = part.indexOf('=');
                            if (ei > 0) {
                                JSONObject c = new JSONObject();
                                c.put("name", part.substring(0, ei).trim());
                                c.put("value", part.substring(ei + 1).trim());
                                a.put(c);
                            }
                        }
                    }
                    JSONObject o = new JSONObject();
                    o.put("ok", true); o.put("cookies", a);
                    return AgentServer.Response.json(o);
                }
                case "/cookies/clear": {
                    String d = body.optString("domain", null);
                    if (d == null || d.isEmpty()) d = query.get("domain");
                    if (d == null) d = "";
                    final String fDomain = d;
                    uiGet(() -> {
                        CookieManager cm = CookieManager.getInstance();
                        if (!fDomain.isEmpty()) {
                            // expire each cookie for the domain (https + http)
                            for (String scheme : new String[]{"https://", "http://"}) {
                                String raw = cm.getCookie(scheme + fDomain);
                                if (raw != null) {
                                    for (String part : raw.split(";")) {
                                        int ei = part.indexOf('=');
                                        if (ei > 0) {
                                            String n = part.substring(0, ei).trim();
                                            cm.setCookie(scheme + fDomain, n
                                                + "=; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/");
                                        }
                                    }
                                }
                            }
                        } else {
                            final CountDownLatch cdl = new CountDownLatch(1);
                            cm.removeAllCookies(v -> cdl.countDown());
                            try { cdl.await(10, TimeUnit.SECONDS); }
                            catch (InterruptedException ie) {
                                Thread.currentThread().interrupt();
                            }
                        }
                        cm.flush();
                        return null;
                    });
                    JSONObject oc = new JSONObject();
                    oc.put("ok", true); oc.put("domain", fDomain);
                    return AgentServer.Response.json(oc);
                }
                case "/cache/clear": {
                    uiGet(() -> {
                        activeWeb().clearCache(true);
                        return null;
                    });
                    return ok();
                }
                case "/cookies/set": {
                    String cname = body.optString("name", "");
                    String cval = body.optString("value", "");
                    if (cname.isEmpty())
                        return AgentServer.Response.err(400, "missing name");
                    String cdom = body.optString("domain", query.get("domain"));
                    if (cdom == null || cdom.isEmpty()) {
                        String u = uiGet(() -> activeWeb().getUrl());
                        try { cdom = u == null ? "" : Uri.parse(u).getHost(); }
                        catch (Exception e) { cdom = ""; }
                    }
                    if (cdom == null || cdom.isEmpty())
                        return AgentServer.Response.err(400, "missing domain");
                    String cpath = body.optString("path", "/");
                    long maxAge = body.optLong("maxAge", 0);
                    final String fDom = cdom;
                    final String cookie = cname + "=" + cval + "; Path=" + cpath
                            + (maxAge > 0 ? "; Max-Age=" + maxAge : "");
                    uiGet(() -> {
                        CookieManager cm = CookieManager.getInstance();
                        cm.setCookie("https://" + fDom, cookie);
                        cm.setCookie("http://" + fDom, cookie);
                        cm.flush();
                        return null;
                    });
                    JSONObject ocs = new JSONObject();
                    ocs.put("ok", true); ocs.put("domain", fDom);
                    return AgentServer.Response.json(ocs);
                }
                case "/ua/set": {
                    String ua = body.optString("ua", query.get("ua"));
                    if (ua == null || ua.isEmpty())
                        return AgentServer.Response.err(400, "missing ua");
                    final String fUa = ua;
                    uiGet(() -> {
                        active.customUA = fUa;
                        applyToggles(active);
                        return null;
                    });
                    return ok();
                }
                case "/ua/reset": {
                    uiGet(() -> {
                        active.customUA = "";
                        applyToggles(active);
                        return null;
                    });
                    return ok();
                }
                case "/find": {
                    String text = body.optString("text", query.get("text"));
                    if (text == null || text.isEmpty())
                        return AgentServer.Response.err(400, "missing text");
                    final String fText = text;
                    final boolean next = body.optBoolean("next", false);
                    final AtomicInteger matches = new AtomicInteger(-1);
                    final CountDownLatch fdl = new CountDownLatch(1);
                    ui.post(() -> {
                        WebView w = activeWeb();
                        w.setFindListener(new WebView.FindListener() {
                            @Override
                            public void onFindResultReceived(int activeMatch,
                                    int numberOfMatches, boolean done) {
                                if (done) {
                                    matches.set(numberOfMatches);
                                    fdl.countDown();
                                }
                            }
                        });
                        w.findAllAsync(fText);
                    });
                    try { fdl.await(10, TimeUnit.SECONDS); }
                    catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                    }
                    if (next && matches.get() > 0)
                        ui.post(() -> activeWeb().findNext(true));
                    JSONObject of = new JSONObject();
                    of.put("ok", true);
                    of.put("matches", matches.get());
                    return AgentServer.Response.json(of);
                }
                case "/touch": {
                    // Synthesized touch (agent 1.20+): tap / swipe /
                    // longpress via MotionEvent -> dispatchTouchEvent.
                    // Coordinates are CSS px (like getBoundingClientRect);
                    // converted to view px with the WebView scale here.
                    // Worth it for things JS click() can't drive: canvas,
                    // maps, custom gesture handlers.
                    String tAction = body.optString("action", "");
                    if (tAction.isEmpty() && query.get("action") != null)
                        tAction = query.get("action");
                    if (tAction.isEmpty()) tAction = "tap";
                    final String fAction = tAction.toLowerCase();
                    if (!fAction.equals("tap") && !fAction.equals("swipe")
                            && !fAction.equals("longpress"))
                        return AgentServer.Response.err(400,
                                "action must be tap|swipe|longpress");
                    String[] tkeys = {"x", "y", "x2", "y2"};
                    final double[] tc = new double[4];
                    for (int ti = 0; ti < 4; ti++) {
                        double tv = -1;
                        if (body.has(tkeys[ti])) tv = body.optDouble(tkeys[ti], -1);
                        else if (query.get(tkeys[ti]) != null) {
                            try { tv = Double.parseDouble(query.get(tkeys[ti])); }
                            catch (Exception ignored) {}
                        }
                        tc[ti] = tv;
                    }
                    long tDur = body.optLong("duration_ms", 0);
                    if (tDur == 0 && query.get("duration_ms") != null) {
                        try { tDur = Long.parseLong(query.get("duration_ms")); }
                        catch (Exception ignored) {}
                    }
                    if (tc[0] < 0 || tc[1] < 0)
                        return AgentServer.Response.err(400,
                                "missing x/y (CSS px)");
                    if (fAction.equals("swipe") && (tc[2] < 0 || tc[3] < 0))
                        return AgentServer.Response.err(400,
                                "swipe needs x2/y2");
                    final long fDur = tDur;
                    try {
                        uiGet(() -> {
                            WebView w = activeWeb();
                            float scale = w.getScale();
                            float[] pts = {(float) (tc[0] * scale),
                                           (float) (tc[1] * scale),
                                           (float) (tc[2] * scale),
                                           (float) (tc[3] * scale)};
                            dispatchTouchSeq(w, fAction, pts, fDur);
                            return null;
                        });
                    } catch (Exception e) {
                        return AgentServer.Response.err(500,
                                "touch failed: " + e.getMessage());
                    }
                    JSONObject ot = new JSONObject();
                    ot.put("ok", true); ot.put("action", fAction);
                    ot.put("x", tc[0]); ot.put("y", tc[1]);
                    agentNote("touch " + fAction + " @"
                            + (int) tc[0] + "," + (int) tc[1]);
                    return AgentServer.Response.json(ot);
                }
                case "/screenshot": {
                    byte[] png = screenshot();
                    return new AgentServer.Response(200, "image/png", png);
                }
                default:
                    return AgentServer.Response.err(404, "unknown path: " + path);
            }
        });
        try {
            server.start();
            agentUp = true;
        } catch (Exception e) {
            agentUp = false;
            Toast.makeText(this, "Agent server failed: " + e.getMessage(),
                    Toast.LENGTH_LONG).show();
        }
    }

    /** Synthesized touch sequence on a WebView. Runs on the UI thread
     * (called inside uiGet); points are view/device pixels.
     * tap: DOWN -> UP; longpress: DOWN, hold, UP;
     * swipe: DOWN -> MOVE... -> UP spread over duration_ms. */
    private void dispatchTouchSeq(WebView w, String action, float[] pts,
                                  long durationMs) {
        long down = SystemClock.uptimeMillis();
        if ("swipe".equals(action)) {
            long dur = durationMs > 0 ? durationMs : 400;
            int steps = Math.max(2, (int) (dur / 16));
            dispatchTouch(w, MotionEvent.ACTION_DOWN, pts[0], pts[1],
                    down, down);
            for (int i = 1; i <= steps; i++) {
                float x = pts[0] + (pts[2] - pts[0]) * i / steps;
                float y = pts[1] + (pts[3] - pts[1]) * i / steps;
                sleepQuiet(16);
                dispatchTouch(w, MotionEvent.ACTION_MOVE, x, y, down,
                        SystemClock.uptimeMillis());
            }
            dispatchTouch(w, MotionEvent.ACTION_UP, pts[2], pts[3], down,
                    SystemClock.uptimeMillis());
        } else if ("longpress".equals(action)) {
            long hold = durationMs > 0 ? durationMs : 800;
            dispatchTouch(w, MotionEvent.ACTION_DOWN, pts[0], pts[1],
                    down, down);
            sleepQuiet(hold);
            dispatchTouch(w, MotionEvent.ACTION_UP, pts[0], pts[1], down,
                    SystemClock.uptimeMillis());
        } else { // tap
            dispatchTouch(w, MotionEvent.ACTION_DOWN, pts[0], pts[1],
                    down, down);
            sleepQuiet(80);
            dispatchTouch(w, MotionEvent.ACTION_UP, pts[0], pts[1], down,
                    SystemClock.uptimeMillis());
        }
    }

    private void dispatchTouch(WebView w, int action, float x, float y,
                               long downTime, long eventTime) {
        MotionEvent e = MotionEvent.obtain(downTime, eventTime, action,
                x, y, 0);
        try {
            w.dispatchTouchEvent(e);
        } finally {
            e.recycle();
        }
    }

    private void sleepQuiet(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ie) {
            Thread.currentThread().interrupt();
        }
    }

    private static AgentServer.Response ok() {
        JSONObject o = new JSONObject();
        try { o.put("ok", true); } catch (Exception ignored) {}
        return AgentServer.Response.json(o);
    }

    private interface UiGet<T> { T get(); }

    private <T> T uiGet(UiGet<T> f) throws Exception {
        if (isUiThread())
            throw new IllegalStateException("uiGet called on UI thread");
        final AtomicReference<T> out = new AtomicReference<>();
        final CountDownLatch latch = new CountDownLatch(1);
        ui.post(() -> { out.set(f.get()); latch.countDown(); });
        if (!latch.await(15, TimeUnit.SECONDS)) throw new Exception("ui timeout");
        return out.get();
    }

    private byte[] screenshot() throws Exception {
        final AtomicReference<Bitmap> ref = new AtomicReference<>();
        final AtomicReference<Throwable> errRef = new AtomicReference<>();
        final CountDownLatch latch = new CountDownLatch(1);
        ui.post(() -> {
            try {
                View root = getWindow().getDecorView();
                int w = root.getWidth(), h = root.getHeight();
                if (w <= 0 || h <= 0) { latch.countDown(); return; }
                final Bitmap bmp = Bitmap.createBitmap(w, h,
                        Bitmap.Config.ARGB_8888);
                if (Build.VERSION.SDK_INT >= 26) {
                    try {
                        lastShotWebViewOnly = false;
                        ref.set(bmp);
                        PixelCopy.request(getWindow(), bmp,
                                copyResult -> latch.countDown(),
                                new Handler(Looper.getMainLooper()));
                    } catch (IllegalArgumentException noSurface) {
                        // App backgrounded/frozen: window has no backing
                        // surface. Fall back to drawing the WebView
                        // directly — works without a surface.
                        lastShotWebViewOnly = true;
                        ref.set(drawWebView());
                        latch.countDown();
                    }
                } else {
                    lastShotWebViewOnly = false;
                    ref.set(bmp);
                    root.draw(new android.graphics.Canvas(bmp));
                    latch.countDown();
                }
            } catch (Throwable t) {
                errRef.set(t);
                latch.countDown();
            }
        });
        if (!latch.await(45, TimeUnit.SECONDS)) throw new Exception("screenshot timeout");
        if (errRef.get() != null)
            throw new Exception("screenshot failed: "
                    + errRef.get().getMessage());
        Bitmap bmp = ref.get();
        if (bmp == null) throw new Exception("screenshot failed");
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        bmp.compress(Bitmap.CompressFormat.PNG, 100, bos);
        return bos.toByteArray();
    }

    /** Draw the active WebView directly (no window surface needed).
     *  Must run on the UI thread. Fallback when PixelCopy can't run. */
    private Bitmap drawWebView() {
        WebView wv = activeWeb();
        int w = Math.max(1, wv.getWidth()), h = Math.max(1, wv.getHeight());
        Bitmap b = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
        wv.draw(new android.graphics.Canvas(b));
        return b;
    }

    /** Full-page screenshot: scroll tile-by-tile, stitch natively.
     *  Called on a worker thread; hops to the UI thread as needed.
     *  RGB_565 (2 bytes/px) + 4000px cap: OOM guard for low-end phones.
     *  Catches Throwable: an OOM must become a 500, never a dead thread. */
    private byte[] fullScreenshot() throws Exception {
        final int[] dims = uiGet(() -> {
            WebView w = activeWeb();
            return new int[]{w.getWidth(), w.getHeight(),
                    (int) (w.getContentHeight() * w.getScale()),
                    w.getScrollY()};
        });
        int vw = dims[0], vh = dims[1];
        int totalH = Math.min(dims[2], 4000);
        final int origY = dims[3];
        if (vw <= 0 || vh <= 0 || totalH <= vh) return screenshot();
        Bitmap full;
        try {
            full = Bitmap.createBitmap(vw, totalH, Bitmap.Config.RGB_565);
        } catch (Throwable t) {
            throw new Exception("full screenshot OOM at "
                    + vw + "x" + totalH + ": " + t.getMessage());
        }
        android.graphics.Canvas canvas = new android.graphics.Canvas(full);
        try {
            for (int y = 0; y < totalH; y += vh) {
                final int sy = y;
                final CountDownLatch latch = new CountDownLatch(1);
                final Bitmap[] tile = new Bitmap[1];
                ui.post(() -> {
                    final WebView w = activeWeb();
                    w.scrollTo(0, sy);
                    ui.postDelayed(() -> {
                        try {
                            Bitmap b = Bitmap.createBitmap(vw, vh,
                                    Bitmap.Config.RGB_565);
                            w.draw(new android.graphics.Canvas(b));
                            tile[0] = b;
                        } catch (Throwable ignored) {}
                        latch.countDown();
                    }, 300);
                });
                if (!latch.await(15, TimeUnit.SECONDS)) break;
                if (tile[0] != null) {
                    canvas.drawBitmap(tile[0], 0, sy, null);
                    tile[0].recycle();
                }
            }
        } finally {
            ui.post(() -> {
                try { activeWeb().scrollTo(0, origY); }
                catch (Exception ignored) {}
            });
        }
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        full.compress(Bitmap.CompressFormat.PNG, 90, bos);
        full.recycle();
        byte[] out = bos.toByteArray();
        if (out.length == 0) throw new Exception("full screenshot failed");
        return out;
    }

    /** Element screenshot: scroll the element to viewport center, capture
     *  the window, crop natively. Called on a worker thread.
     *  The rect is re-queried AFTER the scroll settles (layout can shift
     *  between the scroll and the capture = the old race), and the crop
     *  accounts for the WebView's offset inside the window (toolbar). */
    /** Element screenshot with one native retry: the capture can flake
     *  when the app is backgrounded mid-render or the surface is briefly
     *  invalid. "element not found" is deterministic — not retried. */
    private byte[] elementScreenshot(String selector) throws Exception {
        Exception last = null;
        for (int attempt = 0; attempt < 2; attempt++) {
            try {
                return elementScreenshotOnce(selector);
            } catch (Exception e) {
                last = e;
                String m = e.getMessage();
                if (m != null && m.startsWith("element not found")) throw e;
                try { Thread.sleep(600); } catch (InterruptedException ie) { break; }
            }
        }
        throw last;
    }

    private byte[] elementScreenshotOnce(String selector) throws Exception {
        String q = selector.replace("\\", "\\\\").replace("\"", "\\\"");
        String r = evalJs("(function(s){try{"
                + "var el=" + DQ + "(s);"
                + "if(!el)return 'null';"
                + "el.scrollIntoView({block:'center',inline:'center'});"
                + "return 'ok';}"
                + "catch(e){return 'ERR:'+e.message}})(\"" + q + "\")");
        if (r == null || r.equals("null") || r.startsWith("ERR:"))
            throw new Exception("element not found: " + selector);
        Thread.sleep(400); // let the scroll settle (worker thread)
        // Fresh rect after settling — the pre-sleep rect may be stale.
        // __kancilR gives top-window coords (crosses iframe bounds).
        r = evalJs("(function(s){try{"
                + "var el=" + DQ + "(s);"
                + "if(!el)return 'null';"
                + "var b=" + DQR + "(el);"
                + "return b.x+'|'+b.y+'|'+b.w+'|'+b.h"
                + "+'|'+window.innerWidth;}"
                + "catch(e){return 'ERR:'+e.message}})(\"" + q + "\")");
        if (r == null || r.equals("null") || r.startsWith("ERR:"))
            throw new Exception("element not found: " + selector);
        String[] parts = r.split("\\|");
        double rx = Double.parseDouble(parts[0]);
        double ry = Double.parseDouble(parts[1]);
        double ew = Double.parseDouble(parts[2]);
        double eh = Double.parseDouble(parts[3]);
        double cssW = parts.length > 4 ? Double.parseDouble(parts[4]) : 0;
        final int[] loc = uiGet(() -> {
            WebView wv = activeWeb();
            int[] l = new int[2];
            wv.getLocationInWindow(l);
            return new int[]{l[0], l[1], wv.getWidth()};
        });
        byte[] win = screenshot();
        Bitmap bmp = android.graphics.BitmapFactory.decodeByteArray(
                win, 0, win.length);
        if (bmp == null) throw new Exception("capture failed");
        // CSS px -> device px: window shot is device pixels, rect is CSS px.
        float scale = (cssW > 0) ? (float) loc[2] / (float) cssW : 1f;
        if (scale <= 0 || scale > 10) scale = 1f; // sanity
        // Window shot includes the toolbar; the drawWebView() fallback
        // (backgrounded) is WebView-sized, so its offset is (0,0).
        int ox = lastShotWebViewOnly ? 0 : loc[0];
        int oy = lastShotWebViewOnly ? 0 : loc[1];
        int cx = Math.max(0, ox + (int) (rx * scale));
        int cy = Math.max(0, oy + (int) (ry * scale));
        int cw = Math.min(bmp.getWidth() - cx,
                Math.max(1, (int) (ew * scale)));
        int ch = Math.min(bmp.getHeight() - cy,
                Math.max(1, (int) (eh * scale)));
        if (cw <= 0 || ch <= 0)
            throw new Exception("element outside capture area");
        Bitmap crop = Bitmap.createBitmap(bmp, cx, cy, cw, ch);
        bmp.recycle();
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        crop.compress(Bitmap.CompressFormat.PNG, 90, bos);
        crop.recycle();
        return bos.toByteArray();
    }

    /** Fill form #fi with {name: value}. Values starting with "@" are
     *  file paths: staged for onShowFileChooser and the file input is
     *  clicked natively. React-safe value setting + input/change events. */
    private JSONObject fillForm(int fi, JSONObject fields,
                                boolean submit) throws Exception {
        // Stage file fields first (native side), click their inputs.
        java.util.Iterator<String> keys = fields.keys();
        while (keys.hasNext()) {
            String k = keys.next();
            String v = fields.optString(k, "");
            if (v.startsWith("@")) {
                String p = v.substring(1);
                if (new java.io.File(p).exists()) {
                    pendingUploadPath = p;
                    String q = k.replace("\\", "\\\\").replace("'", "\\'");
                    evalJs("(function(){var f=document.forms[" + fi + "];"
                            + "if(!f)return 'noform';"
                            + "var el=f.elements['" + q + "'];"
                            + "if(el){el.click();return 'clicked';}"
                            + "return 'missing'})()");
                }
            }
        }
        String js = "(function(fi,vals){"
                + "var out={ok:true,filled:[],missing:[]};"
                + "var f=document.forms[fi];"
                + "if(!f){out.ok=false;out.error='no such form';"
                + "return JSON.stringify(out);}"
                + "for(var name in vals){"
                + "var v=vals[name];"
                + "if(typeof v==='string'&&v.charAt(0)==='@')continue;"
                + "var el=f.elements[name];"
                + "if(!el){out.missing.push(name);continue;}"
                + "if(el.length&&!el.tagName){"
                + "for(var i=0;i<el.length;i++){"
                + "if(el[i].value==v){el[i].checked=true;"
                + "el[i].dispatchEvent(new Event('change',{bubbles:true}));}}"
                + "out.filled.push(name);continue;}"
                + "var tag=el.tagName;"
                + "if(tag==='SELECT'){el.value=v;}"
                + "else if(el.type==='checkbox'){"
                + "el.checked=(v==='true'||v==='1');}"
                + "else{var proto=tag==='TEXTAREA'"
                + "?HTMLTextAreaElement.prototype"
                + ":HTMLInputElement.prototype;"
                + "var d=Object.getOwnPropertyDescriptor(proto,'value');"
                + "if(d&&d.set)d.set.call(el,v);else el.value=v;}"
                + "el.dispatchEvent(new Event('input',{bubbles:true}));"
                + "el.dispatchEvent(new Event('change',{bubbles:true}));"
                + "out.filled.push(name);}"
                + (submit
                    ? "if(f.requestSubmit)f.requestSubmit();else f.submit();"
                    : "")
                + "return JSON.stringify(out);})("
                + fi + "," + fields.toString() + ")";
        String res = evalJs(js);
        JSONObject o = new JSONObject();
        try {
            if (res != null && !res.isEmpty() && !"null".equals(res))
                o = new JSONObject(res);
        } catch (Exception ignored) {}
        if (!o.has("ok")) o.put("ok", true);
        return o;
    }

    @Override
    protected void onPause() {
        saveTabs();
        super.onPause();
    }

    @Override
    public void onBackPressed() {
        if (customView != null) { hideCustomView(); return; }
        WebView w = activeWeb();
        if (w.canGoBack()) w.goBack();
        else super.onBackPressed();
    }

    /** Must run on the UI thread. */
    private void hideCustomView() {
        if (customView == null) return;
        try { webContainer.removeView(customView); } catch (Exception ignored) {}
        customView = null;
        if (customViewCallback != null) {
            try { customViewCallback.onCustomViewHidden(); }
            catch (Exception ignored) {}
            customViewCallback = null;
        }
    }

    @Override
    protected void onDestroy() {
        try {
            stopService(new Intent(this, AgentKeepAliveService.class));
        } catch (Exception ignored) {}
        if (server != null) server.stop();
        for (Tab t : tabs) t.web.destroy();
        super.onDestroy();
    }
}
