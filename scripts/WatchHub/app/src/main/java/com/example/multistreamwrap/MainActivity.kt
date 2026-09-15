package com.example.multistreamwrap

import android.app.Activity
import android.app.Dialog
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.graphics.Color
import android.graphics.Typeface
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.Editable
import android.text.TextWatcher
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.EditText
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.ProgressBar
import android.widget.TextView

class MainActivity : Activity() {

    private var loadingDialog: Dialog? = null
    private val handler = Handler(Looper.getMainLooper())

    private val adapter = SiteAdapter(
        this,
        onSiteClick = { site -> handleSiteClick(site) },
        onSiteLongClick = { site -> handleSiteLongClick(site) }
    )

    private var isSidebarOpen = false
    private lateinit var sidebarMenu: View
    private lateinit var dimOverlay: View
    private var adblockText: TextView? = null
    private var categoryContainer: LinearLayout? = null
    private var currentSearchQuery = ""
    private var currentSelectedCategory = "All"

    override fun attachBaseContext(newBase: Context) {
        super.attachBaseContext(ThemeManager.wrapContext(newBase))
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        ThemeManager.applyTheme(this)
        super.onCreate(savedInstanceState)
        setContentView(layout("activity_main"))
        fixStatusBar()
        checkAppLock()
        SiteUrlManager.cleanStaleOverrides(this)

        sidebarMenu = findViewById(resId("sidebar_menu"))
        dimOverlay = findViewById(resId("sidebar_dim_overlay"))

        findViewById<ImageButton>(resId("btn_menu")).setOnClickListener {
            toggleSidebar()
        }
        dimOverlay.setOnClickListener {
            if (isSidebarOpen) toggleSidebar()
        }

        findViewById<View>(resId("menu_favorites")).setOnClickListener { 
            startActivity(Intent(this, FavoritesActivity::class.java))
            toggleSidebar()
        }
        findViewById<View>(resId("menu_history")).setOnClickListener { 
            startActivity(Intent(this, HistoryActivity::class.java))
            toggleSidebar()
        }
        findViewById<View>(resId("menu_downloads")).setOnClickListener { 
            startActivity(Intent(this, DownloadsActivity::class.java))
            toggleSidebar()
        }
        
        val lockText = findViewById<TextView>(resId("menu_app_lock"))
        lockText.text = if (AppLockManager.isAppLockEnabled(this)) "App Lock: ON" else "App Lock: OFF"
        lockText.setOnClickListener {
            if (!AppLockManager.isDeviceSecure(this)) {
                android.widget.Toast.makeText(this, "Set a lock screen PIN/Fingerprint first", android.widget.Toast.LENGTH_LONG).show()
                return@setOnClickListener
            }
            val newState = !AppLockManager.isAppLockEnabled(this)
            AppLockManager.setAppLockEnabled(this, newState)
            lockText.text = if (newState) "App Lock: ON" else "App Lock: OFF"
            android.widget.Toast.makeText(this, if (newState) "App Lock Enabled" else "App Lock Disabled", android.widget.Toast.LENGTH_SHORT).show()
        }

        val abText = findViewById<TextView>(resId("menu_adblock"))
        adblockText = abText
        abText.text = if (AdBlockManager.isAdBlockEnabled(this)) "AdBlocker: ON" else "AdBlocker: OFF"
        abText.setOnClickListener {
            val newState = AdBlockManager.toggleAdBlock(this)
            abText.text = if (newState) "AdBlocker: ON" else "AdBlocker: OFF"
            android.widget.Toast.makeText(this, if (newState) "AdBlocker Enabled" else "AdBlocker Disabled", android.widget.Toast.LENGTH_SHORT).show()
        }
        findViewById<View>(resId("menu_contact")).setOnClickListener { openUrl(contactUrl()) }
        findViewById<View>(resId("menu_community")).setOnClickListener { openUrl(communityUrl()) }
        findViewById<View>(resId("menu_instagram")).setOnClickListener { openUrl(instagramUrl()) }
        findViewById<View>(resId("menu_github")).setOnClickListener { openUrl(githubUrl()) }
        findViewById<View>(resId("menu_portfolio")).setOnClickListener { openUrl(portfolioUrl()) }

        val root = findViewById<View>(resId("root_layout_container"))
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

            toolbar.setPadding(toolbar.paddingLeft, top, toolbar.paddingRight, toolbar.paddingBottom)
            findViewById<View>(resId("root_layout")).setPadding(left, 0, right, bottom)
            
            insets
        }

