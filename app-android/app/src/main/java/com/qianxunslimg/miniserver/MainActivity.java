package com.qianxunslimg.miniserver;

import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import java.io.BufferedReader;
import java.io.DataOutputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {

    private static final String LINUX_SCRIPT = "/data/local/linux/linux.sh";
    private static final String BOOT_SCRIPT = "/data/adb/service.d/99-ubuntu-server.sh";

    private static final int C_BG = Color.parseColor("#090e1a");
    private static final int C_CARD = Color.parseColor("#11192e");
    private static final int C_CARD2 = Color.parseColor("#16213d");
    private static final int C_BORDER = Color.parseColor("#223055");
    private static final int C_TEXT = Color.parseColor("#e8eeff");
    private static final int C_MUTED = Color.parseColor("#8ca1cb");
    private static final int C_ACCENT = Color.parseColor("#4f8cff");
    private static final int C_GREEN = Color.parseColor("#34d399");
    private static final int C_RED = Color.parseColor("#f87171");

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    private View nativePage;
    private View webPage;
    private Button tabNativeBtn;
    private Button tabWebBtn;

    private TextView statusBadge;
    private TextView ipValueText;
    private TextView batteryValueText;
    private TextView servicesDetailText;
    private TextView logOutputText;
    private Switch autostartSwitch;
    private boolean updatingSwitch = false;

    private WebView webView;
    private TextView webUrlBar;
    private String currentIp = "127.0.0.1";
    private String currentWebUrl = "http://127.0.0.1:8888";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(C_BG);
        root.setLayoutParams(new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        root.addView(buildTopBar());

        FrameLayout contentHost = new FrameLayout(this);
        contentHost.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        nativePage = buildNativePage();
        webPage = buildWebPage();
        webPage.setVisibility(View.GONE);

        contentHost.addView(nativePage);
        contentHost.addView(webPage);
        root.addView(contentHost);

        setContentView(root);
        refreshStatus();
    }

    private View buildTopBar() {
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(14), dp(12), dp(14), dp(10));
        bar.setBackgroundColor(C_CARD);

        TextView title = new TextView(this);
        title.setText("小主机控制台");
        title.setTextColor(C_TEXT);
        title.setTextSize(TypedValue.COMPLEX_UNIT_SP, 17);
        title.setTypeface(null, Typeface.BOLD);
        title.setLayoutParams(new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        bar.addView(title);

        tabNativeBtn = makePillButton("宿主开关", true);
        tabWebBtn = makePillButton("Web 面板", false);

        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.setMarginEnd(dp(8));
        tabNativeBtn.setLayoutParams(lp);

        tabNativeBtn.setOnClickListener(v -> showTab(true));
        tabWebBtn.setOnClickListener(v -> {
            showTab(false);
            if (webView.getUrl() == null) {
                loadWeb(currentWebUrl);
            }
        });

        bar.addView(tabNativeBtn);
        bar.addView(tabWebBtn);
        return bar;
    }

    private View buildNativePage() {
        ScrollView scroll = new ScrollView(this);
        scroll.setLayoutParams(new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(dp(14), dp(14), dp(14), dp(24));

        // Card 1: Status & IP
        LinearLayout statusCard = makeCard();
        LinearLayout statusRow = new LinearLayout(this);
        statusRow.setOrientation(LinearLayout.HORIZONTAL);
        statusRow.setGravity(Gravity.CENTER_VERTICAL);

        TextView cardTitle = makeSectionTitle("Ubuntu 运行状态");
        cardTitle.setLayoutParams(new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        statusBadge = new TextView(this);
        statusBadge.setText("检测中...");
        statusBadge.setTextColor(C_MUTED);
        statusBadge.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        statusBadge.setPadding(dp(10), dp(4), dp(10), dp(4));
        statusBadge.setBackground(roundRect(C_CARD2, C_BORDER, 20));
        statusRow.addView(cardTitle);
        statusRow.addView(statusBadge);
        statusCard.addView(statusRow);

        ipValueText = makeInfoLine("局域网 IP：检测中...（点击复制）");
        ipValueText.setTextColor(C_ACCENT);
        ipValueText.setOnClickListener(v -> copyText("ssh", "ssh <user>@" + currentIp));
        statusCard.addView(ipValueText);

        batteryValueText = makeInfoLine("电池状态：检测中...");
        statusCard.addView(batteryValueText);

        servicesDetailText = makeInfoLine("服务状态：等待刷新");
        servicesDetailText.setTypeface(Typeface.MONOSPACE);
        servicesDetailText.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        statusCard.addView(servicesDetailText);

        Button refreshBtn = makeActionButton("刷新状态", C_CARD2, C_BORDER);
        refreshBtn.setOnClickListener(v -> refreshStatus());
        statusCard.addView(withTopMargin(refreshBtn, 10));
        col.addView(statusCard);

        // Card 2: Host Lifecycle Controls
        LinearLayout ctrlCard = makeCard();
        ctrlCard.addView(makeSectionTitle("小主机电源与自启"));

        LinearLayout btnRow = new LinearLayout(this);
        btnRow.setOrientation(LinearLayout.HORIZONTAL);
        btnRow.setPadding(0, dp(10), 0, dp(8));

        Button startBtn = makeActionButton("开启", Color.parseColor("#166534"), C_GREEN);
        Button stopBtn = makeActionButton("关闭", Color.parseColor("#7f1d1d"), C_RED);
        Button restartBtn = makeActionButton("重启", C_CARD2, C_BORDER);

        startBtn.setLayoutParams(weightLp(0, 6));
        stopBtn.setLayoutParams(weightLp(6, 6));
        restartBtn.setLayoutParams(weightLp(6, 0));

        startBtn.setOnClickListener(v -> runHostAction("start", "正在开启小主机..."));
        stopBtn.setOnClickListener(v -> runHostAction("stop", "正在关闭小主机并释放唤醒锁..."));
        restartBtn.setOnClickListener(v -> runHostAction("restart", "正在重启小主机..."));

        btnRow.addView(startBtn);
        btnRow.addView(stopBtn);
        btnRow.addView(restartBtn);
        ctrlCard.addView(btnRow);

        LinearLayout autoRow = new LinearLayout(this);
        autoRow.setOrientation(LinearLayout.HORIZONTAL);
        autoRow.setGravity(Gravity.CENTER_VERTICAL);
        autoRow.setPadding(dp(10), dp(10), dp(10), dp(10));
        autoRow.setBackground(roundRect(C_CARD2, C_BORDER, 10));

        TextView autoLabel = new TextView(this);
        autoLabel.setText("手机重启后自动拉起 Ubuntu 小主机");
        autoLabel.setTextColor(C_TEXT);
        autoLabel.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        autoLabel.setLayoutParams(new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        autostartSwitch = new Switch(this);
        autostartSwitch.setOnCheckedChangeListener((btn, checked) -> {
            if (updatingSwitch) return;
            toggleAutostart(checked);
        });
        autoRow.addView(autoLabel);
        autoRow.addView(autostartSwitch);
        ctrlCard.addView(withTopMargin(autoRow, 6));
        col.addView(withTopMargin(ctrlCard, 12));

        // Card 3: Quick Web Shortcuts
        LinearLayout webCard = makeCard();
        webCard.addView(makeSectionTitle("内置 Web 服务直达"));

        LinearLayout webRow = new LinearLayout(this);
        webRow.setOrientation(LinearLayout.HORIZONTAL);
        webRow.setPadding(0, dp(10), 0, 0);

        Button bDash = makeActionButton("控制台 :8888", C_ACCENT, C_ACCENT);
        Button bFb = makeActionButton("网盘 :8080", C_CARD2, C_BORDER);
        Button bTty = makeActionButton("终端 :7681", C_CARD2, C_BORDER);

        bDash.setLayoutParams(weightLp(0, 6));
        bFb.setLayoutParams(weightLp(6, 6));
        bTty.setLayoutParams(weightLp(6, 0));

        bDash.setOnClickListener(v -> openInWebTab("http://127.0.0.1:8888"));
        bFb.setOnClickListener(v -> openInWebTab("http://127.0.0.1:8080"));
        bTty.setOnClickListener(v -> openInWebTab("http://127.0.0.1:7681"));

        webRow.addView(bDash);
        webRow.addView(bFb);
        webRow.addView(bTty);
        webCard.addView(webRow);
        col.addView(withTopMargin(webCard, 12));

        // Card 4: Command Output
        LinearLayout logCard = makeCard();
        logCard.addView(makeSectionTitle("执行输出"));
        logOutputText = new TextView(this);
        logOutputText.setText("就绪。需要 Magisk Root 权限才能控制宿主脚本。");
        logOutputText.setTextColor(C_MUTED);
        logOutputText.setTypeface(Typeface.MONOSPACE);
        logOutputText.setTextSize(TypedValue.COMPLEX_UNIT_SP, 11.5f);
        logOutputText.setPadding(dp(10), dp(10), dp(10), dp(10));
        logOutputText.setBackground(roundRect(Color.parseColor("#070b15"), C_BORDER, 8));
        logCard.addView(withTopMargin(logOutputText, 8));
        col.addView(withTopMargin(logCard, 12));

        scroll.addView(col);
        return scroll;
    }

    private View buildWebPage() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setLayoutParams(new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        LinearLayout nav = new LinearLayout(this);
        nav.setOrientation(LinearLayout.HORIZONTAL);
        nav.setGravity(Gravity.CENTER_VERTICAL);
        nav.setPadding(dp(10), dp(6), dp(10), dp(6));
        nav.setBackgroundColor(Color.parseColor("#0d1528"));

        webUrlBar = new TextView(this);
        webUrlBar.setText(currentWebUrl);
        webUrlBar.setTextColor(C_MUTED);
        webUrlBar.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        webUrlBar.setLayoutParams(new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        Button reloadBtn = makePillButton("刷新", false);
        Button extBtn = makePillButton("浏览器打开", false);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.setMarginEnd(dp(6));
        reloadBtn.setLayoutParams(lp);

        reloadBtn.setOnClickListener(v -> webView.reload());
        extBtn.setOnClickListener(v -> {
            String u = webView.getUrl() != null ? webView.getUrl() : currentWebUrl;
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(u)));
        });

        nav.addView(webUrlBar);
        nav.addView(reloadBtn);
        nav.addView(extBtn);
        box.addView(nav);

        webView = new WebView(this);
        webView.setBackgroundColor(C_BG);
        webView.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                if (url != null) webUrlBar.setText(url);
            }
        });
        webView.setWebChromeClient(new WebChromeClient());
        box.addView(webView);
        return box;
    }

    private void showTab(boolean showNative) {
        nativePage.setVisibility(showNative ? View.VISIBLE : View.GONE);
        webPage.setVisibility(showNative ? View.GONE : View.VISIBLE);
        stylePillButton(tabNativeBtn, showNative);
        stylePillButton(tabWebBtn, !showNative);
    }

    private void openInWebTab(String url) {
        currentWebUrl = url;
        showTab(false);
        loadWeb(url);
    }

    private void loadWeb(String url) {
        webUrlBar.setText(url);
        webView.loadUrl(url);
    }

    private void runHostAction(String action, String hint) {
        logOutputText.setText(hint);
        executor.execute(() -> {
            ExecResult r = runSu(LINUX_SCRIPT + " " + action);
            mainHandler.post(() -> {
                logOutputText.setText(r.output.isEmpty() ? ("exit=" + r.exitCode) : r.output);
                toast(r.exitCode == 0 ? ("执行完成: " + action) : ("执行失败 (code=" + r.exitCode + ")"));
                refreshStatus();
            });
        });
    }

    private void toggleAutostart(boolean enable) {
        String cmd = enable
                ? ("chmod +x " + BOOT_SCRIPT + " && echo 'AUTOSTART_ON'")
                : ("chmod -x " + BOOT_SCRIPT + " && echo 'AUTOSTART_OFF'");
        executor.execute(() -> {
            ExecResult r = runSu(cmd);
            mainHandler.post(() -> {
                toast(enable ? "已开启开机自启" : "已关闭开机自启");
                logOutputText.setText(r.output);
            });
        });
    }

    private void refreshStatus() {
        executor.execute(() -> {
            String probe = LINUX_SCRIPT + " status; echo '---AUTOSTART---'; "
                    + "[ -x " + BOOT_SCRIPT + " ] && echo 'YES' || echo 'NO'";
            ExecResult r = runSu(probe);
            mainHandler.post(() -> applyStatusOutput(r));
        });
    }

    private void applyStatusOutput(ExecResult r) {
        if (r.exitCode != 0 && r.output.trim().isEmpty()) {
            statusBadge.setText("未获取 Root 权限");
            statusBadge.setTextColor(C_RED);
            logOutputText.setText("请在 Magisk 弹窗中授予 Root 权限后点刷新。");
            return;
        }

        String[] parts = r.output.split("---AUTOSTART---");
        String statusText = parts[0].trim();
        boolean autostartOn = parts.length > 1 && parts[1].trim().equals("YES");

        boolean mounted = statusText.contains("Mount: OK");
        boolean sshOk = statusText.contains("Service OK") && statusText.contains("sshd");

        if (mounted && sshOk) {
            statusBadge.setText("● 运行中");
            statusBadge.setTextColor(C_GREEN);
        } else if (mounted) {
            statusBadge.setText("● 部分运行");
            statusBadge.setTextColor(Color.parseColor("#fbbf24"));
        } else {
            statusBadge.setText("● 已停止");
            statusBadge.setTextColor(C_RED);
        }

        String foundIp = "未连接 Wi-Fi";
        String foundBat = "未知";
        StringBuilder svcLines = new StringBuilder();
        for (String line : statusText.split("\n")) {
            String trimmed = line.replace("[linux-ctl]", "").trim();
            if (trimmed.startsWith("Wi-Fi IP:")) {
                String raw = trimmed.substring("Wi-Fi IP:".length()).trim();
                foundIp = raw.contains("/") ? raw.substring(0, raw.indexOf('/')) : raw;
                if (!foundIp.isEmpty() && !foundIp.equals("N/A")) {
                    currentIp = foundIp;
                }
            } else if (trimmed.startsWith("Battery:")) {
                foundBat = trimmed.substring("Battery:".length()).trim();
            } else if (trimmed.startsWith("Service ")) {
                if (svcLines.length() > 0) svcLines.append('\n');
                svcLines.append(trimmed);
            }
        }

        ipValueText.setText("局域网 IP：" + foundIp + "（点此复制 SSH 地址）");
        batteryValueText.setText("电池状态：" + foundBat);
        servicesDetailText.setText(svcLines.length() > 0 ? svcLines.toString() : statusText);

        updatingSwitch = true;
        autostartSwitch.setChecked(autostartOn);
        updatingSwitch = false;
    }

    private ExecResult runSu(String command) {
        StringBuilder out = new StringBuilder();
        int code = -1;
        try {
            Process p = Runtime.getRuntime().exec("su");
            try (DataOutputStream os = new DataOutputStream(p.getOutputStream())) {
                os.write((command + "\nexit\n").getBytes(StandardCharsets.UTF_8));
                os.flush();
            }
            try (BufferedReader br = new BufferedReader(new InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8))) {
                String line;
                while ((line = br.readLine()) != null) {
                    out.append(line).append('\n');
                }
            }
            try (BufferedReader er = new BufferedReader(new InputStreamReader(p.getErrorStream(), StandardCharsets.UTF_8))) {
                String line;
                while ((line = er.readLine()) != null) {
                    out.append(line).append('\n');
                }
            }
            code = p.waitFor();
        } catch (Exception e) {
            out.append("Error: ").append(e.getMessage());
        }
        return new ExecResult(code, out.toString().trim());
    }

    @Override
    public void onBackPressed() {
        if (webPage.getVisibility() == View.VISIBLE && webView.canGoBack()) {
            webView.goBack();
            return;
        }
        if (webPage.getVisibility() == View.VISIBLE) {
            showTab(true);
            return;
        }
        super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        executor.shutdownNow();
    }

    // ---- UI Helpers ----

    private LinearLayout makeCard() {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(14), dp(14), dp(14), dp(14));
        card.setBackground(roundRect(C_CARD, C_BORDER, 14));
        return card;
    }

    private TextView makeSectionTitle(String text) {
        TextView tv = new TextView(this);
        tv.setText(text);
        tv.setTextColor(C_TEXT);
        tv.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        tv.setTypeface(null, Typeface.BOLD);
        return tv;
    }

    private TextView makeInfoLine(String text) {
        TextView tv = new TextView(this);
        tv.setText(text);
        tv.setTextColor(C_MUTED);
        tv.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        tv.setPadding(0, dp(6), 0, 0);
        return tv;
    }

    private Button makeActionButton(String label, int fill, int stroke) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        b.setTextColor(C_TEXT);
        b.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        b.setTypeface(null, Typeface.BOLD);
        b.setPadding(dp(10), dp(10), dp(10), dp(10));
        b.setMinHeight(0);
        b.setMinimumHeight(0);
        b.setBackground(roundRect(fill, stroke, 10));
        return b;
    }

    private Button makePillButton(String label, boolean active) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        b.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        b.setTypeface(null, Typeface.BOLD);
        b.setPadding(dp(12), dp(6), dp(12), dp(6));
        b.setMinHeight(0);
        b.setMinimumHeight(0);
        b.setMinWidth(0);
        b.setMinimumWidth(0);
        stylePillButton(b, active);
        return b;
    }

    private void stylePillButton(Button b, boolean active) {
        b.setTextColor(active ? Color.WHITE : C_MUTED);
        b.setBackground(roundRect(active ? C_ACCENT : C_CARD2, active ? C_ACCENT : C_BORDER, 9));
    }

    private GradientDrawable roundRect(int fill, int stroke, int radiusDp) {
        GradientDrawable gd = new GradientDrawable();
        gd.setColor(fill);
        gd.setCornerRadius(dp(radiusDp));
        gd.setStroke(dp(1), stroke);
        return gd;
    }

    private LinearLayout.LayoutParams weightLp(int startDp, int endDp) {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        lp.setMarginStart(dp(startDp));
        lp.setMarginEnd(dp(endDp));
        return lp;
    }

    private View withTopMargin(View v, int topDp) {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = dp(topDp);
        v.setLayoutParams(lp);
        return v;
    }

    private int dp(int value) {
        return Math.round(TypedValue.applyDimension(
                TypedValue.COMPLEX_UNIT_DIP, value, getResources().getDisplayMetrics()));
    }

    private void copyText(String label, String text) {
        ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
        if (cm != null) {
            cm.setPrimaryClip(ClipData.newPlainText(label, text));
            toast("已复制: " + text);
        }
    }

    private void toast(String msg) {
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show();
    }

    private static class ExecResult {
        final int exitCode;
        final String output;

        ExecResult(int exitCode, String output) {
            this.exitCode = exitCode;
            this.output = output;
        }
    }
}
