package com.kancil.browser;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
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
import java.util.concurrent.atomic.AtomicReference;

public class MainActivity extends Activity {

    /** One browser tab: its own WebView + network log. */
    private static class Tab {
        final int id;
        final WebView web;
        final NetLog netlog = new NetLog();
        String title = "";
        String defaultUA = "";
        Tab(int id, WebView web) { this.id = id; this.web = web; }
    }

    private FrameLayout webContainer;
    private EditText urlBar;
    private Button tabCountBtn;
    private ProgressBar progressBar;
    private TextView agentStatus;
    private TextView agentToast;    private final Handler ui = new Handler(Looper.getMainLooper());
    private final Handler toastHide = new Handler(Looper.getMainLooper());
    private final List<Tab> tabs = new ArrayList<>();
    private Tab active;
    private int tabSeq = 0;
    private AgentServer server;
    private SharedPreferences prefs;

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

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        setContentView(R.layout.activity_main);
        webContainer = findViewById(R.id.web_container);
        urlBar = findViewById(R.id.url_bar);
        tabCountBtn = findViewById(R.id.btn_tabs);
        progressBar = findViewById(R.id.progress);
        agentStatus = findViewById(R.id.agent_status);
        agentToast = findViewById(R.id.agent_toast);
        prefs = getSharedPreferences("kancil", MODE_PRIVATE);
        getWindow().setStatusBarColor(0xFF0E6B2E);

        findViewById(R.id.btn_back).setOnClickListener(v -> goBack());
        findViewById(R.id.btn_fwd).setOnClickListener(v -> goForward());
        findViewById(R.id.btn_menu).setOnClickListener(v -> showMenu(v));
        tabCountBtn.setOnClickListener(v -> showTabSwitcher());
        urlBar.setOnEditorActionListener((v, actionId, ev) -> {
            if (actionId == EditorInfo.IME_ACTION_GO) {
                navigate(urlBar.getText().toString());
                return true;
            }
            return false;
        });

