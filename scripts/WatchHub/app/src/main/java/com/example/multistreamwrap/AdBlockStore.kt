package com.example.multistreamwrap

import android.content.Context
import android.net.Uri

/**
 * High-performance Ad-block host list loader (singleton).
 * EasyList-style entries are read from assets/adblock_hosts.txt.
 * Uses O(1) Set lookups, subdomain hierarchy checks, and heuristic signatures for rogue ad networks.
 */
class AdBlockStore private constructor(context: Context) {

    private val blockedHosts: Set<String> = loadHosts(context)

    /** Returns true if the URL's host or path matches any ad network entry. */
    fun shouldBlock(url: String?): Boolean {
        if (url.isNullOrBlank()) return false
        val uri = try { Uri.parse(url) } catch (_: Exception) { return false }
        val host = uri.host?.lowercase() ?: return false

        // 1. Direct O(1) host lookup
        if (blockedHosts.contains(host)) return true

        // 2. O(1) Subdomain hierarchy lookup: e.g. "a.b.adsterra.com" -> checks "b.adsterra.com", "adsterra.com"
        var dotIndex = host.indexOf('.')
        while (dotIndex != -1 && dotIndex < host.length - 1) {
            val parent = host.substring(dotIndex + 1)
            if (blockedHosts.contains(parent)) return true
            dotIndex = host.indexOf('.', dotIndex + 1)
        }

        // 3. Fast heuristic signature matching for rogue streaming ad networks & popunders
        if (isRogueAdSignature(host, uri.path.orEmpty())) return true

        return false
    }

    private fun isRogueAdSignature(host: String, path: String): Boolean {
        // Obvious ad/popup subdomains
        if (host.startsWith("pop.") || host.startsWith("popunder.") ||
            host.startsWith("ads.") || host.startsWith("ad.") ||
            host.startsWith("banner.") || host.startsWith("track.") ||
            host.startsWith("telemetry.")) {
            return true
        }

        // Known rogue ad networks and redirect patterns in domain
        if (host.contains("adserver") || host.contains("adservice") ||
            host.contains("onclick") || host.contains("popads") ||
            host.contains("popcash") || host.contains("adsterra") ||
            host.contains("propeller") || host.contains("exoclick") ||
            host.contains("trafficjunky") || host.contains("trafficstars") ||
            host.contains("monetag") || host.contains("hilltopads") ||
            host.contains("bet365") || host.contains("1xbet") ||
            host.contains("parimatch") || host.contains("betwinner")) {
            return true
        }

        // Suspicious path patterns
        val lowerPath = path.lowercase()
        if (lowerPath.contains("/popunder") || lowerPath.contains("/adserver") ||
            lowerPath.contains("/googleads") || lowerPath.contains("/ads.js") ||
            lowerPath.contains("/banner.js") || lowerPath.contains("/ad-delivery")) {
            return true
        }

        return false
    }

    private fun loadHosts(context: Context): Set<String> {
        return try {
            context.assets.open("adblock_hosts.txt")
                .bufferedReader()
                .useLines { lines ->
                    lines.mapNotNull(::parseHostLine).toHashSet()
                }
        } catch (e: Exception) {
            emptySet()
        }
    }

    /** Extracts the host from an EasyList line, ignoring comments ("#", "!"), exceptions ("@@"), and wildcards. */
    private fun parseHostLine(line: String): String? {
        var l = line.trim()
        if (l.isEmpty()) return null
        if (l.startsWith("#") || l.startsWith("!") || l.startsWith("@@")) return null

        if (l.startsWith("||")) l = l.removePrefix("||")

        val cut = l.indexOfFirst { it == '/' || it == '^' || it == '$' || it == ':' || it == '*' }
        if (cut >= 0) l = l.substring(0, cut)

        l = l.trim('*', '.', '^').trim()
        if (l.isEmpty() || l.contains('*')) return null
        return l.lowercase()
    }

    companion object {
        @Volatile
        private var instance: AdBlockStore? = null

        fun get(context: Context): AdBlockStore =
            instance ?: synchronized(this) {
                instance ?: AdBlockStore(context.applicationContext).also { instance = it }
            }
    }
}

