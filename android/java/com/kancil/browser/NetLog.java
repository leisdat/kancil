package com.kancil.browser;

import org.json.JSONArray;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

/** Thread-safe per-request network log (DevTools-style). */
public class NetLog {
    public static class Entry {
        public final int id;
        public final String t;
        public final String method;
        public final String url;
        public volatile Integer status;      // null until response/error
        public volatile String mime = "-";
        public volatile long size;
        public volatile long ms;
        public final long t0 = System.currentTimeMillis();
        public final Map<String, String> reqHeaders;
        public volatile String error;

        Entry(int id, String method, String url, Map<String, String> reqHeaders) {
            this.id = id;
            this.method = method;
            this.url = url;
            this.reqHeaders = reqHeaders;
            this.t = new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date());
        }

        JSONObject toJson() {
            JSONObject o = new JSONObject();
            try {
                o.put("id", id);
                o.put("t", t);
                o.put("method", method);
                o.put("url", url);
                o.put("status", status == null ? JSONObject.NULL : status);
                o.put("mime", mime);
                o.put("size", size);
                o.put("ms", ms > 0 ? ms : (System.currentTimeMillis() - t0));
                if (reqHeaders != null) o.put("req_headers", new JSONObject(reqHeaders));
                if (error != null) o.put("error", error);
            } catch (Exception ignored) {}
            return o;
        }
    }

    private final List<Entry> entries = new ArrayList<>();
    private final AtomicInteger seq = new AtomicInteger(0);
    private static final int MAX = 500;

    public synchronized Entry add(String method, String url, Map<String, String> reqHeaders) {
        Entry e = new Entry(seq.incrementAndGet(), method, url, reqHeaders);
        entries.add(e);
        while (entries.size() > MAX) entries.remove(0);
        return e;
    }

    public synchronized void finish(Entry e, Integer status, String mime, long size) {
        e.status = status;
        if (mime != null) e.mime = mime;
        e.size = size;
        e.ms = System.currentTimeMillis() - e.t0;
    }

    public synchronized void fail(Entry e, String err) {
        e.error = err;
        e.ms = System.currentTimeMillis() - e.t0;
    }

    public synchronized JSONArray toJson() {
        JSONArray a = new JSONArray();
        for (Entry e : entries) a.put(e.toJson());
        return a;
    }

    public synchronized void clear() {
        entries.clear();
    }

    public synchronized int size() {
        return entries.size();
    }

    /** Most recent entry for url whose status is still unknown
     *  (shouldInterceptRequest only logs; statuses are stamped by
     *  onPageFinished / onReceivedHttpError since WebView does not
     *  expose subresource statuses). */
    public synchronized Entry latestFor(String url) {
        for (int i = entries.size() - 1; i >= 0; i--) {
            Entry e = entries.get(i);
            if (e.status == null && e.error == null
                    && url != null && url.equals(e.url)) return e;
        }
        return null;
    }

    /** Millis timestamp of the most recent request, or 0 if empty.
     *  Used by /wait/idle to detect network quiet. */
    public synchronized long lastT0() {
        return entries.isEmpty() ? 0
                : entries.get(entries.size() - 1).t0;
    }
}
