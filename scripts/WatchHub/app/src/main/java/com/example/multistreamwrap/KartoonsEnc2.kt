package com.example.multistreamwrap

import android.util.Base64
import java.security.MessageDigest
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

object KartoonsEnc2 {

    private val keyBytes: ByteArray by lazy {
        val secret = "pmS0CAMG1Ruq49WbMyhE3fh1sOuLYEL9rtFazYYljVI2j4BPSog73hW7A7xMhceHD0iwrPrVVDXLvxyWr"
        MessageDigest.getInstance("SHA-256").digest(secret.toByteArray(Charsets.UTF_8))
    }

    fun decrypt(encStr: String): String {
        val clean = encStr.trim()
        if (!clean.startsWith("enc2:")) return clean
        return try {
            val raw = clean.removePrefix("enc2:").replace("-", "+").replace("_", "/")
            val remainder = raw.length % 4
            val padded = if (remainder > 0) raw + "=".repeat(4 - remainder) else raw
            val allBytes = Base64.decode(padded, Base64.DEFAULT)
            if (allBytes.size < 13) return clean

            val iv = allBytes.copyOfRange(0, 12)
            val ciphertext = allBytes.copyOfRange(12, allBytes.size)

            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            val keySpec = SecretKeySpec(keyBytes, "AES")
            val gcmSpec = GCMParameterSpec(128, iv)
            cipher.init(Cipher.DECRYPT_MODE, keySpec, gcmSpec)
            val decrypted = cipher.doFinal(ciphertext)
            String(decrypted, Charsets.UTF_8)
        } catch (_: Throwable) {
            clean
        }
    }

    /**
     * Decrypts all encrypted URIs and segments inside an M3U8 playlist.
     * Replaces enc2: segment URLs, key URIs, media map URIs, and sub-stream URLs.
     */
    fun decryptM3u8(playlistText: String): String {
        if (!playlistText.contains("enc2:")) return playlistText
        val sb = StringBuilder()
        val uriRegex = Regex("""URI="([^"]+)"""")

        for (line in playlistText.lines()) {
            val trimmed = line.trim()
            if (trimmed.isEmpty()) {
                sb.append("\n")
                continue
            }

            if (trimmed.startsWith("#")) {
                if (trimmed.contains("enc2:")) {
                    val replaced = uriRegex.replace(trimmed) { match ->
                        val uriVal = match.groupValues[1]
                        val dec = if (uriVal.startsWith("enc2:")) decrypt(uriVal) else uriVal
                        """URI="$dec""""
                    }
                    sb.append(replaced).append("\n")
                } else {
                    sb.append(trimmed).append("\n")
                }
            } else if (trimmed.startsWith("enc2:")) {
                sb.append(decrypt(trimmed)).append("\n")
            } else {
                sb.append(trimmed).append("\n")
            }
        }
        return sb.toString()
    }
}
