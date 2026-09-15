package com.example.multistreamwrap

object NativeLib {
    private var isLoaded = false
    init {
        try {
            System.loadLibrary("native-lib")
            isLoaded = true
        } catch (e: Throwable) {
            isLoaded = false
        }
    }

    private external fun getFirebaseUrlNative(): String
    private external fun getContactUrlNative(): String
    private external fun getCommunityUrlNative(): String
    private external fun getInstagramUrlNative(): String
    private external fun getGithubUrlNative(): String
    private external fun getPortfolioUrlNative(): String
    private external fun getUpdateUrlNative(): String
    private external fun getSitesDataNative(): String
    private external fun getCosmeticDataNative(): String
    private external fun isUpdateRequiredNative(currentVersion: Int, targetVersion: Int): Boolean

    fun getFirebaseUrl(): String = if (isLoaded) {
        try { getFirebaseUrlNative() } catch (_: Throwable) { "https://pymob-3bfb1-default-rtdb.firebaseio.com/dialogs/" }
    } else "https://pymob-3bfb1-default-rtdb.firebaseio.com/dialogs/"

    fun getContactUrl(): String = if (isLoaded) {
        try { getContactUrlNative() } catch (_: Throwable) { "https://t.me/R3V_X" }
    } else "https://t.me/R3V_X"

    fun getCommunityUrl(): String = if (isLoaded) {
        try { getCommunityUrlNative() } catch (_: Throwable) { "https://t.me/allinformation0173" }
    } else "https://t.me/allinformation0173"

    fun getInstagramUrl(): String = if (isLoaded) {
        try { getInstagramUrlNative() } catch (_: Throwable) { "https://www.instagram.com/opeditzxx/?utm_source=qr&r=nametag" }
    } else "https://www.instagram.com/opeditzxx/?utm_source=qr&r=nametag"

    fun getGithubUrl(): String = if (isLoaded) {
        try { getGithubUrlNative() } catch (_: Throwable) { "https://github.com/v54087912-collab" }
    } else "https://github.com/v54087912-collab"

    fun getPortfolioUrl(): String = if (isLoaded) {
        try { getPortfolioUrlNative() } catch (_: Throwable) { "https://aboutmee.pages.dev/" }
    } else "https://aboutmee.pages.dev/"

    fun getUpdateUrl(): String = if (isLoaded) {
        try { getUpdateUrlNative() } catch (_: Throwable) { "https://raw.githubusercontent.com/v54087912-collab/z-project-anime/main/update.json" }
    } else "https://raw.githubusercontent.com/v54087912-collab/z-project-anime/main/update.json"

    fun getSitesData(): String = if (isLoaded) {
        try { getSitesDataNative().ifBlank { SiteData.sites } } catch (_: Throwable) { SiteData.sites }
    } else SiteData.sites

    fun getCosmeticData(): String = if (isLoaded) {
        try { getCosmeticDataNative().ifBlank { SiteData.cosmetic } } catch (_: Throwable) { SiteData.cosmetic }
    } else SiteData.cosmetic

    fun isUpdateRequired(currentVersion: Int, targetVersion: Int): Boolean {
        return if (isLoaded) {
            try { isUpdateRequiredNative(currentVersion, targetVersion) } catch (_: Throwable) { targetVersion > currentVersion }
        } else (targetVersion > currentVersion)
    }
}
