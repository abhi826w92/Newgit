package com.example.multistreamwrap

import android.content.Context
import android.os.Environment
import android.util.Log
import android.webkit.CookieManager
import java.io.BufferedInputStream
import java.io.BufferedReader
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.IOException
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL
import java.nio.ByteBuffer
import java.security.SecureRandom
import java.security.cert.X509Certificate
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import javax.crypto.Cipher
import javax.crypto.spec.IvParameterSpec
import javax.crypto.spec.SecretKeySpec
import javax.net.ssl.HostnameVerifier
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManager
import javax.net.ssl.X509TrustManager

data class HlsQuality(
    val resolution: String,
    val bandwidth: Long,
    val url: String
) {
    val displayLabel: String
        get() {
            val h = if (resolution.contains("x")) resolution.split("x")[1] else resolution
            return when {
                h.equals("Auto", ignoreCase = true) -> "Auto (Best Quality)"
                h.startsWith("1080") -> "1080p (Full HD)"
                h.startsWith("720") -> "720p (HD)"
                h.startsWith("480") -> "480p (SD)"
                h.startsWith("360") -> "360p (Low)"
                resolution.isNotBlank() && !resolution.equals("Auto", ignoreCase = true) -> "${resolution}p"
                else -> "Auto (Best Quality)"
            }
        }
}

class HlsDownloader(private val context: Context? = null) {

    companion object {
        private const val TAG = "HlsDownloader"
        private const val USER_AGENT =
            "Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
        private const val MIN_VIDEO_BYTES = 500 * 1024L // Minimum 500 KB to be a valid video

        fun getDownloadDirectory(context: Context? = null): File {
            // 1. Try public storage if available and writable
            try {
                val publicDownloads = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
                val watchHubFolder = File(publicDownloads, "WatchHub")
                if (watchHubFolder.exists() || watchHubFolder.mkdirs()) {
                    if (watchHubFolder.canWrite()) {
                        return watchHubFolder
                    }
                }
            } catch (_: Exception) {}

            // 2. Safe fallback: App external files directory (100% writable on all Android versions, no permissions needed)
            if (context != null) {
                try {
                    val extDownloads = context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)
                    if (extDownloads != null && (extDownloads.exists() || extDownloads.mkdirs()) && extDownloads.canWrite()) {
                        val appWatchHub = File(extDownloads, "WatchHub")
                        if (appWatchHub.exists() || appWatchHub.mkdirs()) return appWatchHub
                        return extDownloads
                    }
                } catch (_: Exception) {}

                try {
                    val filesDir = File(context.filesDir, "WatchHub")
                    if (filesDir.exists() || filesDir.mkdirs()) return filesDir
                    return context.filesDir
                } catch (_: Exception) {}
            }

            // 3. Fallback to public
            val publicDownloads = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
            return File(publicDownloads, "WatchHub")
        }

        fun sanitizeFilename(name: String): String {
            val safe = name.replace(Regex("[\\\\/:*?\"<>|]"), "_").trim()
            return if (safe.endsWith(".mp4", ignoreCase = true)) safe else "$safe.mp4"
        }

