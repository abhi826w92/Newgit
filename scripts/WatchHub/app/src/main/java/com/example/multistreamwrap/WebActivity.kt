package com.example.multistreamwrap

import android.annotation.SuppressLint
import android.app.Activity
import android.app.Dialog
import android.content.Context
import android.content.Intent
import android.content.pm.ActivityInfo
import android.graphics.Bitmap
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.GestureDetector
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.webkit.CookieManager
import android.webkit.SslErrorHandler
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.FrameLayout
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.TextView
import android.widget.Toast
import android.util.Log
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.util.Collections
import org.json.JSONObject

class WebActivity : Activity() {

    companion object {
        const val EXTRA_SITE_NAME = "extra_site_name"
        const val EXTRA_SITE_URL = "extra_site_url"
        private const val AUTO_DISMISS_DELAY_MS = 8_000L
    }

    private lateinit var webView: WebView
    private lateinit var fullscreenContainer: FrameLayout
    private lateinit var btnRotate: ImageButton
    private lateinit var btnAdBlock: ImageButton
    private lateinit var btnDownload: ImageButton
    private lateinit var tvDownloadBadge: TextView
    private var downloadDialog: Dialog? = null

    private lateinit var gestureDetector: GestureDetector
    private lateinit var tutorialOverlay: LinearLayout
    private var tutorialTapCount = 0
    private var isVideoZoomed = false

    private var customView: View? = null
    private var customViewCallback: WebChromeClient.CustomViewCallback? = null
    private var originalOrientation = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
    private var isLandscape = false

    private lateinit var siteName: String
    private lateinit var siteUrl: String
    private var allowedHost: String? = null

    private val handler = Handler(Looper.getMainLooper())
    private var promptDialog: Dialog? = null
    private var loadingDialog: Dialog? = null

    private var vpnNotice: View? = null
    private var vpnTimerRunnable: Runnable? = null
    private var pageLoaded = false

    @Volatile
    private var currentTopUrl: String = ""
    @Volatile
    private var currentPageTitle: String = ""
    private val zephyrixResolvedSet = Collections.synchronizedSet(mutableSetOf<String>())

