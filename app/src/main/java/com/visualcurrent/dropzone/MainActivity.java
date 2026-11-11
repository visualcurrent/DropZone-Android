package com.visualcurrent.dropzone;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * The app's only screen: Start/Stop the server, show the address other devices
 * use, and open the host page in the phone's own browser.
 *
 * The host page is opened in the browser (not shown inside the app) on purpose:
 * the browser already handles file picking and multi-GB downloads, and that is
 * the path DropZone has been field-tested on.
 */
public class MainActivity extends Activity {

    private static final String HOST_URL = "http://localhost:7070";

    private TextView status;
    private Button toggle;
    private Button openHost;

    // Re-reads ServerService's state once a second while the screen is visible.
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable refresher = new Runnable() {
        @Override public void run() {
            refresh();
            handler.postDelayed(this, 1000);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // The layout is three widgets, so it is built here instead of in XML.
        int pad = (int) (24 * getResources().getDisplayMetrics().density);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER_HORIZONTAL);
        root.setPadding(pad, pad, pad, pad);
        // No title widget: the system title bar already shows "DropZone".

        status = new TextView(this);
        status.setTextSize(16);
        status.setTextIsSelectable(true);          // lets the address be long-pressed and copied
        status.setPadding(0, pad, 0, pad);
        root.addView(status);

        toggle = new Button(this);
        toggle.setOnClickListener(v -> {
            Intent svc = new Intent(this, ServerService.class);
            if (ServerService.running) {
                stopService(svc);
            } else {
                ServerService.lastError = "";
                startForegroundService(svc);
            }
        });
        root.addView(toggle);

        openHost = new Button(this);
        openHost.setText("Open host page");
        openHost.setOnClickListener(v ->
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(HOST_URL))));
        root.addView(openHost);

        setContentView(root);

        // Android 13+ hides notifications until the user allows them. The
        // server runs either way; this only makes its notification visible.
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                   != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        handler.post(refresher);
    }

    @Override
    protected void onPause() {
        handler.removeCallbacks(refresher);
        super.onPause();
    }

    /** Copy ServerService's published state onto the screen. */
    private void refresh() {
        boolean on = ServerService.running;
        if (on) {
            status.setText("Running  (DropZone v" + ServerService.coreVersion + ")\n\n"
                    + "Others on this WiFi open:\n" + ServerService.remoteUrl + "\n\n"
                    + "The host page shows the QR code.");
        } else if (!ServerService.lastError.isEmpty()) {
            status.setText("Could not start:\n" + ServerService.lastError);
        } else {
            status.setText("Stopped.\nStopping deletes the files shared in this session.");
        }
        toggle.setText(on ? "Stop server" : "Start server");
        openHost.setEnabled(on);
    }
}
