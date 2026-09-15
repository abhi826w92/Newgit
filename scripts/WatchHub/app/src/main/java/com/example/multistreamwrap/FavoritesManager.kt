package com.example.multistreamwrap

import android.content.Context
import android.content.SharedPreferences

object FavoritesManager {

    private const val PREFS_NAME = "favorites_prefs"
    private const val KEY_FAVORITES = "favorite_sites"

    private fun getPrefs(context: Context): SharedPreferences {
        return context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
    }

    fun getFavorites(context: Context): Set<String> {
        return getPrefs(context).getStringSet(KEY_FAVORITES, emptySet())?.toSet() ?: emptySet()
    }

    fun addFavorite(context: Context, siteName: String) {
        val favorites = getFavorites(context).toMutableSet()
        favorites.add(siteName)
        getPrefs(context).edit().putStringSet(KEY_FAVORITES, favorites).apply()
    }

    fun removeFavorite(context: Context, siteName: String) {
        val favorites = getFavorites(context).toMutableSet()
        favorites.remove(siteName)
        getPrefs(context).edit().putStringSet(KEY_FAVORITES, favorites).apply()
    }

    fun isFavorite(context: Context, siteName: String): Boolean {
        return getFavorites(context).contains(siteName)
    }
}
