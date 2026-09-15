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
import android.widget.TextView

class FavoritesActivity : Activity() {

    private lateinit var adapter: SiteAdapter
    private lateinit var emptyText: TextView

    override fun attachBaseContext(newBase: Context) {
        super.attachBaseContext(ThemeManager.wrapContext(newBase))
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        ThemeManager.applyTheme(this)
        super.onCreate(savedInstanceState)
        setContentView(resources.getIdentifier("activity_favorites", "layout", packageName))
        fixStatusBar()

        val root = findViewById<View>(resources.getIdentifier("root_layout", "id", packageName))
        val toolbar = findViewById<LinearLayout>(resources.getIdentifier("toolbar", "id", packageName))
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
            root.setPadding(left, 0, right, bottom)
            insets
        }

        findViewById<ImageButton>(resources.getIdentifier("btn_back", "id", packageName)).setOnClickListener {
            finish()
        }

        emptyText = findViewById(resources.getIdentifier("empty_text", "id", packageName))
        
        adapter = SiteAdapter(this, onSiteClick = { site ->
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
        }, onFavoriteChanged = {
            loadFavorites()
        })

        val list = findViewById<ListView>(resources.getIdentifier("site_list", "id", packageName))
        list.adapter = adapter

        loadFavorites()
    }
    
    override fun onResume() {
        super.onResume()
        loadFavorites()
    }

    private fun loadFavorites() {
        val favNames = FavoritesManager.getFavorites(this)
        val favSites = mutableListOf<Site>()
        
        for (cat in SitesProvider.categories) {
            for (site in cat.sites) {
                if (favNames.contains(site.name)) {
                    if (!favSites.any { it.name == site.name }) {
                        favSites.add(site)
                    }
                }
            }
        }
        
        val items = mutableListOf<Any>()
        if (favSites.isNotEmpty()) {
            items.add("Favorites")
            items.addAll(favSites)
            emptyText.visibility = View.GONE
        } else {
            emptyText.visibility = View.VISIBLE
        }
        
        adapter.submit(items)
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