        fun getUniqueTargetFile(dir: File, baseName: String): File {
            val safe = sanitizeFilename(baseName)
            val nameWithoutExt = if (safe.endsWith(".mp4", ignoreCase = true)) safe.substring(0, safe.length - 4) else safe
            var file = File(dir, "$nameWithoutExt.mp4")
            var count = 1
            while (file.exists() && file.length() > 0) {
                file = File(dir, "$nameWithoutExt ($count).mp4")
                count++
            }
            return file
        }
    }

    @Volatile
    var isCancelled: Boolean = false

    // Permissive SSL context for CDNs with intermediate certificate or SNI discrepancies
    private val permissiveSslContext by lazy {
        try {
            val trustAll = arrayOf<TrustManager>(object : X509TrustManager {
                override fun getAcceptedIssuers(): Array<X509Certificate>? = null
                override fun checkClientTrusted(chain: Array<X509Certificate>?, authType: String?) {}
                override fun checkServerTrusted(chain: Array<X509Certificate>?, authType: String?) {}
            })
            val sc = SSLContext.getInstance("TLS")
            sc.init(null, trustAll, SecureRandom())
            sc
        } catch (_: Exception) {
            null
        }
    }

    /**
     * Inspects an .m3u8 URL. If it's a master playlist with multiple streams, returns available qualities.
     * Decrypts any enc2: streams if present.
     */
    fun parseQualities(masterUrl: String, headers: Map<String, String>): List<HlsQuality> {
        val qualities = mutableListOf<HlsQuality>()
        val actualMasterUrl = if (masterUrl.startsWith("enc2:")) KartoonsEnc2.decrypt(masterUrl) else masterUrl

        try {
            var content = fetchText(actualMasterUrl, headers)
            if (content.isBlank() || (!content.contains("#EXTM3U") && !content.contains("#EXTINF"))) {
                Log.w(TAG, "parseQualities: text does not look like an M3U8 playlist")
                return listOf(HlsQuality("Auto", 0L, actualMasterUrl))
            }

            if (content.contains("enc2:")) {
                content = KartoonsEnc2.decryptM3u8(content)
            }

            val lines = content.lines()
            var lastResolution = ""
            var lastBandwidth: Long = 0

            for (i in lines.indices) {
                val line = lines[i].trim()
                if (line.startsWith("#EXT-X-STREAM-INF:")) {
                    val resMatch = Regex("RESOLUTION=(\\d+x\\d+)").find(line)
                    if (resMatch != null) {
                        lastResolution = resMatch.groupValues[1]
                    }
                    val bwMatch = Regex("BANDWIDTH=(\\d+)").find(line)
                    if (bwMatch != null) {
                        lastBandwidth = bwMatch.groupValues[1].toLongOrNull() ?: 0L
                    }
                } else if (line.isNotEmpty() && !line.startsWith("#")) {
                    if (lastResolution.isNotEmpty() || lastBandwidth > 0) {
                        val streamLine = if (line.startsWith("enc2:")) KartoonsEnc2.decrypt(line) else line
                        val resolvedUrl = resolveUrl(actualMasterUrl, streamLine)
                        qualities.add(HlsQuality(lastResolution, lastBandwidth, resolvedUrl))
                        lastResolution = ""
                        lastBandwidth = 0L
                    }
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to parse master qualities", e)
        }

        return if (qualities.isEmpty()) {
            listOf(HlsQuality("Auto", 0L, actualMasterUrl))
        } else {
            qualities.sortedByDescending { it.bandwidth }
        }
    }

    /**
     * Downloads an HLS stream (or direct video) and writes directly to an output MP4 file.
     */
    fun download(
        streamUrl: String,
        headers: Map<String, String>,
        targetFile: File,
        onProgress: (bytesDownloaded: Long, currentSegment: Int, totalSegments: Int, speedBytesPerSec: Long) -> Unit
    ): Boolean {
        isCancelled = false
        val actualUrl = if (streamUrl.startsWith("enc2:")) KartoonsEnc2.decrypt(streamUrl) else streamUrl

        val isHls = actualUrl.contains(".m3u8", ignoreCase = true) ||
                actualUrl.contains(".m3u", ignoreCase = true) ||
                actualUrl.contains("/hls/") ||
                actualUrl.contains("master.") ||
                actualUrl.contains("playlist") ||
                actualUrl.contains("manifest")

        return if (isHls) {
            downloadHlsInternal(actualUrl, headers, targetFile, onProgress)
        } else {
            downloadDirectInternal(actualUrl, headers, targetFile, onProgress)
        }
    }

    private fun downloadHlsInternal(
        m3u8Url: String,
        headers: Map<String, String>,
        targetFile: File,
        onProgress: (Long, Int, Int, Long) -> Unit
    ): Boolean {
        var tempDir: File? = null
        try {
            val actualUrl = if (m3u8Url.startsWith("enc2:")) KartoonsEnc2.decrypt(m3u8Url) else m3u8Url
            var playlistText = fetchText(actualUrl, headers)

            if (playlistText.isBlank() || (!playlistText.contains("#EXTM3U") && !playlistText.contains("#EXTINF"))) {
                Log.e(TAG, "Invalid HLS playlist content received for: $actualUrl")
                return false
            }

            // Decrypt any encrypted URLs or lines (Kartoons enc2: format)
            if (playlistText.contains("enc2:")) {
                playlistText = KartoonsEnc2.decryptM3u8(playlistText)
            }

            val lines = playlistText.lines()

            // Check if this is a master playlist pointing to another sub-stream
            if (playlistText.contains("#EXT-X-STREAM-INF:")) {
                val qualities = parseQualities(actualUrl, headers)
                val best = qualities.firstOrNull() ?: return false
                return downloadHlsInternal(best.url, headers, targetFile, onProgress)
            }

            // Parse encryption info (Standard AES-128)
            var encryptionKey: ByteArray? = null
            var encryptionIv: ByteArray? = null
            var isEncrypted = false

            val keyLine = lines.firstOrNull { it.startsWith("#EXT-X-KEY:") }
            if (keyLine != null && !keyLine.contains("METHOD=NONE")) {
                isEncrypted = true
                val uriMatch = Regex("URI=\"([^\"]+)\"").find(keyLine)
                if (uriMatch != null) {
                    val rawKeyUri = uriMatch.groupValues[1]
                    val resolvedKeyUri = if (rawKeyUri.startsWith("enc2:")) KartoonsEnc2.decrypt(rawKeyUri) else rawKeyUri
                    val keyUrl = resolveUrl(actualUrl, resolvedKeyUri)
                    encryptionKey = fetchBytesWithRetry(keyUrl, headers, 5)
                }
                val ivMatch = Regex("IV=(0x[0-9a-fA-F]+)").find(keyLine)
                if (ivMatch != null) {
                    encryptionIv = hexStringToByteArray(ivMatch.groupValues[1].removePrefix("0x"))
                }
            }

            // Support #EXT-X-MAP (Fragmented MP4 / CMAF initialization header)
            var initSegmentBytes: ByteArray? = null
            val mapLine = lines.firstOrNull { it.startsWith("#EXT-X-MAP:") }
            if (mapLine != null) {
                val mapUriMatch = Regex("URI=\"([^\"]+)\"").find(mapLine)
                if (mapUriMatch != null) {
                    val rawMapUri = mapUriMatch.groupValues[1]
                    val resolvedMapUri = if (rawMapUri.startsWith("enc2:")) KartoonsEnc2.decrypt(rawMapUri) else rawMapUri
                    val mapUrl = resolveUrl(actualUrl, resolvedMapUri)
                    initSegmentBytes = fetchBytesWithRetry(mapUrl, headers, 5)
                }
            }

            // Extract segment URLs
            val segments = mutableListOf<String>()
            for (line in lines) {
                val l = line.trim()
                if (l.isNotEmpty() && !l.startsWith("#")) {
                    val resolved = if (l.startsWith("enc2:")) KartoonsEnc2.decrypt(l) else l
                    segments.add(resolveUrl(actualUrl, resolved))
                }
            }

            if (segments.isEmpty()) {
                Log.e(TAG, "No segments found in M3U8: $actualUrl")
                return false
            }

            val totalSegments = segments.size

            // Ensure parent directory exists
            targetFile.parentFile?.let {
                if (!it.exists()) it.mkdirs()
            }

            // Safe working temporary directory in app's internal/external cache (100% permission-free on Android 10-15)
            val baseTempDir = context?.externalCacheDir ?: context?.cacheDir ?: targetFile.parentFile ?: File(".")
            tempDir = File(baseTempDir, ".temp_hls_${System.currentTimeMillis()}_${(1000..9999).random()}")
            if (!tempDir.exists()) tempDir.mkdirs()

            val completedCount = AtomicInteger(0)
            val totalBytes = AtomicLong(0L)
            val hasFailed = AtomicBoolean(false)

            // Allow tolerance for up to ~4% dropped segments on long streams before declaring fatal failure
            val maxAllowedFailed = maxOf(3, totalSegments / 25)
            val failedSegmentCount = AtomicInteger(0)

            // Speed calculation trackers
            var speedWindowStart = System.currentTimeMillis()
            var speedBytesWindow = 0L
            var currentSpeed = 0L

            // 3 concurrent workers for high-speed segment downloading
            val threadPool = Executors.newFixedThreadPool(3)

            for (index in segments.indices) {
                val segIndex = index
                val segUrl = segments[segIndex]

                threadPool.execute {
                    if (isCancelled || hasFailed.get()) return@execute

                    try {
                        val segHeaders = if (segUrl.contains("zn-grid") || segUrl.contains("zephyrix.org")) {
                            headers.toMutableMap().apply {
                                put("Referer", "https://play.zephyrix.org/")
                                put("Origin", "https://play.zephyrix.org")
                            }
                        } else {
                            headers
                        }
                        var segBytes = fetchBytesWithRetry(segUrl, segHeaders, 5)
                        if (segBytes == null || segBytes.isEmpty()) {
                            val currentFails = failedSegmentCount.incrementAndGet()
                            Log.w(TAG, "Failed segment $segIndex ($currentFails / $maxAllowedFailed): $segUrl")
                            if (currentFails > maxAllowedFailed) {
                                Log.e(TAG, "Too many segment failures. Aborting download.")
                                hasFailed.set(true)
                                return@execute
                            }
                            // Tolerant fallback: empty placeholder so file stream doesn't misalign
                            segBytes = ByteArray(0)
                        }

                        if (isEncrypted && encryptionKey != null && segBytes.isNotEmpty()) {
                            val iv = encryptionIv ?: toBytes16Big(segIndex + 1)
                            segBytes = decryptAes(encryptionKey, segBytes, iv)
                        }

                        val chunkFile = File(tempDir, String.format("seg_%06d.ts", segIndex))
                        FileOutputStream(chunkFile).use { it.write(segBytes) }

                        val currentDone = completedCount.incrementAndGet()
                        val bytesNow = totalBytes.addAndGet(segBytes.size.toLong())

                        synchronized(this) {
                            val now = System.currentTimeMillis()
                            speedBytesWindow += segBytes.size
                            if (now - speedWindowStart >= 800) {
                                currentSpeed = ((speedBytesWindow.toDouble() / (now - speedWindowStart)) * 1000).toLong()
                                speedWindowStart = now
                                speedBytesWindow = 0
                            }
                            onProgress(bytesNow, currentDone, totalSegments, currentSpeed)
                        }
                    } catch (e: Exception) {
                        Log.e(TAG, "Error downloading segment $segIndex", e)
                        val currentFails = failedSegmentCount.incrementAndGet()
                        if (currentFails > maxAllowedFailed) {
                            hasFailed.set(true)
                        }
                    }
                }
            }

            threadPool.shutdown()
            threadPool.awaitTermination(2, TimeUnit.HOURS)

            if (isCancelled || hasFailed.get() || completedCount.get() < (totalSegments - maxAllowedFailed)) {
                cleanupTempDir(tempDir)
                if (targetFile.exists()) targetFile.delete()
                return false
            }

            // Concatenate segments in order into the final target MP4 file
            FileOutputStream(targetFile).use { outStream ->
                // Write fMP4 initialization header if present
                if (initSegmentBytes != null && initSegmentBytes.isNotEmpty()) {
                    outStream.write(initSegmentBytes)
                }

                val buffer = ByteArray(64 * 1024)
                for (i in 0 until totalSegments) {
                    val chunkFile = File(tempDir, String.format("seg_%06d.ts", i))
                    if (chunkFile.exists()) {
                        if (chunkFile.length() > 0) {
                            FileInputStream(chunkFile).use { inStream ->
                                var read: Int
                                while (inStream.read(buffer).also { read = it } != -1) {
                                    outStream.write(buffer, 0, read)
                                }
                            }
                        }
                        chunkFile.delete()
                    }
                }
                outStream.flush()
            }

            cleanupTempDir(tempDir)

            // Safety guard: File must exist and be at least 500 KB to be a real video
            if (!targetFile.exists() || targetFile.length() < MIN_VIDEO_BYTES) {
                Log.e(TAG, "Target video file is too small or missing: ${targetFile.length()} bytes")
                if (targetFile.exists()) targetFile.delete()
                return false
            }

            return true
        } catch (e: Exception) {
            Log.e(TAG, "HLS download failed", e)
            if (tempDir != null) cleanupTempDir(tempDir)
            if (targetFile.exists()) targetFile.delete()
            return false
        }
    }

    private fun downloadDirectInternal(
        urlStr: String,
        headers: Map<String, String>,
        targetFile: File,
        onProgress: (Long, Int, Int, Long) -> Unit
    ): Boolean {
        try {
            val conn = openConnectionWithRedirects(urlStr, headers)

            // Safety check: verify content-type is not an HTML page, error document, or JSON
            val contentType = conn.contentType?.lowercase().orEmpty()
            if (contentType.contains("text/html") ||
                contentType.contains("application/json") ||
                contentType.contains("text/plain") ||
                contentType.contains("text/xml")
            ) {
                Log.e(TAG, "Direct download rejected: Server returned non-video Content-Type: '$contentType'")
                conn.disconnect()
                return false
            }

            val totalLength = conn.contentLength.toLong()
            val inputStream = BufferedInputStream(conn.inputStream)

            targetFile.parentFile?.let {
                if (!it.exists()) it.mkdirs()
            }
            val outputStream = FileOutputStream(targetFile)

            val buffer = ByteArray(64 * 1024)
            var bytesRead: Int
            var downloaded: Long = 0

            var speedWindowStart = System.currentTimeMillis()
            var speedBytesWindow: Long = 0
            var currentSpeed: Long = 0

            try {
                while (inputStream.read(buffer).also { bytesRead = it } != -1) {
                    if (isCancelled) {
                        outputStream.close()
                        inputStream.close()
                        targetFile.delete()
                        return false
                    }

                    outputStream.write(buffer, 0, bytesRead)
                    downloaded += bytesRead
                    speedBytesWindow += bytesRead

                    val now = System.currentTimeMillis()
                    if (now - speedWindowStart >= 1000) {
                        currentSpeed = ((speedBytesWindow.toDouble() / (now - speedWindowStart)) * 1000).toLong()
                        speedWindowStart = now
                        speedBytesWindow = 0
                    }

                    val total100 = if (totalLength > 0) 100 else 1
                    val currentPercent = if (totalLength > 0) ((downloaded * 100) / totalLength).toInt() else 0
                    onProgress(downloaded, currentPercent, total100, currentSpeed)
                }
                outputStream.flush()
            } finally {
                try { outputStream.close() } catch (_: Exception) {}
                try { inputStream.close() } catch (_: Exception) {}
            }

            // Safety guard: Must be at least 500 KB
            if (!targetFile.exists() || targetFile.length() < MIN_VIDEO_BYTES) {
                Log.e(TAG, "Direct download file is too small: ${targetFile.length()} bytes. Deleting.")
                if (targetFile.exists()) targetFile.delete()
                return false
            }

            return true
        } catch (e: Exception) {
            Log.e(TAG, "Direct download failed", e)
            if (targetFile.exists()) targetFile.delete()
            return false
        }
    }

    private fun cleanupTempDir(dir: File) {
        try {
            if (dir.exists()) {
                dir.listFiles()?.forEach { it.delete() }
                dir.delete()
            }
        } catch (_: Exception) {}
    }

    private fun openConnectionWithRedirects(
        urlStr: String,
        headers: Map<String, String>,
        maxRedirects: Int = 6
    ): HttpURLConnection {
        var currentUrl = urlStr
        var redirects = 0
        while (redirects < maxRedirects) {
            val url = URL(currentUrl)
            val conn = (url.openConnection() as HttpURLConnection).apply {
                instanceFollowRedirects = true
                connectTimeout = 15000
                readTimeout = 20000
                setRequestProperty("User-Agent", USER_AGENT)
                setRequestProperty("Accept", "*/*")
                setRequestProperty("Accept-Language", "en-US,en;q=0.9")

                // Apply permissive SSL settings if HTTPS
                if (this is HttpsURLConnection && permissiveSslContext != null) {
                    try {
                        sslSocketFactory = permissiveSslContext!!.socketFactory
                        hostnameVerifier = HostnameVerifier { _, _ -> true }
                    } catch (_: Exception) {}
                }

                // Set caller headers
                for ((k, v) in headers) {
                    setRequestProperty(k, v)
                }

                // Synchronize session cookies from CookieManager for CDN domains
                try {
                    val cookie = CookieManager.getInstance().getCookie(currentUrl)
                    if (!cookie.isNullOrBlank() && !headers.containsKey("Cookie") && !headers.containsKey("cookie")) {
                        setRequestProperty("Cookie", cookie)
                    }
                } catch (_: Throwable) {}

                // If target is Zephyrix / zn-grid, ensure proper referer and origin
                if (currentUrl.contains("zn-grid") || currentUrl.contains("zephyrix.org")) {
                    setRequestProperty("Referer", "https://play.zephyrix.org/")
                    setRequestProperty("Origin", "https://play.zephyrix.org")
                } else {
                    // Default Referer and Origin if missing
                    val hasReferer = headers.keys.any { it.equals("Referer", ignoreCase = true) }
                    if (!hasReferer) {
                        setRequestProperty("Referer", "${url.protocol}://${url.host}/")
                    }
                    val hasOrigin = headers.keys.any { it.equals("Origin", ignoreCase = true) }
                    if (!hasOrigin) {
                        setRequestProperty("Origin", "${url.protocol}://${url.host}")
                    }
                }
            }

            val code = conn.responseCode
            if (code in 301..308 && code != 304) {
                val location = conn.getHeaderField("Location") ?: conn.getHeaderField("location")
                conn.disconnect()
                if (!location.isNullOrBlank()) {
                    currentUrl = resolveUrl(currentUrl, location)
                    redirects++
                    continue
                }
            }
            return conn
        }
        throw IOException("Too many redirects: $urlStr")
    }

    private fun fetchText(urlStr: String, headers: Map<String, String>): String {
        if (urlStr.startsWith("file://")) {
            return File(URI(urlStr)).readText()
        }
        val conn = openConnectionWithRedirects(urlStr, headers)
        val reader = BufferedReader(InputStreamReader(conn.inputStream))
        val sb = StringBuilder()
        var line: String?
        while (reader.readLine().also { line = it } != null) {
            sb.append(line).append("\n")
        }
        reader.close()
        return sb.toString()
    }

    private fun fetchBytesWithRetry(urlStr: String, headers: Map<String, String>, maxRetries: Int = 5): ByteArray? {
        var attempts = 0
        var activeHeaders = headers
        while (attempts < maxRetries) {
            attempts++
            try {
                val conn = openConnectionWithRedirects(urlStr, activeHeaders)
                val code = conn.responseCode
                if (code in 200..299) {
                    return conn.inputStream.use { it.readBytes() }
                } else if (code == 403 || code == 401) {
                    conn.disconnect()
                    if (urlStr.contains("zn-grid") || urlStr.contains("zephyrix.org")) {
                        activeHeaders = activeHeaders.toMutableMap().apply {
                            put("Referer", "https://play.zephyrix.org/")
                            put("Origin", "https://play.zephyrix.org")
                        }
                    } else {
                        // If 403 Forbidden, retry with stream host as Referer (bypasses anti-hotlink check)
                        val u = URL(urlStr)
                        activeHeaders = mapOf(
                            "Referer" to "${u.protocol}://${u.host}/",
                            "Origin" to "${u.protocol}://${u.host}"
                        )
                    }
                } else {
                    conn.disconnect()
                }
            } catch (e: Exception) {
                if (isCancelled) return null
            }
            if (attempts < maxRetries) {
                try { Thread.sleep(attempts * 600L) } catch (_: Exception) {}
            }
        }
        return null
    }

    /**
     * Resolves a relative or absolute URL against a base URL.
     * Crucially preserves security tokens, authentication parameters, and queries from baseUrl
     * if the segment does not define its own query string!
     */
    fun resolveUrl(baseUrl: String, relativeOrAbsolute: String): String {
        return try {
            val rel = relativeOrAbsolute.trim()
            if (rel.startsWith("file://")) return rel

            val baseUri = URI(baseUrl)
            val resolvedUri = if (rel.startsWith("http://") || rel.startsWith("https://")) {
                URI(rel)
            } else {
                baseUri.resolve(rel)
            }

            // If the resolved URL has no query parameters, but baseUrl has query parameters
            // (e.g. auth tokens/HMAC/expiry like ?token=xyz&exp=123), carry them over!
            if (resolvedUri.query.isNullOrBlank() && !baseUri.query.isNullOrBlank()) {
                val resolvedStr = resolvedUri.toString()
                if (resolvedStr.contains("?")) "$resolvedStr&${baseUri.query}" else "$resolvedStr?${baseUri.query}"
            } else {
                resolvedUri.toString()
            }
        } catch (_: Exception) {
            relativeOrAbsolute
        }
    }

    private fun decryptAes(key: ByteArray, encrypted: ByteArray, iv: ByteArray): ByteArray {
        return try {
            val cipher = Cipher.getInstance("AES/CBC/PKCS5Padding")
            val keySpec = SecretKeySpec(key, "AES")
            val ivSpec = IvParameterSpec(iv)
            cipher.init(Cipher.DECRYPT_MODE, keySpec, ivSpec)
            cipher.doFinal(encrypted)
        } catch (e: Exception) {
            Log.w(TAG, "AES decryption warning: ${e.message}, writing raw segment")
            encrypted
        }
    }

    private fun toBytes16Big(n: Int): ByteArray {
        return ByteBuffer.allocate(16).apply {
            putLong(0L)
            putLong(n.toLong())
        }.array()
    }

    private fun hexStringToByteArray(s: String): ByteArray {
        val len = s.length
        val data = ByteArray(len / 2)
        var i = 0
        while (i < len) {
            data[i / 2] = ((Character.digit(s[i], 16) shl 4) + Character.digit(s[i + 1], 16)).toByte()
            i += 2
        }
        return data
    }
}
