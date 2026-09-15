package com.example.multistreamwrap

import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.webkit.CookieManager
import java.util.Collections

data class DetectedMedia(
    val url: String,
    val referer: String,
    val headers: Map<String, String>,
    val type: String,
    val title: String,
    val detectedAt: Long = System.currentTimeMillis()
)

object VideoSniffer {

    private val detectedList = Collections.synchronizedList(mutableListOf<DetectedMedia>())
    private val seenUrls = Collections.synchronizedSet(mutableSetOf<String>())
    private val handler = Handler(Looper.getMainLooper())

    var listener: ((Int) -> Unit)? = null

    private val IGNORE_HOSTS = setOf(
        "google-analytics.com",
        "doubleclick.net",
        "googlesyndication.com",
        "adservice.google.com",
        "scorecardresearch.com",
        "ytimg.com",
        "gstatic.com",
        "ggpht.com",
        "googleusercontent.com",
        "facebook.com",
        "connect.facebook.net"
    )

    private val IGNORE_PATH_KEYWORDS = listOf(
        "/api/stats",
        "generate_204",
        "/pagead",
        "/ptracking",
        "log_event",
        "analytics",
        "/telemetry",
        "favicon",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
        ".css",
        ".js",
        ".vtt",
        ".srt"
    )

    fun clear() {
        detectedList.clear()
        seenUrls.clear()
        notifyListener(0)
    }

    fun getDetectedMedia(): List<DetectedMedia> {
        synchronized(detectedList) {
            return ArrayList(detectedList)
        }
    }

    fun count(): Int = detectedList.size

    fun registerMedia(item: DetectedMedia) {
        val isNew = synchronized(seenUrls) {
            seenUrls.add(item.url)
        }
        if (isNew) {
            detectedList.add(item)
            notifyListener(detectedList.size)
        }
    }