        startAgentServer();
        newTab(homeUrl(), false);
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
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setBuiltInZoomControls(true);
        s.setDisplayZoomControls(false);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(w, true);
        w.setDownloadListener((url, userAgent, contentDisposition,
                               mimeType, contentLength) -> {
            try {
                DownloadManager.Request req =
                        new DownloadManager.Request(Uri.parse(url));
                req.setMimeType(mimeType);
                String name = URLUtil.guessFileName(url, contentDisposition,
                        mimeType);
                req.setTitle(name);
                req.setDescription("Kancil Browser");
                req.setNotificationVisibility(DownloadManager.Request
                        .VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                req.setDestinationInExternalPublicDir(
                        Environment.DIRECTORY_DOWNLOADS, name);
                DownloadManager dm = (DownloadManager)
                        getSystemService(DOWNLOAD_SERVICE);
                dm.enqueue(req);
                Toast.makeText(MainActivity.this,
                        "Mengunduh: " + name, Toast.LENGTH_SHORT).show();
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
        if (desktop()) {
            String ua = tab.defaultUA.isEmpty()
                    ? s.getUserAgentString() : tab.defaultUA;
            // turn the mobile UA into a desktop one, keeping the version
            ua = ua.replace("; Mobile", "").replace("Mobile ", "")
                   .replace("Android ", "");
            s.setUserAgentString(ua);
        } else if (!tab.defaultUA.isEmpty()) {
            s.setUserAgentString(tab.defaultUA);
        }
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
        tabCountBtn.setText(String.valueOf(tabs.size()));
    }

    private void showSettings() {
        String cur = engineKey();
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        root.setPadding(pad, pad / 2, pad, pad / 2);

        TextView engLabel = new TextView(this);
        engLabel.setText("Search engine");
        engLabel.setTextSize(13);
        root.addView(engLabel);

        RadioGroup rg = new RadioGroup(this);
        int checkedId = 0;
        for (int i = 0; i < ENGINES.length; i++) {
            RadioButton rb = new RadioButton(this);
            rb.setText(ENGINES[i][1]);
            rb.setId(1000 + i);
            if (ENGINES[i][0].equals(cur)) checkedId = rb.getId();
            rg.addView(rb);
        }
        rg.check(checkedId);
        root.addView(rg);

        final CheckBox cbAd = new CheckBox(this);
        cbAd.setText("Adblock (blokir iklan & tracker)");
        cbAd.setChecked(adblock());
        final CheckBox cbData = new CheckBox(this);
        cbData.setText("Hemat data (tanpa gambar)");
        cbData.setChecked(dataSaver());
        final CheckBox cbDesk = new CheckBox(this);
        cbDesk.setText("Situs desktop");
        cbDesk.setChecked(desktop());
        final CheckBox cbDark = new CheckBox(this);
        cbDark.setText("Mode malam");
        cbDark.setChecked(dark());
        root.addView(cbAd);
        root.addView(cbData);
        root.addView(cbDesk);
        root.addView(cbDark);

        ScrollView sv = new ScrollView(this);
        sv.addView(root);

        final int[] selEng = {0};
        for (int i = 0; i < ENGINES.length; i++)
            if (ENGINES[i][0].equals(cur)) selEng[0] = i;
        rg.setOnCheckedChangeListener((g, id) -> selEng[0] = id - 1000);

        new AlertDialog.Builder(this)
                .setTitle("Pengaturan")
                .setView(sv)
                .setPositiveButton("OK", (d, which) -> {
                    boolean needReload = cbData.isChecked() != dataSaver()
                            || cbDesk.isChecked() != desktop();
                    prefs.edit()
                            .putString("search_engine", ENGINES[selEng[0]][0])
                            .putBoolean("adblock", cbAd.isChecked())
                            .putBoolean("datasaver", cbData.isChecked())
                            .putBoolean("desktop", cbDesk.isChecked())
                            .putBoolean("dark", cbDark.isChecked())
                            .apply();
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

    private static final String DARK_ON =
            "(function(){if(document.getElementById('kancil-dark'))return;"
            + "var s=document.createElement('style');s.id='kancil-dark';"
            + "s.textContent='html{filter:invert(1) hue-rotate(180deg);background:#111 !important}'"
            + "+'img,video,picture,canvas,[style*=\"background-image\"]{filter:invert(1) hue-rotate(180deg)}';"
            + "document.head.appendChild(s);})()";
    private static final String DARK_OFF =
            "(function(){var s=document.getElementById('kancil-dark');"
            + "if(s)s.remove();})()";

    private void setDarkAll(boolean on) {
        ui.post(() -> {
            for (Tab t : new ArrayList<>(tabs))
                t.web.evaluateJavascript(on ? DARK_ON : DARK_OFF, null);
        });
    }

    // ---------- find in page ----------

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
    }

    private void showMenu(View anchor) {
        PopupMenu pm = new PopupMenu(this, anchor);
        pm.getMenu().add("Reload");
        pm.getMenu().add("Tab baru");
        pm.getMenu().add("Reader mode");
        pm.getMenu().add("Cari di halaman");
        pm.getMenu().add("Download");
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
                case "Pengaturan":
                    showSettings();
                    break;
            }
            return true;
        });
        pm.show();
    }

    private void showTabSwitcher() {
        final List<Tab> copy = new ArrayList<>(tabs);
        String[] names = new String[copy.size() + 1];
        for (int i = 0; i < copy.size(); i++) {
            Tab t = copy.get(i);
            String label = t.title.isEmpty() ? (t.web.getUrl() != null
                    ? t.web.getUrl() : "new tab") : t.title;
            if (label.length() > 40) label = label.substring(0, 40) + "…";
            names[i] = (t == active ? "● " : "○ ") + label;
        }
        names[copy.size()] = "＋ New tab";
        new AlertDialog.Builder(this)
                .setTitle("Tabs")
                .setAdapter(new ArrayAdapter<>(this,
                        android.R.layout.simple_list_item_1, names),
                        (d, which) -> {
                            if (which < copy.size())
                                activateTab(copy.get(which).id);
                            else newTab(homeUrl(), false);
                        })
                .setNegativeButton("Close", null)
                .show();
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
                return false;
            }

            @Override
            public WebResourceResponse shouldInterceptRequest(WebView v,
                                                             WebResourceRequest r) {
                String url = String.valueOf(r.getUrl());
                if (adblock() && isAd(url)) {
                    NetLog.Entry e = tab.netlog.add(r.getMethod(), url, null);
                    tab.netlog.fail(e, "blocked:adblock");
                    try {
                        return new WebResourceResponse("text/plain", "utf-8",
                                200, "OK", new HashMap<String, String>(),
                                new java.io.ByteArrayInputStream(new byte[0]));
                    } catch (Exception ignored) {}
                    return null;
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
                if (tab == active) ui.post(() -> {
                    urlBar.setText(url);
                    agentStatus.setText("Agent :8080");
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
                if (tab == active) ui.post(() -> {
                    urlBar.setText(url);
                    if (!tab.title.isEmpty()) setTitle(tab.title);
                    progressBar.setVisibility(View.GONE);
                });
                if (dark()) {
                    v.evaluateJavascript(DARK_ON, null);
                }
                CookieManager.getInstance().flush();
            }

            @Override
            public void onReceivedError(WebView v, WebResourceRequest r,
                                        android.webkit.WebResourceError e) {
                if (r.isForMainFrame() && tab == active) {
                    ui.post(() -> Toast.makeText(MainActivity.this,
                            "Load error: " + e.getDescription(),
                            Toast.LENGTH_SHORT).show());
                }
            }
        };
    }

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
        };
    }

    private void navigate(String input) {
        String u = input.trim();
        if (!u.matches("^[a-zA-Z][a-zA-Z0-9+.-]*:.*")) {
            if (u.contains(".") && !u.contains(" ")) u = "https://" + u;
            else u = searchUrl(u);
        }
        final String url = u;
        ui.post(() -> {
            urlBar.setText(url);
            activeWeb().loadUrl(url);
        });
    }

    // ---------- JS bridge (worker thread -> UI thread) ----------

    private String evalJs(String expr) throws Exception {
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

    private void agentNote(final String msg) {
        ui.post(() -> {
            agentToast.setText("agent → " + msg);
            agentToast.setVisibility(View.VISIBLE);
            agentStatus.setText("Agent :8080 • aktif");
            toastHide.removeCallbacksAndMessages(null);
            toastHide.postDelayed(() -> {
                agentToast.setVisibility(View.GONE);
                agentStatus.setText("Agent :8080");
            }, 4000);
        });
    }

    // ---------- agent server ----------

    private JSONObject tabJson(Tab t) {
        JSONObject o = new JSONObject();
        try {
            o.put("id", t.id);
            o.put("url", t.web.getUrl() == null ? "" : t.web.getUrl());
            o.put("title", t.title);
            o.put("active", t == active);
        } catch (Exception ignored) {}
        return o;
    }

    private void startAgentServer() {
        server = new AgentServer((method, path, query, body) -> {
            switch (path) {
                case "/status": {
                    JSONObject o = new JSONObject();
                    o.put("ok", true);
                    o.put("agent", "kancil-browser/1.1");
                    o.put("url", uiGet(() -> activeWeb().getUrl()));
                    o.put("title", active.title);
                    o.put("tab", active.id);
                    o.put("tab_count", tabs.size());
                    o.put("network_count", active.netlog.size());
                    return AgentServer.Response.json(o);
                }
                case "/tabs": {
                    JSONArray a = new JSONArray();
                    for (Tab t : new ArrayList<>(tabs)) a.put(tabJson(t));
                    JSONObject o = new JSONObject();
                    o.put("ok", true);
                    o.put("tabs", a);
                    o.put("active", active.id);
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
                    if (findTab(id) == null)
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
                case "/click": {
                    String sel = body.optString("selector", query.get("selector"));
                    if (sel == null) return AgentServer.Response.err(400, "missing selector");
                    String r = evalJs("(function(){var el=document.querySelector("
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
                    String r = evalJs("(function(){var el=document.querySelector("
                            + JSONObject.quote(sel) + ");if(!el) return 'not-found';"
                            + "el.focus();document.execCommand('selectAll',false,null);"
                            + "document.execCommand('insertText',false,"
                            + JSONObject.quote(text) + ");"
                            + "el.dispatchEvent(new Event('input',{bubbles:true}));"
                            + "el.dispatchEvent(new Event('change',{bubbles:true}));"
                            + "return 'typed'})()");
                    JSONObject o = new JSONObject();
                    o.put("ok", true); o.put("result", r);
                    agentNote("type " + sel);
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
        } catch (Exception e) {
            Toast.makeText(this, "Agent server failed: " + e.getMessage(),
                    Toast.LENGTH_LONG).show();
        }
    }

    private static AgentServer.Response ok() {
        JSONObject o = new JSONObject();
        try { o.put("ok", true); } catch (Exception ignored) {}
        return AgentServer.Response.json(o);
    }

    private interface UiGet<T> { T get(); }

    private <T> T uiGet(UiGet<T> f) throws Exception {
        final AtomicReference<T> out = new AtomicReference<>();
        final CountDownLatch latch = new CountDownLatch(1);
        ui.post(() -> { out.set(f.get()); latch.countDown(); });
        if (!latch.await(15, TimeUnit.SECONDS)) throw new Exception("ui timeout");
        return out.get();
    }

    private byte[] screenshot() throws Exception {
        final AtomicReference<Bitmap> ref = new AtomicReference<>();
        final CountDownLatch latch = new CountDownLatch(1);
        ui.post(() -> {
            View root = getWindow().getDecorView();
            int w = root.getWidth(), h = root.getHeight();
            if (w <= 0 || h <= 0) { latch.countDown(); return; }
            final Bitmap bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
            ref.set(bmp);
            if (Build.VERSION.SDK_INT >= 26) {
                PixelCopy.request(getWindow(), bmp,
                        copyResult -> latch.countDown(),
                        new Handler(Looper.getMainLooper()));
            } else {
                root.draw(new android.graphics.Canvas(bmp));
                latch.countDown();
            }
        });
        if (!latch.await(15, TimeUnit.SECONDS)) throw new Exception("screenshot timeout");
        Bitmap bmp = ref.get();
        if (bmp == null) throw new Exception("screenshot failed");
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        bmp.compress(Bitmap.CompressFormat.PNG, 100, bos);
        return bos.toByteArray();
    }

    @Override
    public void onBackPressed() {
        WebView w = activeWeb();
        if (w.canGoBack()) w.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        if (server != null) server.stop();
        for (Tab t : tabs) t.web.destroy();
        super.onDestroy();
    }
}
