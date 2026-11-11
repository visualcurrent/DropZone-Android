package com.visualcurrent.dropzone;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.util.List;

/**
 * Runs the DropZone Python server for as long as this service is alive.
 *
 * It is a FOREGROUND service (it shows a permanent notification). Android kills
 * ordinary background work within minutes; a foreground service is the
 * sanctioned way to keep a long transfer running after the user leaves the app.
 *
 * State is published through the static fields below; MainActivity reads them
 * on a one-second timer. That is deliberately the simplest possible link
 * between the two - there is exactly one service and one screen.
 */
public class ServerService extends Service {

    private static final String CHANNEL_ID = "dropzone_server";
    private static final int NOTIFICATION_ID = 1;

    // --- state read by MainActivity ---------------------------------------
    public static volatile boolean running = false;
    public static volatile String remoteUrl = "";     // e.g. http://192.168.1.20:7070/remote
    public static volatile String coreVersion = "";   // DropZone.py __version__
    public static volatile String lastError = "";     // empty when the last start worked

    // Held while the server runs so transfers continue with the screen off:
    // the wake lock keeps the CPU awake, the WiFi lock keeps the radio out of
    // its power-saving mode. Both are released in onDestroy().
    private PowerManager.WakeLock wakeLock;
    private WifiManager.WifiLock wifiLock;

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // Android requires startForeground() within a few seconds of the
        // service being started, so it comes first.
        Notification starting = buildNotification("Starting...");
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, starting,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);
        } else {
            startForeground(NOTIFICATION_ID, starting);
        }

        if (!running) {
            try {
                // Start the embedded interpreter once per app process.
                if (!Python.isStarted()) {
                    Python.start(new AndroidPlatform(getApplicationContext()));
                }
                PyObject host = Python.getInstance().getModule("dz_host");
                // dz_host.start() returns [remote_url, core_version].
                List<PyObject> info = host.callAttr("start", getFilesDir().getAbsolutePath()).asList();
                remoteUrl = info.get(0).toString();
                coreVersion = info.get(1).toString();
                lastError = "";
                running = true;
                acquireLocks();
                getSystemService(NotificationManager.class)
                        .notify(NOTIFICATION_ID, buildNotification("Sharing at " + remoteUrl));
            } catch (Throwable t) {
                // Most likely cause: port 7070 already in use. Report and quit.
                lastError = String.valueOf(t.getMessage());
                running = false;
                stopSelf();
            }
        }
        // NOT_STICKY: if Android kills the app, do not silently restart a
        // server whose shared-file list has been lost.
        return START_NOT_STICKY;
    }

    @Override
    public void onDestroy() {
        if (running) {
            try {
                Python.getInstance().getModule("dz_host").callAttr("stop");
            } catch (Throwable t) {
                lastError = String.valueOf(t.getMessage());
            }
            running = false;
        }
        releaseLocks();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;   // nothing binds to this service; it is started and stopped only
    }

    private void acquireLocks() {
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "DropZone:server");
        wakeLock.acquire();

        WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
        wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "DropZone:server");
        wifiLock.acquire();
    }

    private void releaseLocks() {
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        if (wifiLock != null && wifiLock.isHeld()) wifiLock.release();
    }

    /** The permanent notification; tapping it reopens the app screen. */
    private Notification buildNotification(String text) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        // Creating a channel that already exists is a no-op.
        nm.createNotificationChannel(new NotificationChannel(
                CHANNEL_ID, "DropZone server", NotificationManager.IMPORTANCE_LOW));

        PendingIntent open = PendingIntent.getActivity(this, 0,
                new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);

        return new Notification.Builder(this, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_launcher)
                .setContentTitle("DropZone is running")
                .setContentText(text)
                .setContentIntent(open)
                .setOngoing(true)
                .build();
    }
}
