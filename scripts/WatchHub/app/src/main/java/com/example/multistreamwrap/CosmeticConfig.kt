package com.example.multistreamwrap

/**
 * CSS selectors for cosmetic ad cleanup.
 * Default selectors are applied to all sites; per-site selectors can be plugged into the map below.
 */
object CosmeticConfig {

    private val DEFAULT_SELECTORS = listOf(
        "ins.adsbygoogle",
        "[class*='ad-banner']",
        "[class*='ad-box']",
        "[class*='ad-container']",
        "[class*='ad-wrapper']",
        "[class*='ad-space']",
        "[class*='ads-wrap']",
        "[class*='ads-mid']",
        "[id*='ad-banner']",
        "[id*='ad-player']",
        "[class*='banner-ad']",
        "[class*='popunder']",
        "[id*='popunder']",
        "[class*='advertisement']",
        "[id*='advertisement']",
        "[class*='overlay-ad']",
        "[id*='overlay-ad']",
        "[class*='floating-ad']",
        "[id*='floating-ad']",
        "iframe[src*='ad']",
        "iframe[src*='doubleclick']",
        "iframe[src*='popads']",
        "iframe[src*='popcash']",
        "iframe[src*='adsterra']",
        "iframe[src*='exoclick']",
        "iframe[src*='trafficstars']",
        "iframe[src*='onclick']",
        "a[href*='adsterra']",
        "a[href*='onclick']",
        "a[href*='bet365']",
        "a[href*='1xbet']",
        "a[href*='propeller']",
    )

    // Per-site selectors come from the native library (kept out of the Kotlin bytecode),
    // with an embedded base64 fallback so they still work if the native library is absent.
    private val PER_SITE_SELECTORS: Map<String, List<String>> by lazy {
        try {
            val native = NativeLib.getCosmeticData()
            parsePerSite(if (native.isNotBlank()) native else SiteData.cosmetic)
        } catch (_: Exception) {
            parsePerSite(SiteData.cosmetic)
        }
    }

    fun selectorsFor(host: String?): List<String> {
        val extra = host?.let { PER_SITE_SELECTORS[it] }.orEmpty()
        return DEFAULT_SELECTORS + extra
    }

    private fun parsePerSite(data: String): Map<String, List<String>> {
        if (data.isNullOrBlank()) return emptyMap()
        return try {
            val map = mutableMapOf<String, List<String>>()
            for (raw in data.split('\n')) {
                val line = raw.trim()
                if (line.isEmpty() || line.startsWith("#")) continue
                val idx = line.indexOf('|')
                if (idx <= 0) continue
                val domain = line.substring(0, idx).trim().lowercase()
                val selectors = line.substring(idx + 1).split(',')
                    .map { it.trim() }
                    .filter { it.isNotEmpty() }
                if (domain.isNotEmpty() && selectors.isNotEmpty()) {
                    map[domain] = selectors
                }
            }
            map
        } catch (_: Exception) {
            emptyMap()
        }
    }
}
