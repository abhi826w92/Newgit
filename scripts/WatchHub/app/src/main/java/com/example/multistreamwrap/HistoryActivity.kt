package com.example.multistreamwrap

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.view.View
import android.view.WindowManager
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.ListView

class HistoryActivity : Activity() {

    private lateinit var adapter: SiteAdapter
    private lateinit var emptyView: View

    override fun attachBaseContext(newBase: Context) {
        super.attachBaseContext(ThemeManager.wrapContext(newBase))
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        ThemeManager.applyTheme(this)
        super.onCreate(savedInstanceState)
        setContentView(layout("activity_history"))
        fixStatusBar()

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
            toolbar.setPadding(toolbar.paddingLeft, top, toolbar.paddingRight, toolbar.paddingBottom)
            findViewById<View>(resId("root_layout")).setPadding(0, 0, 0, bottom)
            insets
        }

        findViewById<ImageButton>(resId("btn_back")).setOnClickListener { finish() }

        emptyView = findViewById(resId("empty_view"))
        val list = findViewById<ListView>(resId("history_list"))
        
        adapter = SiteAdapter(
            this,
            onSiteClick = { site ->
                val targetUrl = SiteUrlManager.getResolvedUrl(this, site.name, site.url)
                if (SiteLockManager.isSiteLocked(this, site.name)) {
                    SiteLockManager.authenticate(this, "Unlock ${site.name}", onSuccess = {
                        val intent = Intent(this, WebActivity::class.java)
                            .putExtra(WebActivity.EXTRA_SITE_NAME, site.name)
                            .putExtra(WebActivity.EXTRA_SITE_URL, targetUrl)
                        startActivity(intent)
                    })
                } else {
                    val intent = Intent(this, WebActivity::class.java)
                        .putExtra(WebActivity.EXTRA_SITE_NAME, site.name)
                        .putExtra(WebActivity.EXTRA_SITE_URL, targetUrl)
                    startActivity(intent)
                }
            },
            onSiteSwipeDelete = { site ->
                HistoryManager.removeSite(this, site.url)
                loadHistory()
                android.widget.Toast.makeText(this, "${site.name} removed from history", android.widget.Toast.LENGTH_SHORT).show()
            }
        )
        list.adapter = adapter

        val switchToggle = findViewById<android.widget.Switch>(resId("switch_history_toggle"))
        switchToggle?.isChecked = HistoryManager.isHistoryEnabled(this)
        switchToggle?.setOnCheckedChangeListener { _, isChecked ->
            HistoryManager.setHistoryEnabled(this, isChecked)
            val msg = if (isChecked) "Watch History Recording Enabled" else "Watch History Recording Disabled"
            android.widget.Toast.makeText(this, msg, android.widget.Toast.LENGTH_SHORT).show()
        }

        findViewById<ImageButton>(resId("btn_clear")).setOnClickListener {
            HistoryManager.clearHistory(this)
            loadHistory()
        }
    }

    override fun onResume() {
        super.onResume()
        loadHistory()
    }

    private fun loadHistory() {
        val history = HistoryManager.getHistory(this)
        if (history.isEmpty()) {
            emptyView.visibility = View.VISIBLE
            adapter.submit(emptyList())
        } else {
            emptyView.visibility = View.GONE
            val items = mutableListOf<Any>()
            history.forEach {
                items.add(Site(it.name, it.url))
            }
            adapter.submit(items)
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
}