        val list = findViewById<ListView>(resId("site_list"))
        list.adapter = adapter

        categoryContainer = findViewById(resId("category_container"))
        refreshCategoryChips()
        refreshMainList()

        DialogLoader.startMonitoring(this)

        UpdateManager.checkForUpdates(this)

        findViewById<EditText>(resId("search_input")).addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {
                currentSearchQuery = s?.toString() ?: ""
                refreshMainList()
            }
            override fun afterTextChanged(s: Editable?) {}
        })

        findViewById<ImageButton>(resId("btn_theme")).setOnClickListener {
            ThemeManager.toggleTheme(this)
            recreate()
        }
        
        showTelegramPopupIfFirstTime()

        handler.postDelayed({
            if (!isFinishing && !isDestroyed) requestAllPermissions()
        }, 500)
    }

    private fun requestAllPermissions() {
        if (Build.VERSION.SDK_INT < 23) return
        val needed = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= 33) {
            addIfNeeded(needed, "android.permission.POST_NOTIFICATIONS")
            addIfNeeded(needed, "android.permission.READ_MEDIA_IMAGES")
            addIfNeeded(needed, "android.permission.READ_MEDIA_VIDEO")
            addIfNeeded(needed, "android.permission.READ_MEDIA_AUDIO")
        } else {
            addIfNeeded(needed, "android.permission.READ_EXTERNAL_STORAGE")
            addIfNeeded(needed, "android.permission.WRITE_EXTERNAL_STORAGE")
        }
        if (needed.isNotEmpty()) {
            try {
                requestPermissions(needed.toTypedArray(), 1001)
            } catch (_: Exception) {}
        }
    }

    private fun addIfNeeded(list: MutableList<String>, permission: String) {
        try {
            if (checkSelfPermission(permission) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                list.add(permission)
            }
        } catch (_: Exception) {}
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
    }

    private fun showTelegramPopupIfFirstTime() {
        val prefs = getSharedPreferences("app_prefs", Context.MODE_PRIVATE)
        if (prefs.getBoolean("has_seen_tg_popup", false)) return
        handler.postDelayed({
            if (isFinishing || isDestroyed) return@postDelayed
            if (prefs.getBoolean("has_seen_tg_popup", false)) return@postDelayed
            val dialog = Dialog(this)
            dialog.requestWindowFeature(android.view.Window.FEATURE_NO_TITLE)
            dialog.setContentView(resources.getIdentifier("dialog_telegram", "layout", packageName))

            dialog.findViewById<View>(resId("btn_tg_join")).setOnClickListener {
                prefs.edit().putBoolean("has_seen_tg_popup", true).apply()
                dialog.dismiss()
                val url = communityUrl()
                if (url.isNotEmpty()) {
                    try {
                        startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
                    } catch (_: Exception) {}
                }
            }

            dialog.findViewById<View>(resId("btn_tg_later")).setOnClickListener {
                prefs.edit().putBoolean("has_seen_tg_popup", true).apply()
                dialog.dismiss()
            }

            dialog.window?.apply {
                setBackgroundDrawableResource(android.R.color.transparent)
                setLayout(WindowManager.LayoutParams.MATCH_PARENT, WindowManager.LayoutParams.WRAP_CONTENT)
                setGravity(android.view.Gravity.BOTTOM)
            }
            dialog.show()
        }, 600)
    }

    override fun onResume() {
        super.onResume()
        dismissLoadingDialog()
        adblockText?.text = if (AdBlockManager.isAdBlockEnabled(this)) "AdBlocker: ON" else "AdBlocker: OFF"
        adapter.notifyDataSetChanged()
    }

    private fun showLoadingDialog() {
        if (loadingDialog?.isShowing == true) return
        val dialog = Dialog(this)
        dialog.requestWindowFeature(android.view.Window.FEATURE_NO_TITLE)
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(64, 48, 64, 48)
            setBackgroundColor(resources.getColor(colorRes("dialog_bg")))
            gravity = Gravity.CENTER
        }
        val progressBar = ProgressBar(this).apply {
            isIndeterminate = true
            indeterminateTintList = android.content.res.ColorStateList.valueOf(resources.getColor(colorRes("primary")))
        }
        val textView = TextView(this).apply {
            text = "Loading..."
            setTextColor(resources.getColor(colorRes("text_primary")))
            textSize = 16f
            setPadding(0, 24, 0, 0)
            gravity = Gravity.CENTER
        }
        layout.addView(progressBar)
        layout.addView(textView)
        dialog.setContentView(layout)
        dialog.setCancelable(false)
        dialog.window?.setBackgroundDrawableResource(android.R.color.transparent)
        loadingDialog = dialog
        dialog.show()
    }

    private fun handleSiteClick(site: Site) {
        if (SiteLockManager.isSiteLocked(this, site.name)) {
            SiteLockManager.authenticate(this, "Unlock ${site.name}", onSuccess = {
                launchSite(site)
            })
        } else {
            launchSite(site)
        }
    }

    private fun launchSite(site: Site) {
        val targetUrl = SiteUrlManager.getResolvedUrl(this, site.name, site.url)
        HistoryManager.addSite(this, site.name, targetUrl)
        showLoadingDialog()
        val intent = Intent(this, WebActivity::class.java)
            .putExtra(WebActivity.EXTRA_SITE_NAME, site.name)
            .putExtra(WebActivity.EXTRA_SITE_URL, targetUrl)
        startActivity(intent)
    }

    private fun buildFlatList(): List<Any> {
        val result = mutableListOf<Any>()
        val query = currentSearchQuery.trim()
        
        for (cat in SitesProvider.categories) {
            if (currentSelectedCategory != "All" && currentSelectedCategory != cat.name) continue
            
            if (query.isEmpty()) {
                result.add(cat.name)
                result.addAll(cat.sites)
            } else {
                val matchingSites = cat.sites.filter {
                    it.name.contains(query, ignoreCase = true) || it.domain.contains(query, ignoreCase = true)
                }
                if (matchingSites.isNotEmpty()) {
                    result.add(cat.name)
                    result.addAll(matchingSites)
                }
            }
        }
        return result
    }

    private fun refreshMainList() {
        adapter.submit(buildFlatList())
    }

    private fun refreshCategoryChips() {
        val container = categoryContainer ?: return
        container.removeAllViews()
        val categories = mutableListOf("All")
        categories.addAll(SitesProvider.categories.map { it.name })

        for (catName in categories) {
            val tv = TextView(this).apply {
                text = catName
                textSize = 14f
                setPadding(40, 16, 40, 16)
                val bg = android.graphics.drawable.GradientDrawable().apply {
                    cornerRadius = 50f
                    if (currentSelectedCategory == catName) {
                        setColor(resources.getColor(colorRes("primary")))
                        setStroke(0, Color.TRANSPARENT)
                    } else {
                        setColor(resources.getColor(colorRes("bg_search")))
                        setStroke(2, resources.getColor(colorRes("text_hint")))
                    }
                }
                background = bg
                setTextColor(if (currentSelectedCategory == catName) Color.WHITE else resources.getColor(colorRes("text_primary")))
                layoutParams = LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT
                ).apply {
                    setMargins(0, 0, 24, 0)
                }
                setOnClickListener {
                    if (currentSelectedCategory != catName) {
                        currentSelectedCategory = catName
                        refreshCategoryChips()
                        refreshMainList()
                    }
                }
            }
            container.addView(tv)
        }
    }

    private fun handleSiteLongClick(site: Site) {
        val catName = SiteLockManager.getCategoryForSite(site.name)
        if (SiteLockManager.isAutoLockedCategory(catName)) {
            android.widget.Toast.makeText(this, "Category '$catName' is protected by Biometrics", android.widget.Toast.LENGTH_SHORT).show()
            return
        }

        val isLocked = SiteLockManager.isCustomLocked(this, site.name)
        val actionText = if (isLocked) "Unlock ${site.name}" else "Lock ${site.name}"
        
        val dialog = android.app.AlertDialog.Builder(this)
            .setTitle(site.name)
            .setMessage("Do you want to ${actionText.lowercase()}?")
            .setPositiveButton(if (isLocked) "Unlock" else "Lock") { _, _ ->
                SiteLockManager.authenticate(this, actionText, onSuccess = {
                    if (isLocked) {
                        SiteLockManager.unlockSite(this, site.name)
                        android.widget.Toast.makeText(this, "${site.name} Unlocked", android.widget.Toast.LENGTH_SHORT).show()
                    } else {
                        SiteLockManager.lockSite(this, site.name)
                        android.widget.Toast.makeText(this, "${site.name} Locked", android.widget.Toast.LENGTH_SHORT).show()
                    }
                    adapter.notifyDataSetChanged()
                })
            }
            .setNegativeButton("Cancel", null)
            .create()
        dialog.show()
    }

    private fun dismissLoadingDialog() {
        loadingDialog?.let {
            if (it.isShowing) it.dismiss()
        }
        loadingDialog = null
    }

    private val AUTH_REQUEST_CODE = 999
    
    @Suppress("DEPRECATION")
    private fun checkAppLock() {
        if (AppLockManager.isAppLockEnabled(this) && !AppLockManager.isUnlockedSession) {
            findViewById<View>(resId("root_layout_container")).visibility = View.INVISIBLE
            val km = getSystemService(Context.KEYGUARD_SERVICE) as android.app.KeyguardManager
            val intent = km.createConfirmDeviceCredentialIntent("Unlock App", "Please confirm your identity to open the app")
            if (intent != null) {
                startActivityForResult(intent, AUTH_REQUEST_CODE)
            } else {
                AppLockManager.isUnlockedSession = true
                findViewById<View>(resId("root_layout_container")).visibility = View.VISIBLE
            }
        } else {
            findViewById<View>(resId("root_layout_container")).visibility = View.VISIBLE
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == AUTH_REQUEST_CODE) {
            if (resultCode == RESULT_OK) {
                AppLockManager.isUnlockedSession = true
                findViewById<View>(resId("root_layout_container")).visibility = View.VISIBLE
            } else {
                finish()
            }
        }
    }

    @Suppress("DEPRECATION")
    private fun fixStatusBar() {
        window.addFlags(WindowManager.LayoutParams.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS)
        window.statusBarColor = Color.parseColor("#5E35B1")
        window.navigationBarColor = Color.parseColor("#5E35B1")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            window.decorView.systemUiVisibility = 0
        }
    }

    override fun onBackPressed() {
        if (isSidebarOpen) {
            toggleSidebar()
        } else {
            super.onBackPressed()
        }
    }

    private fun toggleSidebar() {
        isSidebarOpen = !isSidebarOpen
        val width = 280f * resources.displayMetrics.density

        if (isSidebarOpen) {
            sidebarMenu.visibility = View.VISIBLE
            dimOverlay.visibility = View.VISIBLE
            sidebarMenu.animate().translationX(0f).setDuration(250).start()
            dimOverlay.animate().alpha(1f).setDuration(250).start()
        } else {
            sidebarMenu.animate().translationX(-width).setDuration(250).withEndAction {
                sidebarMenu.visibility = View.GONE
            }.start()
            dimOverlay.animate().alpha(0f).setDuration(250).withEndAction {
                dimOverlay.visibility = View.GONE
            }.start()
        }
    }

    private fun openUrl(url: String) {
        try {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        } catch (_: Exception) {}
        toggleSidebar()
    }

    private fun contactUrl(): String =
        orFallback(NativeLib.getContactUrl(), "https://t.me/R3V_X")

    private fun communityUrl(): String =
        orFallback(NativeLib.getCommunityUrl(), "https://t.me/allinformation0173")

    private fun instagramUrl(): String =
        orFallback(NativeLib.getInstagramUrl(), "https://www.instagram.com/opeditzxx/?utm_source=qr&r=nametag")

    private fun githubUrl(): String =
        orFallback(NativeLib.getGithubUrl(), "https://github.com/v54087912-collab")

    private fun portfolioUrl(): String =
        orFallback(NativeLib.getPortfolioUrl(), "https://aboutmee.pages.dev/")

    private fun orFallback(native: String, fallback: String): String =
        if (native.isBlank() || native == "ERROR_LIB_NOT_LOADED") fallback else native
}
