package com.example.multistreamwrap

import android.app.Activity
import android.app.KeyguardManager
import android.content.Context
import android.content.Intent
import android.hardware.biometrics.BiometricPrompt
import android.os.Build
import android.os.CancellationSignal
import android.widget.Toast

object SiteLockManager {
    private const val PREFS_NAME = "site_lock_prefs"
    private const val KEY_CUSTOM_LOCKED = "custom_locked_sites"

    fun isAutoLockedCategory(categoryName: String?): Boolean {
        if (categoryName == null) return false
        val lower = categoryName.lowercase()
        return lower.contains("18+") || lower.contains("adult") || lower.contains("hentai") || lower.contains("porn") || lower.contains("erotica") || lower.contains("nsfw")
    }

    fun getCategoryForSite(siteName: String): String? {
        for (cat in SitesProvider.categories) {
            if (cat.sites.any { it.name.equals(siteName, ignoreCase = true) || it.url.equals(siteName, ignoreCase = true) }) return cat.name
        }
        return null
    }

    fun isSiteLocked(context: Context, siteName: String): Boolean {
        val lowerName = siteName.lowercase()
        if (lowerName.contains("hentai") || lowerName.contains("xxx") || lowerName.contains("porn") || lowerName.contains("sex") || lowerName.contains("xhamster") || lowerName.contains("adult")) {
            return true
        }

        val catName = getCategoryForSite(siteName)
        if (isAutoLockedCategory(catName)) return true
        return isCustomLocked(context, siteName)
    }

    fun isCustomLocked(context: Context, siteName: String): Boolean {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val lockedSet = prefs.getStringSet(KEY_CUSTOM_LOCKED, emptySet()) ?: emptySet()
        return lockedSet.contains(siteName)
    }

    fun lockSite(context: Context, siteName: String) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val lockedSet = (prefs.getStringSet(KEY_CUSTOM_LOCKED, emptySet()) ?: emptySet()).toMutableSet()
        lockedSet.add(siteName)
        prefs.edit().putStringSet(KEY_CUSTOM_LOCKED, lockedSet).apply()
    }

    fun unlockSite(context: Context, siteName: String) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val lockedSet = (prefs.getStringSet(KEY_CUSTOM_LOCKED, emptySet()) ?: emptySet()).toMutableSet()
        lockedSet.remove(siteName)
        prefs.edit().putStringSet(KEY_CUSTOM_LOCKED, lockedSet).apply()
    }

    fun authenticate(activity: Activity, title: String, onSuccess: () -> Unit, onFailure: (() -> Unit)? = null) {
        val keyguardManager = activity.getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager
        val isSecure = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            keyguardManager.isDeviceSecure
        } else {
            keyguardManager.isKeyguardSecure
        }

        if (!isSecure) {
            Toast.makeText(activity, "No lock screen set on device", Toast.LENGTH_SHORT).show()
            onSuccess()
            return
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            try {
                val builder = BiometricPrompt.Builder(activity)
                    .setTitle(title)
                    .setSubtitle("Confirm identity to proceed")
                
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                    builder.setAllowedAuthenticators(
                        android.hardware.biometrics.BiometricManager.Authenticators.BIOMETRIC_STRONG or
                        android.hardware.biometrics.BiometricManager.Authenticators.DEVICE_CREDENTIAL
                    )
                } else {
                    @Suppress("DEPRECATION")
                    builder.setNegativeButton("Cancel", activity.mainExecutor) { _, _ ->
                        onFailure?.invoke()
                    }
                }
                
                val prompt = builder.build()
                prompt.authenticate(
                    CancellationSignal(),
                    activity.mainExecutor,
                    object : BiometricPrompt.AuthenticationCallback() {
                        override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult?) {
                            activity.runOnUiThread { onSuccess() }
                        }

                        override fun onAuthenticationError(errorCode: Int, errString: CharSequence?) {
                            activity.runOnUiThread {
                                if (errorCode != BiometricPrompt.BIOMETRIC_ERROR_CANCELED && errorCode != BiometricPrompt.BIOMETRIC_ERROR_USER_CANCELED) {
                                    Toast.makeText(activity, "Authentication failed: $errString", Toast.LENGTH_SHORT).show()
                                }
                                onFailure?.invoke()
                            }
                        }

                        override fun onAuthenticationFailed() {
                            // Biometric failed once, retry automatically
                        }
                    }
                )
                return
            } catch (_: Exception) {}
        }

        // Fallback for older devices or if BiometricPrompt fails: use Keyguard intent
        @Suppress("DEPRECATION")
        val intent = keyguardManager.createConfirmDeviceCredentialIntent(title, "Please enter your device PIN/Pattern")
        if (intent != null) {
            // Note: Keyguard intent requires startActivityForResult in Activity
            val authLauncherActivity = activity as? AuthLauncher
            if (authLauncherActivity != null) {
                authLauncherActivity.launchAuthIntent(intent, onSuccess, onFailure)
            } else {
                onSuccess()
            }
        } else {
            onSuccess()
        }
    }

    interface AuthLauncher {
        fun launchAuthIntent(intent: Intent, onSuccess: () -> Unit, onFailure: (() -> Unit)?)
    }
}