    fun inspectUrl(
        url: String?,
        headers: Map<String, String>?,
        pageUrl: String,
        pageTitle: String
    ): Boolean {
        if (url.isNullOrBlank()) return false

        val actualUrl = if (url.startsWith("enc2:")) KartoonsEnc2.decrypt(url) else url
        return try {
            val lower = actualUrl.lowercase()

            // Skip data / javascript schemes
            if (lower.startsWith("data:") || lower.startsWith("javascript:")) {
                return false
            }

            // Local file cached playlists are always valid streams
            val isLocalFile = actualUrl.startsWith("file://")

            val uri = try { Uri.parse(actualUrl) } catch (_: Exception) { null }
            val host = uri?.host?.lowercase().orEmpty()
            val path = uri?.path?.lowercase().orEmpty()

            if (!isLocalFile) {
                // Fast junk check
                for (ignore in IGNORE_HOSTS) {
                    if (host.contains(ignore)) return false
                }

                // Secondary junk check for tracking URLs disguised with video extensions
                for (junk in IGNORE_PATH_KEYWORDS) {
                    if (junk.startsWith(".") && path.endsWith(junk)) return false
                    if (!junk.startsWith(".") && path.contains(junk)) return false
                }

                // Exclude individual transport stream segments (.ts, .m4s) from cluttering the picker,
                // because the master .m3u8 playlist contains all segments.
                if (path.endsWith(".ts") || path.endsWith(".m4s") || path.endsWith(".aac")) {
                    return false
                }

                // Ignore non-media documents and text assets (strictly reject .txt, master.txt, etc.)
                if (path.endsWith(".html") || path.endsWith(".htm") || path.endsWith(".php") ||
                    path.endsWith(".js") || path.endsWith(".css") || path.endsWith(".vtt") ||
                    path.endsWith(".srt") || path.endsWith(".json") || path.endsWith(".xml") ||
                    path.endsWith(".txt") || lower.contains("master.txt")) {
                    return false
                }

                // Exclude HTML player/iframe embed paths (e.g. /embed/, /player/, /video/hash)
                if (lower.contains("/embed/") || lower.contains("/player/") ||
                    Regex("/video/[a-zA-Z0-9_-]+$").containsMatchIn(path)) {
                    return false
                }
            }

            // Real media stream verification
            val hasMpegUrlAccept = headers?.get("Accept")?.contains("mpegurl", ignoreCase = true) == true ||
                    headers?.get("accept")?.contains("mpegurl", ignoreCase = true) == true

            val isM3u8 = isLocalFile ||
                    lower.contains(".m3u8") || lower.contains(".m3u") ||
                    lower.contains("playlist") || lower.contains("manifest") ||
                    (lower.contains("master") && !path.endsWith(".js") && !path.endsWith(".txt") && !lower.contains("master.txt")) ||
                    (lower.contains("/hls") && !path.endsWith(".ts") && !path.endsWith(".js") && !path.endsWith(".txt") && !lower.contains("master.txt")) ||
                    hasMpegUrlAccept

            val isMp4 = (lower.contains(".mp4") || lower.contains(".m4v") || lower.contains("videoplayback")) &&
                    !path.endsWith(".html")

            val isWebm = lower.contains(".webm")
            val isMkv = lower.contains(".mkv")

            if (!isM3u8 && !isMp4 && !isWebm && !isMkv) {
                return false
            }

            val isNew = synchronized(seenUrls) {
                seenUrls.add(actualUrl)
            }
            if (!isNew) return false

            val type = when {
                isM3u8 -> "HLS Stream"
                isMp4 -> "MP4 Video"
                isWebm -> "WebM Video"
                isMkv -> "MKV Video"
                else -> "Video Stream"
            }

            val cleanHeaders = mutableMapOf<String, String>()
            if (headers != null) {
                cleanHeaders.putAll(headers)
            }

            // Normalize and resolve Referer
            val existingReferer = cleanHeaders.entries.firstOrNull { it.key.equals("Referer", ignoreCase = true) }?.value
            val effectiveReferer = if (!existingReferer.isNullOrBlank()) {
                existingReferer
            } else if (actualUrl.contains("zephyrix.org") || actualUrl.contains("zn-grid")) {
                "https://play.zephyrix.org/"
            } else if (pageUrl.isNotBlank()) {
                pageUrl
            } else {
                try {
                    val u = Uri.parse(actualUrl)
                    if (!u.host.isNullOrBlank()) "${u.scheme}://${u.host}/" else ""
                } catch (_: Throwable) { "" }
            }
            if (effectiveReferer.isNotBlank()) {
                cleanHeaders["Referer"] = effectiveReferer
            }

            // Capture and normalize Origin
            val existingOrigin = cleanHeaders.entries.firstOrNull { it.key.equals("Origin", ignoreCase = true) }?.value
            if (existingOrigin.isNullOrBlank()) {
                try {
                    val pUri = if (pageUrl.isNotBlank()) Uri.parse(pageUrl) else Uri.parse(actualUrl)
                    if (!pUri.host.isNullOrBlank()) {
                        cleanHeaders["Origin"] = "${pUri.scheme}://${pUri.host}"
                    }
                } catch (_: Throwable) {}
            }

            // Always enforce Zephyrix player headers for zephyrix and zn-grid CDN domains
            if (actualUrl.contains("zephyrix.org") || actualUrl.contains("zn-grid")) {
                cleanHeaders["Referer"] = "https://play.zephyrix.org/"
                cleanHeaders["Origin"] = "https://play.zephyrix.org"
            }

            // Automatically attach current session cookies from CookieManager for CDN auth
            try {
                val cookies = CookieManager.getInstance().getCookie(actualUrl) ?: CookieManager.getInstance().getCookie(pageUrl)
                val hasCookie = cleanHeaders.keys.any { it.equals("Cookie", ignoreCase = true) }
                if (!cookies.isNullOrBlank() && !hasCookie) {
                    cleanHeaders["Cookie"] = cookies
                }
            } catch (_: Throwable) {}

            val safeTitle = sanitizeTitle(pageTitle)
            val item = DetectedMedia(
                url = actualUrl,
                referer = effectiveReferer,
                headers = cleanHeaders,
                type = type,
                title = safeTitle
            )

            detectedList.add(item)
            notifyListener(detectedList.size)
            true
        } catch (_: Throwable) {
            false
        }
    }

    private fun sanitizeTitle(title: String): String {
        var clean = title.trim()
        if (clean.isBlank()) return "Video_${System.currentTimeMillis()}"

        // Remove common site suffix clutter
        clean = clean.replace(Regex("(?i)\\s*\\|\\s*watch.*"), "")
            .replace(Regex("(?i)\\s*-\\s*watch.*"), "")
            .replace(Regex("(?i)\\s*online free.*"), "")
            .replace(Regex("[\\\\/:*?\"<>|]"), " ")
            .trim()

        return if (clean.length > 80) clean.substring(0, 80).trim() else clean
    }

    private fun notifyListener(count: Int) {
        handler.post {
            listener?.invoke(count)
        }
    }
}
