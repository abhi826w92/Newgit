package com.example.multistreamwrap

import android.content.Context
import android.content.SharedPreferences
import android.net.Uri

object SiteUrlManager {
    private const val PREFS_NAME = "site_url_overrides"

    private fun getPrefs(context: Context): SharedPreferences {
        return context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
    }

    /**
     * Cleans up any stale or corrupted overrides (such as specific deep browsing URLs,
     * query parameters, or error pages) so site cards always open clean homepages.
     */
    fun cleanStaleOverrides(context: Context) {
        try {
            val prefs = getPrefs(context)
            val editor = prefs.edit()
            var modified = false
            for ((key, value) in prefs.all) {
                val str = value as? String ?: continue
                val uri = try { Uri.parse(str) } catch (_: Exception) { null }
                if (uri == null || !uri.query.isNullOrBlank() || (uri.path != null && uri.path!!.length > 8)) {
                    editor.remove(key)
                    modified = true
                }
            }
            if (modified) editor.apply()
        } catch (_: Exception) {}
    }

    /**
     * Returns the latest working URL for a site, or the default bundled URL if not overridden.
     * Guarantees that deep subpaths or query strings are never returned for site launchers.
     */
    fun getResolvedUrl(context: Context, siteName: String, defaultUrl: String): String {
        if (siteName.isBlank()) return defaultUrl
        val saved = getPrefs(context).getString(siteName.trim(), null)
        if (saved.isNullOrBlank()) return defaultUrl

        val uri = try { Uri.parse(saved) } catch (_: Exception) { return defaultUrl }
        // If saved URL contains deep subpaths, query strings, or error parameters, purge it
        if (!uri.query.isNullOrBlank() || (uri.path != null && uri.path!!.length > 8)) {
            getPrefs(context).edit().remove(siteName.trim()).apply()
            return defaultUrl
        }
        return saved
    }

    /**
     * Saves a new working domain for a site.
     * STRICTLY extracts and stores ONLY the root origin (scheme + host), NEVER subpaths or query strings.
     */
    fun saveResolvedUrl(context: Context, siteName: String, newUrl: String) {
        if (siteName.isBlank() || newUrl.isBlank()) return
        val uri = try { Uri.parse(newUrl) } catch (_: Exception) { return }
        val host = uri.host ?: return
        val scheme = uri.scheme ?: "https"

        // Never store subpaths or query strings - only store the base origin
        val rootOrigin = "$scheme://$host/"
        getPrefs(context).edit().putString(siteName.trim(), rootOrigin).apply()
    }

    /**
     * Extracts the core brand / second-level domain name from a host.
     * e.g. "nightflix.vg" -> "nightflix", "www.cinezo.net" -> "cinezo", "sub.domain.co.uk" -> "domain"
     */
    fun extractBrandName(host: String?): String {
        if (host.isNullOrBlank()) return ""
        var cleanHost = host.lowercase().trim()
        if (cleanHost.startsWith("www.")) {
            cleanHost = cleanHost.substring(4)
        }
        val colonIdx = cleanHost.indexOf(':')
        if (colonIdx > 0) {
            cleanHost = cleanHost.substring(0, colonIdx)
        }

        val parts = cleanHost.split('.')
        if (parts.size <= 1) return cleanHost

        val secondLevelTlds = setOf("co", "com", "org", "net", "edu", "gov", "ac")
        val isMultiPartTld = parts.size >= 3 &&
                secondLevelTlds.contains(parts[parts.size - 2]) &&
                parts[parts.size - 1].length == 2

        val brandIndex = if (isMultiPartTld) parts.size - 3 else parts.size - 2
        return if (brandIndex >= 0) parts[brandIndex] else parts[0]
    }

    /**
     * Determines whether [newHost] represents a domain migration / mirror of [currentHost].
     */
    fun isSameBrandOrMigration(currentHost: String?, newHost: String?): Boolean {
        if (currentHost.isNullOrBlank() || newHost.isNullOrBlank()) return false
        val brand1 = extractBrandName(currentHost)
        val brand2 = extractBrandName(newHost)
        if (brand1.isEmpty() || brand2.isEmpty()) return false

        // Exact brand match (e.g. nightflix.net -> nightflix.vg)
        if (brand1 == brand2) return true

        // Subdomain / brand containment with minimum safe length
        val genericWords = setOf("video", "stream", "watch", "movie", "movies", "anime", "play", "embed", "cloud")
        if (!genericWords.contains(brand1) && !genericWords.contains(brand2)) {
            if (brand1.length >= 5 && brand2.length >= 5) {
                if (brand1.startsWith(brand2) || brand2.startsWith(brand1)) return true
            }
        }

        return false
    }
}
