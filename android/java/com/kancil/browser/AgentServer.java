package com.kancil.browser;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;

/**
 * Minimal HTTP agent server (no dependencies).
 * Listens on 127.0.0.1:8080, one request per connection.
 */
public class AgentServer {
    public static final int PORT = 8080;

    public interface Handler {
        /** Runs on a worker thread; must not touch the WebView directly. */
        Response handle(String method, String path, Map<String, String> query,
                        JSONObject body) throws Exception;
    }

    public static class Response {
        public final int code;
        public final String contentType;
        public final byte[] body;
        Response(int code, String contentType, byte[] body) {
            this.code = code; this.contentType = contentType; this.body = body;
        }
        static Response json(JSONObject o) {
            return new Response(200, "application/json",
                    o.toString().getBytes(StandardCharsets.UTF_8));
        }
        static Response err(int code, String msg) {
            JSONObject o = new JSONObject();
            try { o.put("ok", false); o.put("error", msg); } catch (Exception ignored) {}
            return new Response(code, "application/json",
                    o.toString().getBytes(StandardCharsets.UTF_8));
        }
    }

    private final Handler handler;
    private ServerSocket server;
    private volatile boolean running;
    /** API key (header X-Kancil-Key). null/empty = auth off (legacy). */
    private volatile String apiKey;

    public AgentServer(Handler handler) {
        this.handler = handler;
    }

    public void setApiKey(String k) { apiKey = k; }

    public void start() throws Exception {
        server = new ServerSocket(PORT, 16,
                java.net.InetAddress.getByName("127.0.0.1"));
        running = true;
        Thread t = new Thread(this::acceptLoop, "agent-server");
        t.setDaemon(true);
        t.start();
    }

    public void stop() {
        running = false;
        try { if (server != null) server.close(); } catch (Exception ignored) {}
    }

    private void acceptLoop() {
        while (running) {
            try {
                final Socket s = server.accept();
                Thread t = new Thread(() -> serve(s), "agent-conn");
                t.setDaemon(true);
                t.start();
            } catch (Exception e) {
                if (!running) break;
            }
        }
    }

    private void serve(Socket s) {
        try {
            s.setSoTimeout(120000);
            InputStream in = s.getInputStream();
            OutputStream out = s.getOutputStream();
            String head = readHead(in);
            if (head == null || head.isEmpty()) { s.close(); return; }
            String[] lines = head.split("\r\n");
            String[] rl = lines[0].split(" ", 3);
            if (rl.length < 2) { s.close(); return; }
            String method = rl[0].toUpperCase();
            String target = rl[1];
            String path = target;
            Map<String, String> query = new HashMap<>();
            int qi = target.indexOf('?');
            if (qi >= 0) {
                path = target.substring(0, qi);
                query = parseQuery(target.substring(qi + 1));
            }
            int contentLength = 0;
            Map<String, String> headers = new HashMap<>();
            for (int i = 1; i < lines.length; i++) {
                int ci = lines[i].indexOf(':');
                if (ci > 0) {
                    String hn = lines[i].substring(0, ci).trim()
                            .toLowerCase(java.util.Locale.US);
                    String hv = lines[i].substring(ci + 1).trim();
                    headers.put(hn, hv);
                    if (hn.equals("content-length")) {
                        try {
                            contentLength = Integer.parseInt(hv);
                        } catch (Exception ignored) {}
                    }
                }
            }
            // API key auth (1.28+): tiap request wajib bawa X-Kancil-Key
            // yang cocok, kecuali belum ada key yang dikonfigurasi.
            String key = apiKey;
            if (key != null && !key.isEmpty()) {
                String got = headers.get("x-kancil-key");
                if (!key.equals(got)) {
                    writeResponse(out, Response.err(401,
                            "unauthorized: bad or missing X-Kancil-Key"));
                    s.close();
                    return;
                }
            }
            JSONObject body = new JSONObject();
            if (contentLength > 0 && contentLength < 8 * 1024 * 1024) {
                byte[] buf = readFully(in, contentLength);
                String bs = new String(buf, StandardCharsets.UTF_8).trim();
                if (!bs.isEmpty()) {
                    try { body = new JSONObject(bs); }
                    catch (Exception e) { body = new JSONObject(); }
                }
            }
            Response r;
            try {
                r = handler.handle(method, path, query, body);
            } catch (Throwable e) {
                // Throwable, not Exception: an OOM must become a 500,
                // never a silently dead handler thread.
                String msg = String.valueOf(e.getMessage());
                r = Response.err(500, e.getClass().getSimpleName() + ": "
                        + msg.substring(0, Math.min(200, msg.length())));
            }
            writeResponse(out, r);
            s.close();
        } catch (Exception ignored) {
            try { s.close(); } catch (Exception ignored2) {}
        }
    }

    private static String readHead(InputStream in) throws Exception {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        int[] last = new int[4];
        int n = 0;
        while (n < 65536) {
            int b = in.read();
            if (b < 0) break;
            bos.write(b);
            last[0] = last[1]; last[1] = last[2]; last[2] = last[3]; last[3] = b;
            n++;
            if (n >= 4 && last[0] == '\r' && last[1] == '\n'
                    && last[2] == '\r' && last[3] == '\n') break;
        }
        return bos.toString("UTF-8");
    }

    private static byte[] readFully(InputStream in, int len) throws Exception {
        byte[] buf = new byte[len];
        int off = 0;
        while (off < len) {
            int r = in.read(buf, off, len - off);
            if (r < 0) break;
            off += r;
        }
        if (off < len) {
            byte[] small = new byte[off];
            System.arraycopy(buf, 0, small, 0, off);
            return small;
        }
        return buf;
    }

    private static Map<String, String> parseQuery(String q) {
        Map<String, String> m = new HashMap<>();
        for (String part : q.split("&")) {
            int ei = part.indexOf('=');
            try {
                if (ei >= 0) m.put(java.net.URLDecoder.decode(part.substring(0, ei), "UTF-8"),
                        java.net.URLDecoder.decode(part.substring(ei + 1), "UTF-8"));
                else if (!part.isEmpty()) m.put(java.net.URLDecoder.decode(part, "UTF-8"), "");
            } catch (Exception ignored) {}
        }
        return m;
    }

    private static void writeResponse(OutputStream out, Response r) throws Exception {
        String status = r.code == 200 ? "OK" : r.code == 404 ? "Not Found" : "Error";
        String h = "HTTP/1.1 " + r.code + " " + status + "\r\n"
                + "Content-Type: " + r.contentType + "\r\n"
                + "Content-Length: " + r.body.length + "\r\n"
                + "Connection: close\r\n"
                + "Access-Control-Allow-Origin: *\r\n\r\n";
        out.write(h.getBytes(StandardCharsets.UTF_8));
        out.write(r.body);
        out.flush();
    }
}