    override fun attachBaseContext(newBase: Context) {
        super.attachBaseContext(ThemeManager.wrapContext(newBase))
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        ThemeManager.applyTheme(this)
        super.onCreate(savedInstanceState)
        setContentView(layout("activity_web"))
        fixStatusBar()

        val root = findViewById<FrameLayout>(resId("root_layout"))
        val toolbar = findViewById<LinearLayout>(resId("toolbar"))
        root.setOnApplyWindowInsetsListener { _, insets ->
            val top = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                insets.getInsets(android.view.WindowInsets.Type.systemBars()).top
            } else {
                @Suppress("DEPRECATION")
                insets.systemWindowInsetTop
            }
            val bottom = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                insets.getInsets(android.view.WindowInsets.Type.systemBars()).bottom
            } else {
                @Suppress("DEPRECATION")
                insets.systemWindowInsetBottom
            }
            val left = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                insets.getInsets(android.view.WindowInsets.Type.systemBars()).left
            } else {
                @Suppress("DEPRECATION")
                insets.systemWindowInsetLeft
            }
            val right = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                insets.getInsets(android.view.WindowInsets.Type.systemBars()).right
            } else {
                @Suppress("DEPRECATION")
                insets.systemWindowInsetRight
            }

            val startPadding = (8 * resources.displayMetrics.density).toInt()
            toolbar.setPadding(startPadding, top, startPadding, 0)
            
            val innerLayout = root.getChildAt(0)
            innerLayout.setPadding(left, 0, right, bottom)
            
            insets
        }

        webView = findViewById(resId("web_view"))
        fullscreenContainer = findViewById(resId("fullscreen_container"))
        btnRotate = findViewById(resId("btn_rotate"))
        vpnNotice = findViewById(resId("vpn_notice"))
        startVpnTimer()

        siteName = intent.getStringExtra(EXTRA_SITE_NAME) ?: getString(stringRes("app_name"))
        val initialUrl = intent.getStringExtra(EXTRA_SITE_URL) ?: run {
            finish()
            return
        }
        siteUrl = SiteUrlManager.getResolvedUrl(this, siteName, initialUrl)
        currentTopUrl = siteUrl
        currentPageTitle = siteName
        tutorialOverlay = findViewById(resId("tutorial_overlay"))
        gestureDetector = GestureDetector(this, object : GestureDetector.SimpleOnGestureListener() {
            override fun onDoubleTap(e: MotionEvent): Boolean {
                if (fullscreenContainer.visibility == View.VISIBLE) {
                    toggleZoom()
                    if (tutorialOverlay.visibility == View.VISIBLE) {
                        tutorialOverlay.visibility = View.GONE
                        markTutorialSeen()
                    }
                    return true
                }
                return super.onDoubleTap(e)
            }
        })

        allowedHost = Uri.parse(siteUrl).host

        findViewById<TextView>(resId("toolbar_title")).text = siteName
        findViewById<ImageButton>(resId("btn_back")).setOnClickListener { finish() }

        btnAdBlock = findViewById(resId("btn_adblock"))
        updateAdBlockButtonState()
        btnAdBlock.setOnClickListener {
            val newState = AdBlockManager.toggleAdBlock(this)
            updateAdBlockButtonState()
            Toast.makeText(
                this,
                if (newState) "AdBlocker Enabled" else "AdBlocker Disabled",
                Toast.LENGTH_SHORT
            ).show()
            webView.reload()
        }

        btnRotate.setOnClickListener {
            isLandscape = !isLandscape
            requestedOrientation = if (isLandscape) {
                ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
            } else {
                ActivityInfo.SCREEN_ORIENTATION_SENSOR_PORTRAIT
            }
        }

        btnDownload = findViewById(resId("btn_download"))
        tvDownloadBadge = findViewById(resId("tv_download_badge"))
        val downloadClickListener = View.OnClickListener {
            if (VideoSniffer.count() > 0) {
                showDownloadDialog()
            } else {
                injectMediaDetector()
                webView.evaluateJavascript("""
                    (function() {
                        var found = [];
                        var vids = document.querySelectorAll('video, audio, iframe');
                        for (var i = 0; i < vids.length; i++) {
                            var v = vids[i];
                            var s = v.currentSrc || v.src;
                            if (s && !s.startsWith('blob:') && !s.startsWith('data:')) {
                                found.push(s);
                            }
                        }
                        return JSON.stringify(found);
                    })();
                """.trimIndent()) { result ->
                    try {
                        val arr = org.json.JSONArray(result)
                        for (i in 0 until arr.length()) {
                            val u = arr.getString(i)
                            VideoSniffer.inspectUrl(u, emptyMap(), currentTopUrl, currentPageTitle)
                        }
                    } catch (_: Exception) {}

                    if (VideoSniffer.count() > 0) {
                        showDownloadDialog()
                    } else {
                        Toast.makeText(this@WebActivity, "Scanning for video stream... Tap play on the video first!", Toast.LENGTH_SHORT).show()
                    }
                }
            }
        }
        btnDownload.setOnClickListener(downloadClickListener)
        findViewById<View>(resId("btn_download_container"))?.setOnClickListener(downloadClickListener)

        VideoSniffer.listener = { count ->
            updateDownloadBadge(count)
        }

        setupWebView()
        webView.loadUrl(siteUrl)

        tutorialOverlay.setOnClickListener {
            tutorialTapCount++
            if (tutorialTapCount >= 2) {
                tutorialOverlay.visibility = View.GONE
                markTutorialSeen()
            }
        }
    }

    private fun updateAdBlockButtonState() {
        val isEnabled = AdBlockManager.isAdBlockEnabled(this)
        if (isEnabled) {
            btnAdBlock.setColorFilter(Color.parseColor("#00E676")) // Vibrant green when active
            btnAdBlock.alpha = 1.0f
        } else {
            btnAdBlock.setColorFilter(Color.parseColor("#B0BEC5")) // Muted grey when disabled
            btnAdBlock.alpha = 0.5f
        }
    }

    private fun markTutorialSeen() {
        getSharedPreferences("app_prefs", Context.MODE_PRIVATE).edit().putBoolean("has_seen_zoom_tutorial", true).apply()
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView() {
        val settings: WebSettings = webView.settings
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        settings.setSupportMultipleWindows(false)
        settings.javaScriptCanOpenWindowsAutomatically = false
        settings.loadsImagesAutomatically = true
        settings.useWideViewPort = true
        settings.loadWithOverviewMode = true
        settings.mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
        settings.mediaPlaybackRequiresUserGesture = false
        settings.userAgentString = settings.userAgentString.replace("; wv", "") // Bypass basic bot checks

        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, true)

        webView.addJavascriptInterface(FullscreenJsBridge(), "AndroidFullscreen")
        webView.addJavascriptInterface(AndroidMediaBridge(), "AndroidMediaBridge")

        webView.webViewClient = object : WebViewClient() {
            override fun onReceivedSslError(view: WebView?, handler: SslErrorHandler?, error: android.net.http.SslError?) {
                handler?.proceed() // Ignore SSL errors for streaming sites
            }
            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                request?.url?.let { return handleNavigation(it.toString()) }
                return false
            }

            @Deprecated("Deprecated in Java")
            override fun shouldOverrideUrlLoading(view: WebView?, url: String?): Boolean {
                url?.let { return handleNavigation(it) }
                return false
            }

            override fun shouldInterceptRequest(view: WebView?, request: WebResourceRequest?): WebResourceResponse? {
                val url = request?.url?.toString() ?: return null
                val adResponse = interceptAd(url)
                if (adResponse != null) return adResponse

                try {
                    val headers = request.requestHeaders
                    val title = if (currentPageTitle.isNotBlank()) currentPageTitle else siteName
                    VideoSniffer.inspectUrl(url, headers, currentTopUrl, title)
                    resolveZephyrixStream(url, currentTopUrl, title)
                } catch (_: Throwable) {}
                return null
            }

            @Deprecated("Deprecated in Java")
            override fun shouldInterceptRequest(view: WebView?, url: String?): WebResourceResponse? {
                if (url.isNullOrEmpty()) return null
                val adResponse = interceptAd(url)
                if (adResponse != null) return adResponse

                try {
                    val title = if (currentPageTitle.isNotBlank()) currentPageTitle else siteName
                    VideoSniffer.inspectUrl(url, emptyMap(), currentTopUrl, title)
                    resolveZephyrixStream(url, currentTopUrl, title)
                } catch (_: Throwable) {}
                return null
            }

            override fun onPageStarted(view: WebView?, url: String?, favicon: Bitmap?) {
                super.onPageStarted(view, url, favicon)
                currentTopUrl = url ?: ""
                currentPageTitle = siteName
                pageLoaded = false
                zephyrixResolvedSet.clear()
                VideoSniffer.clear()
                updateDownloadBadge(0)
                hideVpnNotice()
                startVpnTimer()
                showLoadingDialog()
                injectAntiPopup()
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                super.onPageFinished(view, url)
                pageLoaded = true
                val title = view?.title
                if (!title.isNullOrBlank()) {
                    currentPageTitle = title
                }
                url?.let { finishedUrl ->
                    val finishedHost = try { Uri.parse(finishedUrl).host } catch (_: Exception) { null }
                    if (finishedHost != null && SiteUrlManager.isSameBrandOrMigration(allowedHost, finishedHost)) {
                        if (allowedHost != null && !allowedHost.equals(finishedHost, ignoreCase = true)) {
                            allowedHost = finishedHost
                            SiteUrlManager.saveResolvedUrl(this@WebActivity, siteName, finishedUrl)
                        }
                    }
                }
                hideVpnNotice()
                dismissLoadingDialog()
                injectAntiPopup()
                injectAdblockBypass()
                injectCosmeticFilters()
                injectFullscreenHelper()
                injectFullscreenLock()
                injectMediaDetector()
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onReceivedTitle(view: WebView?, title: String?) {
                super.onReceivedTitle(view, title)
                if (!title.isNullOrBlank()) {
                    currentPageTitle = title
                }
            }

            override fun onProgressChanged(view: WebView?, newProgress: Int) {
                if (newProgress in 25..35 || newProgress in 70..80) {
                    injectAntiPopup()
                    injectCosmeticFilters()
                    injectMediaDetector()
                }
            }

            override fun onCreateWindow(view: WebView?, isDialog: Boolean, isUserGesture: Boolean, resultMsg: android.os.Message?): Boolean {
                // Silently block new window / popup creations from ads
                return false
            }

            override fun onShowCustomView(view: View?, callback: CustomViewCallback?) {
                if (customView != null) {
                    callback?.onCustomViewHidden()
                    return
                }
                customView = view
                customViewCallback = callback
                originalOrientation = requestedOrientation

                fullscreenContainer.visibility = View.VISIBLE
                fullscreenContainer.addView(view, 0)
                fullscreenContainer.bringToFront()

                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                    window.attributes = window.attributes.apply {
                        layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES
                    }
                }

                val prefs = getSharedPreferences("app_prefs", Context.MODE_PRIVATE)
                if (!prefs.getBoolean("has_seen_zoom_tutorial", false)) {
                    tutorialOverlay.visibility = View.VISIBLE
                } else {
                    tutorialOverlay.visibility = View.GONE
                }

                // White line fix: Force the status/navigation bars and window background
                // to black inside fullscreen video to prevent any light strips from appearing.
                window.statusBarColor = Color.BLACK
                window.navigationBarColor = Color.BLACK
                window.decorView.setBackgroundColor(Color.BLACK)
                window.setBackgroundDrawable(android.graphics.drawable.ColorDrawable(Color.BLACK))

                requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
                webView.evaluateJavascript("window.__msFS = true;", null)
                hideSystemUI()
            }

            override fun onHideCustomView() {
                exitFullscreen()
            }
        }
    }

    private fun interceptAd(url: String): WebResourceResponse? {
        if (!AdBlockManager.isAdBlockEnabled(this)) return null

        val host = try { Uri.parse(url).host?.lowercase() } catch (_: Exception) { null } ?: return null
        val mainHost = allowedHost?.lowercase().orEmpty()

        // Never block assets from the site's own primary domain or its subdomains
        if (mainHost.isNotEmpty() && (host == mainHost || host.endsWith(".$mainHost"))) {
            return null
        }

        if (AdBlockStore.get(applicationContext).shouldBlock(url)) {
            val emptyStream = java.io.ByteArrayInputStream(ByteArray(0))
            return WebResourceResponse("text/plain", "utf-8", emptyStream)
        }
        return null
    }

    private fun handleNavigation(url: String): Boolean {
        if (url.isBlank()) return true
        val uri = try { Uri.parse(url) } catch (_: Exception) { return true }
        val host = uri.host?.lowercase().orEmpty()

        // 1. Silent Drop: If AdBlock is ON and it is a known ad/popup redirect, block it silently without opening any prompt!
        if (AdBlockManager.isAdBlockEnabled(this) && AdBlockStore.get(applicationContext).shouldBlock(url)) {
            return true
        }

        // 2. Allow login URLs inside app
        if (isLoginUrl(url)) {
            showLoginInAppToast()
            return false
        }

        // 3. Same-site navigation allowed
        val main = allowedHost?.lowercase().orEmpty()
        if (main.isNotEmpty() && (host == main || host.endsWith(".$main"))) return false
        if (host == "www.$main" || "www.$host" == main) return false

        // 3b. Smart Domain-Migration Auto-Detection:
        // Automatically allow domain redirects for the same site (e.g. nightflix.net -> nightflix.vg, cinezo.org -> cinezo.net)
        // without showing any external redirect prompt.
        if (SiteUrlManager.isSameBrandOrMigration(allowedHost, host)) {
            if (allowedHost != null && !allowedHost.equals(host, ignoreCase = true)) {
                allowedHost = host
                SiteUrlManager.saveResolvedUrl(this, siteName, url)
            }
            currentTopUrl = url
            return false
        }

        // 4. Block non-http schemes (market://, intent://, etc.) commonly used by rogue ad networks
        val scheme = uri.scheme?.lowercase().orEmpty()
        if (scheme != "http" && scheme != "https") {
            return true
        }

        // 5. Allow common embedded streaming player domains
        if (isVideoHost(host)) {
            return false
        }

        // 6. Only show redirect prompt for legitimate external links
        showRedirectPrompt(url)
        return true
    }

    private fun isVideoHost(host: String): Boolean {
        return host.contains("stream") || host.contains("video") || host.contains("embed") ||
               host.contains("player") || host.contains("m3u8") || host.contains("mp4upload") ||
               host.contains("filemoon") || host.contains("dood") || host.contains("vidcloud") ||
               host.contains("megacloud") || host.contains("rabbitstream") || host.contains("rapid-cloud")
    }

    private fun isLoginUrl(url: String): Boolean {
        val host = Uri.parse(url).host?.lowercase().orEmpty()
        val lower = url.lowercase()
        if (host == "accounts.google.com" || host.endsWith(".accounts.google.com")) return true
        if (host == "login.microsoftonline.com" || host.endsWith(".login.microsoftonline.com")) return true
        if (host == "login.live.com" || host == "login.yahoo.com" || host == "appleid.apple.com") return true
        if (lower.contains("/o/oauth2/")) return true
        if (lower.contains("oauth2/auth") || lower.contains("oauth2/authorize")) return true
        if (lower.contains("oauth2.googleapis.com")) return true
        if (lower.contains("/login/") || lower.contains("/signin/") || lower.contains("/signup/")) return true
        if (lower.contains("login?") || lower.contains("signin?") || lower.contains("signup?")) return true
        return false
    }

    private fun showLoginInAppToast() {
        runOnUiThread {
            Toast.makeText(this, "Sign in inside the app", Toast.LENGTH_SHORT).show()
        }
    }

    private fun showRedirectPrompt(url: String) {
        if (promptDialog?.isShowing == true) return
        val dialog = Dialog(this)
        dialog.setContentView(layout("dialog_redirect_prompt"))
        dialog.findViewById<TextView>(resId("prompt_url")).text = url
        dialog.findViewById<Button>(resId("btn_dismiss")).setOnClickListener { dialog.dismiss() }
        dialog.findViewById<Button>(resId("btn_open")).setOnClickListener {
            dialog.dismiss()
            val newHost = try { Uri.parse(url).host } catch (_: Exception) { null }
            if (newHost != null) {
                allowedHost = newHost
                currentTopUrl = url
                SiteUrlManager.saveResolvedUrl(this, siteName, url)
            }
            webView.loadUrl(url)
        }
        dialog.findViewById<Button>(resId("btn_open")).setOnLongClickListener {
            dialog.dismiss()
            openExternal(url)
            true
        }
        dialog.setOnDismissListener {
            handler.removeCallbacksAndMessages(null)
            promptDialog = null
        }
        dialog.show()
        promptDialog = dialog
        handler.postDelayed({ if (dialog.isShowing) dialog.dismiss() }, AUTO_DISMISS_DELAY_MS)
    }

    private fun openExternal(url: String) {
        try {
            startActivity(
                Intent(Intent.ACTION_VIEW, Uri.parse(url))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
        } catch (_: Exception) {}
    }

    private fun showLoadingDialog() {
        return
    }

    private fun startVpnTimer() {
        stopVpnTimer()
        vpnTimerRunnable = Runnable {
            if (!pageLoaded && !isFinishing && !isDestroyed) {
                showVpnNotice()
            }
        }
        handler.postDelayed(vpnTimerRunnable!!, 10_000L)
    }

    private fun stopVpnTimer() {
        vpnTimerRunnable?.let { handler.removeCallbacks(it) }
        vpnTimerRunnable = null
    }

    private fun showVpnNotice() {
        vpnNotice?.visibility = View.VISIBLE
    }

    private fun hideVpnNotice() {
        vpnNotice?.visibility = View.GONE
    }

    private fun dismissLoadingDialog() {
        loadingDialog?.let {
            if (it.isShowing) it.dismiss()
        }
        loadingDialog = null
    }

    private fun injectCosmeticFilters() {
        if (!AdBlockManager.isAdBlockEnabled(this)) return
        val selectors = CosmeticConfig.selectorsFor(allowedHost)
        if (selectors.isEmpty()) return
        val jsonSelectors = selectors.joinToString(", ") { "\"$it\"" }
        val script = """
            (function () {
                var sels = [$jsonSelectors];
                var cleaned = new WeakSet();
                function clean() {
                    sels.forEach(function (sel) {
                        try {
                            document.querySelectorAll(sel).forEach(function (el) {
                                if (!cleaned.has(el)) {
                                    el.style.setProperty('display', 'none', 'important');
                                    cleaned.add(el);
                                }
                            });
                        } catch (e) {}
                    });
                }
                clean();
                new MutationObserver(clean).observe(document.documentElement, { childList: true, subtree: true });
            })();
        """.trimIndent()
        webView.evaluateJavascript(script, null)
    }

    private fun injectAntiPopup() {
        if (!AdBlockManager.isAdBlockEnabled(this)) return
        val js = """
            (function() {
                try {
                    window.open = function() { return null; };
                    Object.defineProperty(window, 'open', {
                        value: function() { return null; },
                        writable: false,
                        configurable: false
                    });
                } catch(e) {}

                try {
                    window.alert = function() {};
                    window.confirm = function() { return false; };
                    window.prompt = function() { return null; };
                } catch(e) {}
            })();
        """.trimIndent()
        webView.evaluateJavascript(js, null)
    }

    private fun injectFullscreenHelper() {
        // Disabled aggressive fullscreen injection as it causes false positives
        // when users just want to click/pause the video.
    }

    private fun injectAdblockBypass() {
        if (!AdBlockManager.isAdBlockEnabled(this)) return
        val js = """
            (function() {
                'use strict';
                // 1. Google Ads stub (prevents broken page script loops)
                try {
                    window.adsbygoogle = window.adsbygoogle || [];
                    window.adsbygoogle.loaded = true;
                } catch(e) {}

                // 2. uBlock Origin popads-dummy defuser
                try {
                    delete window.PopAds;
                    delete window.popns;
                    Object.defineProperties(window, {
                        PopAds: { value: {} },
                        popns: { value: {} }
                    });
                } catch(e) {}

                // 3. uBlock Origin nofab (FuckAdBlock / BlockAdBlock / SniffAdBlock stub)
                try {
                    var noopfn = function() {};
                    var Fab = function() {};
                    Fab.prototype.check = noopfn;
                    Fab.prototype.clearEvent = noopfn;
                    Fab.prototype.emitEvent = noopfn;
                    Fab.prototype.on = function(a, b) { if (!a && b) { b(); } return this; };
                    Fab.prototype.onDetected = function() { return this; };
                    Fab.prototype.onNotDetected = function(a) { if (a) { a(); } return this; };
                    Fab.prototype.setOption = noopfn;
                    Fab.prototype.options = { set: noopfn, get: noopfn };
                    var fab = new Fab();
                    var getSetFab = { get: function() { return Fab; }, set: function() {} };
                    var getsetfab = { get: function() { return fab; }, set: function() {} };
                    if (window.hasOwnProperty('FuckAdBlock')) { window.FuckAdBlock = Fab; }
                    else { Object.defineProperty(window, 'FuckAdBlock', getSetFab); }
                    if (window.hasOwnProperty('BlockAdBlock')) { window.BlockAdBlock = Fab; }
                    else { Object.defineProperty(window, 'BlockAdBlock', getSetFab); }
                    if (window.hasOwnProperty('SniffAdBlock')) { window.SniffAdBlock = Fab; }
                    else { Object.defineProperty(window, 'SniffAdBlock', getSetFab); }
                    if (window.hasOwnProperty('fuckAdBlock')) { window.fuckAdBlock = fab; }
                    else { Object.defineProperty(window, 'fuckAdBlock', getsetfab); }
                    if (window.hasOwnProperty('blockAdBlock')) { window.blockAdBlock = fab; }
                    else { Object.defineProperty(window, 'blockAdBlock', getsetfab); }
                    if (window.hasOwnProperty('sniffAdBlock')) { window.sniffAdBlock = fab; }
                    else { Object.defineProperty(window, 'sniffAdBlock', getsetfab); }
                } catch(e) {}

                // 4. Modal anti-adblock element remover
                setInterval(function() {
                    try {
                        var els = document.querySelectorAll('div, section');
                        for (var i = 0; i < els.length; i++) {
                            var el = els[i];
                            if (el.style.zIndex && parseInt(el.style.zIndex) > 100 && el.textContent) {
                                var text = el.textContent.toLowerCase();
                                if ((text.includes('adblock') || text.includes('ad blocker')) && 
                                    (text.includes('disable') || text.includes('turn off') || text.includes('detected'))) {
                                    el.style.setProperty('display', 'none', 'important');
                                }
                            }
                        }
                        if (document.body && document.body.style.overflow === 'hidden') {
                            document.body.style.setProperty('overflow', 'auto', 'important');
                        }
                    } catch(e) {}
                }, 1000);
            })();
        """.trimIndent()
        webView.evaluateJavascript(js, null)
    }

    private fun injectFullscreenLock() {
        val js = """
            (function() {
                if (window.__msFS !== undefined) return;
                window.__msFS = false;

                function patch(scope, name) {
                    var orig = scope[name];
                    if (!orig) return;
                    scope[name] = function() {
                        if (window.__msFS) return;
                        return orig.apply(this, arguments);
                    };
                }

                if (window.Document) {
                    patch(Document.prototype, 'exitFullscreen');
                    patch(Document.prototype, 'webkitExitFullscreen');
                    patch(Document.prototype, 'webkitCancelFullScreen');
                }
                if (document.exitFullscreen) patch(document, 'exitFullscreen');
                if (document.webkitExitFullscreen) patch(document, 'webkitExitFullscreen');
                if (document.webkitCancelFullScreen) patch(document, 'webkitCancelFullScreen');
                if (window.HTMLMediaElement) {
                    patch(HTMLMediaElement.prototype, 'webkitExitFullscreen');
                }

                function sync() {
                    window.__msFS = !!(document.fullscreenElement || document.webkitFullscreenElement);
                }
                document.addEventListener('fullscreenchange', sync);
                document.addEventListener('webkitfullscreenchange', sync);
                sync();

                setInterval(function() {
                    if (!window.__msFS || !window.AndroidFullscreen) return;
                    var v = document.querySelector('video');
                    if (v && v.ended) window.AndroidFullscreen.onVideoEnded();
                }, 2000);
            })();
        """.trimIndent()
        webView.evaluateJavascript(js, null)
    }

    private fun hideSystemUI() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            window.insetsController?.let {
                it.hide(android.view.WindowInsets.Type.statusBars() or android.view.WindowInsets.Type.navigationBars())
                it.systemBarsBehavior = android.view.WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            }
        } else {
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility = (
                View.SYSTEM_UI_FLAG_FULLSCREEN
                    or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                    or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            )
        }
    }

    private fun showSystemUI() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            window.insetsController?.show(
                android.view.WindowInsets.Type.statusBars() or android.view.WindowInsets.Type.navigationBars()
            )
        } else {
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_VISIBLE
        }
    }

    private fun exitFullscreen() {
        if (customView == null) return
        webView.evaluateJavascript("window.__msFS = false;", null)
        fullscreenContainer.removeView(customView)
        isVideoZoomed = false
        customView?.scaleX = 1f
        customView?.scaleY = 1f
        tutorialOverlay.visibility = View.GONE
        
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            window.attributes = window.attributes.apply {
                layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_DEFAULT
            }
        }
        
        fullscreenContainer.visibility = View.GONE
        
        customView = null
        customViewCallback?.onCustomViewHidden()
        customViewCallback = null
        requestedOrientation = originalOrientation
        showSystemUI()
        fixStatusBar()
    }

    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        if (customView != null) {
            exitFullscreen()
        } else if (webView.canGoBack()) {
            webView.goBack()
        } else {
            super.onBackPressed()
        }
    }

    override fun onResume() {
        super.onResume()
        updateAdBlockButtonState()
    }

    override fun onDestroy() {
        VideoSniffer.listener = null
        VideoSniffer.clear()
        downloadDialog?.dismiss()
        downloadDialog = null
        handler.removeCallbacksAndMessages(null)
        stopVpnTimer()
        dismissLoadingDialog()
        webView.destroy()
        super.onDestroy()
    }

    @Suppress("DEPRECATION")
    private fun fixStatusBar() {
        window.addFlags(WindowManager.LayoutParams.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS)
        window.statusBarColor = Color.parseColor("#5E35B1")
        window.navigationBarColor = Color.parseColor("#5E35B1")
        val bg = android.graphics.drawable.ColorDrawable(resources.getColor(colorRes("bg_window")))
        window.decorView.setBackgroundDrawable(bg)
        window.setBackgroundDrawable(bg)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            window.decorView.systemUiVisibility = 0
        }
    }

    private var isConsumingDoubleTap = false

    override fun dispatchTouchEvent(ev: MotionEvent): Boolean {
        if (fullscreenContainer.visibility == View.VISIBLE) {
            val action = ev.actionMasked
            val consumedByDetector = gestureDetector.onTouchEvent(ev)

            if (action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_CANCEL) {
                if (isConsumingDoubleTap) {
                    isConsumingDoubleTap = false
                    return true // Swallow the UP/CANCEL event of the double tap
                }
            }

            if (consumedByDetector) {
                if (action == MotionEvent.ACTION_DOWN) {
                    isConsumingDoubleTap = true
                    val cancelEvent = MotionEvent.obtain(ev)
                    cancelEvent.action = MotionEvent.ACTION_CANCEL
                    super.dispatchTouchEvent(cancelEvent)
                    cancelEvent.recycle()
                }
                return true
            }

            if (isConsumingDoubleTap) {
                return true // Swallow MOVE events of the double tap
            }
        }
        return super.dispatchTouchEvent(ev)
    }

    private fun toggleZoom() {
        if (customView == null) return
        isVideoZoomed = !isVideoZoomed
        if (isVideoZoomed) {
            val screenW = resources.displayMetrics.widthPixels.toFloat()
            val screenH = resources.displayMetrics.heightPixels.toFloat()
            val screenRatio = maxOf(screenW, screenH) / minOf(screenW, screenH)
            val videoRatio = 16f / 9f
            val scale = if (screenRatio > videoRatio) screenRatio / videoRatio else videoRatio / screenRatio
            customView?.scaleX = scale
            customView?.scaleY = scale
        } else {
            customView?.scaleX = 1f
            customView?.scaleY = 1f
        }
    }

    private fun updateDownloadBadge(count: Int) {
        if (count > 0) {
            btnDownload.alpha = 1.0f
            tvDownloadBadge.visibility = View.VISIBLE
            tvDownloadBadge.text = if (count > 9) "9+" else count.toString()
        } else {
            btnDownload.alpha = 0.4f
            tvDownloadBadge.visibility = View.GONE
        }
    }

    private fun dpToPx(dp: Int): Int = (dp * resources.displayMetrics.density).toInt()

    private fun showDownloadDialog() {
        val detected = VideoSniffer.getDetectedMedia()
        if (detected.isEmpty()) {
            Toast.makeText(this, "No video stream found yet. Start playing a video first!", Toast.LENGTH_SHORT).show()
            return
        }

        downloadDialog?.dismiss()
        val validDetected = detected.filter { !it.url.contains(".txt", ignoreCase = true) }
        val media = validDetected.lastOrNull { it.url.contains(".m3u8", ignoreCase = true) }
            ?: validDetected.lastOrNull { it.type.contains("HLS", ignoreCase = true) }
            ?: validDetected.lastOrNull()
            ?: detected.last()

        val dialog = Dialog(this)
        dialog.setContentView(layout("dialog_download_picker"))
        dialog.window?.setBackgroundDrawableResource(android.R.color.transparent)

        val tvTitle = dialog.findViewById<TextView>(resId("tv_video_title"))
        val tvType = dialog.findViewById<TextView>(resId("tv_stream_type"))
        val tvQualityLabel = dialog.findViewById<TextView>(resId("tv_quality_label"))
        val rgQualities = dialog.findViewById<RadioGroup>(resId("rg_qualities"))
        val btnStart = dialog.findViewById<Button>(resId("btn_start_download"))
        val btnViewDownloads = dialog.findViewById<Button>(resId("btn_view_downloads"))
        val btnClose = dialog.findViewById<ImageButton>(resId("btn_close_dialog"))

        tvTitle.text = media.title
        tvType.text = media.type

        var selectedStreamUrl = media.url

        if (media.type.contains("HLS", ignoreCase = true) || media.url.contains(".m3u8", ignoreCase = true)) {
            tvQualityLabel.visibility = View.VISIBLE
            rgQualities.visibility = View.VISIBLE

            Thread {
                val qualities = HlsDownloader(this@WebActivity).parseQualities(media.url, media.headers)
                runOnUiThread {
                    rgQualities.removeAllViews()
                    for (i in qualities.indices) {
                        val q = qualities[i]
                        val rb = RadioButton(this).apply {
                            text = q.displayLabel
                            id = View.generateViewId()
                            setTextColor(Color.parseColor("#E0E0E0"))
                            textSize = 14f
                            setPadding(dpToPx(6), dpToPx(6), dpToPx(6), dpToPx(6))
                            tag = q.url
                        }
                        rgQualities.addView(rb)
                        if (i == 0) {
                            rb.isChecked = true
                            selectedStreamUrl = q.url
                        }
                    }

                    rgQualities.setOnCheckedChangeListener { group, checkedId ->
                        val checkedRb = group.findViewById<RadioButton>(checkedId)
                        val url = checkedRb?.tag as? String
                        if (url != null) {
                            selectedStreamUrl = url
                        }
                    }
                }
            }.start()
        } else {
            tvQualityLabel.visibility = View.GONE
            rgQualities.visibility = View.GONE
        }

        btnStart.setOnClickListener {
            dialog.dismiss()
            DownloadService.startDownload(
                context = this,
                title = media.title,
                streamUrl = selectedStreamUrl,
                headers = media.headers
            )
            Toast.makeText(this, "Download started in background!", Toast.LENGTH_SHORT).show()
        }

        btnViewDownloads.setOnClickListener {
            dialog.dismiss()
            startActivity(Intent(this, DownloadsActivity::class.java))
        }

        btnClose?.setOnClickListener {
            dialog.dismiss()
        }

        dialog.show()

        // Responsive dialog layout width (92% screen width, capped at 420dp)
        val dialogWidth = (resources.displayMetrics.widthPixels * 0.92f).toInt().coerceAtMost(dpToPx(420))
        dialog.window?.setLayout(dialogWidth, WindowManager.LayoutParams.WRAP_CONTENT)
        dialog.window?.setGravity(Gravity.CENTER)

        downloadDialog = dialog
    }

    private fun resolveZephyrixStream(url: String, pageUrl: String, title: String) {
        if (!url.contains("play.zephyrix.org/video/") && !url.contains("play.zephyrix.org/embed/")) {
            return
        }
        val match = Regex("""play\.zephyrix\.org/(?:video|embed)/([a-zA-Z0-9_-]+)""").find(url) ?: return
        val videoId = match.groupValues[1]
        if (!zephyrixResolvedSet.add(videoId)) return

        Thread {
            try {
                val apiUrl = "https://play.zephyrix.org/player/index.php?data=$videoId&do=getVideo"
                val conn = (URL(apiUrl).openConnection() as HttpURLConnection).apply {
                    requestMethod = "POST"
                    connectTimeout = 10000
                    readTimeout = 10000
                    doOutput = true
                    setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")
                    setRequestProperty("Referer", url)
                    setRequestProperty("X-Requested-With", "XMLHttpRequest")
                    setRequestProperty("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
                }
                val postData = "hash=$videoId&r=" + URLEncoder.encode(pageUrl, "UTF-8")
                conn.outputStream.use { it.write(postData.toByteArray(Charsets.UTF_8)) }
                if (conn.responseCode == 200) {
                    val resp = conn.inputStream.bufferedReader().readText()
                    val json = JSONObject(resp)
                    val securedLink = json.optString("securedLink")
                    val videoSource = json.optString("videoSource")
                    val candidate = if (securedLink.isNotBlank() && !securedLink.contains(".txt", ignoreCase = true)) {
                        securedLink
                    } else if (videoSource.isNotBlank() && !videoSource.contains(".txt", ignoreCase = true)) {
                        videoSource
                    } else {
                        ""
                    }
                    val finalStream = if (candidate.startsWith("//")) "https:$candidate" else candidate
                    if (finalStream.isNotBlank()) {
                        val sniffHeaders = mapOf(
                            "Referer" to "https://play.zephyrix.org/",
                            "Origin" to "https://play.zephyrix.org"
                        )
                        VideoSniffer.inspectUrl(finalStream, sniffHeaders, pageUrl, title)
                    }
                }
            } catch (e: Exception) {
                Log.w("WebActivity", "Failed to resolve Zephyrix stream for $videoId", e)
            }
        }.start()
    }

    private fun injectMediaDetector() {
        val js = """
            (function() {
                if (window.__msMediaHook) return;
                window.__msMediaHook = true;

                function report(url) {
                    if (!url || typeof url !== 'string' || url.length < 5) return;
                    if (url.startsWith('blob:') || url.startsWith('data:') || url.startsWith('javascript:')) return;
                    if (window.AndroidMediaBridge && window.AndroidMediaBridge.onMediaFound) {
                        window.AndroidMediaBridge.onMediaFound(url, document.title || '');
                    }
                }

                // 1. Hook URL.createObjectURL to catch in-memory decrypted HLS/M3U8 blobs (e.g. Kartoons.me)
                try {
                    var origCreateObjectURL = URL.createObjectURL;
                    URL.createObjectURL = function(blob) {
                        try {
                            if (blob && blob.type && (blob.type.includes('mpegurl') || blob.type.includes('m3u8') || blob.type.includes('video'))) {
                                var reader = new FileReader();
                                reader.onload = function() {
                                    var text = reader.result;
                                    if (text && typeof text === 'string' && (text.includes('#EXTM3U') || text.includes('#EXTINF'))) {
                                        if (window.AndroidMediaBridge && window.AndroidMediaBridge.onPlaylistContentFound) {
                                            window.AndroidMediaBridge.onPlaylistContentFound(text, document.title || '', window.location.href);
                                        }
                                    }
                                };
                                reader.readAsText(blob);
                            }
                        } catch(e) {}
                        return origCreateObjectURL.apply(this, arguments);
                    };
                } catch(e) {}

                // 2. Hook window.fetch
                try {
                    var origFetch = window.fetch;
                    window.fetch = function(input, init) {
                        try {
                            var u = (typeof input === 'string') ? input : (input && input.url ? input.url : '');
                            if (u && typeof u === 'string') {
                                report(u);
                            }
                        } catch(e) {}
                        return origFetch.apply(this, arguments);
                    };
                } catch(e) {}

                // 3. Hook XMLHttpRequest.prototype.open
                try {
                    var origXhr = XMLHttpRequest.prototype.open;
                    XMLHttpRequest.prototype.open = function(method, url) {
                        try {
                            if (url && typeof url === 'string') {
                                report(url);
                            }
                        } catch(e) {}
                        return origXhr.apply(this, arguments);
                    };
                } catch(e) {}

                // 4. Hook HTMLMediaElement.prototype.play and src property changes
                try {
                    var origPlay = HTMLMediaElement.prototype.play;
                    HTMLMediaElement.prototype.play = function() {
                        var src = this.currentSrc || this.src;
                        if (src) report(src);
                        return origPlay.apply(this, arguments);
                    };
                } catch(e) {}

                // 5. Scan all existing video and source tags in DOM
                function scanVideos() {
                    var videos = document.getElementsByTagName('video');
                    for (var i = 0; i < videos.length; i++) {
                        var v = videos[i];
                        var src = v.currentSrc || v.src;
                        if (src) report(src);
                        var sources = v.getElementsByTagName('source');
                        for (var j = 0; j < sources.length; j++) {
                            if (sources[j].src) report(sources[j].src);
                        }
                    }
                }
                scanVideos();
                var obs = new MutationObserver(function() { scanVideos(); });
                if (document.body) {
                    obs.observe(document.body, { childList: true, subtree: true });
                }
            })();
        """.trimIndent()
        webView.evaluateJavascript(js, null)
    }

    private inner class AndroidMediaBridge {
        @android.webkit.JavascriptInterface
        fun onMediaFound(streamUrl: String?, title: String?) {
            if (!streamUrl.isNullOrBlank()) {
                runOnUiThread {
                    val pageTitle = if (currentPageTitle.isNotBlank()) currentPageTitle else siteName
                    VideoSniffer.inspectUrl(
                        url = streamUrl,
                        headers = emptyMap(),
                        pageUrl = currentTopUrl,
                        pageTitle = pageTitle
                    )
                }
            }
        }

        @android.webkit.JavascriptInterface
        fun onPlaylistContentFound(playlistContent: String?, title: String?, pageUrl: String?) {
            if (!playlistContent.isNullOrBlank() && (playlistContent.contains("#EXTM3U") || playlistContent.contains("#EXTINF"))) {
                runOnUiThread {
                    try {
                        val pageTitle = if (currentPageTitle.isNotBlank()) currentPageTitle else siteName
                        val decrypted = if (playlistContent.contains("enc2:")) {
                            KartoonsEnc2.decryptM3u8(playlistContent)
                        } else {
                            playlistContent
                        }
                        val tempM3u8 = File(cacheDir, "stream_${System.currentTimeMillis()}.m3u8")
                        tempM3u8.writeText(decrypted)
                        val fileUrl = "file://${tempM3u8.absolutePath}"
                        val detected = DetectedMedia(
                            url = fileUrl,
                            referer = pageUrl ?: currentTopUrl,
                            headers = emptyMap(),
                            type = "HLS Stream",
                            title = pageTitle
                        )
                        VideoSniffer.registerMedia(detected)
                    } catch (e: Exception) {
                        Log.e("WebActivity", "Error saving playlist blob content", e)
                    }
                }
            }
        }
    }

    private inner class FullscreenJsBridge {
        @android.webkit.JavascriptInterface
        fun onVideoEnded() {
            runOnUiThread {
                if (customView != null) {
                    exitFullscreen()
                }
            }
        }
    }
}