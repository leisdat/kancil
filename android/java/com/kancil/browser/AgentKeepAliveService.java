package com.kancil.browser;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;

/**
 * Foreground service that keeps the Kancil agent HTTP server alive.
 * Without this, MIUI/EMUI task killers freeze or kill the app when the
 * screen is off, and the Termux agent starts timing out.
 */
public class AgentKeepAliveService extends Service {
    public static final String CHANNEL = "kancil_agent";
    private static final int NOTIF_ID = 7001;

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        NotificationManager nm =
                (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CHANNEL,
                    "Kancil agent", NotificationManager.IMPORTANCE_LOW);
            ch.setDescription("Menjaga agent browser tetap hidup");
            nm.createNotificationChannel(ch);
        }
        Intent open = new Intent(this, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT
                        | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b =
                Build.VERSION.SDK_INT >= 26
                        ? new Notification.Builder(this, CHANNEL)
                        : new Notification.Builder(this);
        Notification n = b
                .setContentTitle("Kancil agent aktif")
                .setContentText("Browser siap dikendalikan dari Termux")
                .setSmallIcon(R.drawable.ic_agent)
                .setContentIntent(pi)
                .setOngoing(true)
                .build();
        // specialUse (sideloaded app): keep the agent server process alive.
        startForeground(NOTIF_ID, n);
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        stopForeground(true);
        super.onDestroy();
    }
}
