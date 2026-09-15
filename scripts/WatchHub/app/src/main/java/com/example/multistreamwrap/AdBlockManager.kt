package com.example.multistreamwrap

import android.content.Context
import android.content.SharedPreferences

/**
 * Manages the global AdBlock Extension state (ON/OFF) across the application.
 */
object AdBlockManager {

    private const val PREFS_NAME = "adblock_prefs"
    private const val KEY_ENABLED = "is_adblock_enabled"

    private fun getPrefs(context: Context): SharedPreferences {
        return context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
    }

    /** Returns true if the AdBlocker extension is enabled (defaults to true). */
    fun isAdBlockEnabled(context: Context): Boolean {
        return getPrefs(context).getBoolean(KEY_ENABLED, true)
    }

    fun setAdBlockEnabled(context: Context, enabled: Boolean) {
        getPrefs(context).edit().putBoolean(KEY_ENABLED, enabled).apply()
    }

    /** Toggles the state and returns the new state. */
    fun toggleAdBlock(context: Context): Boolean {
        val newState = !isAdBlockEnabled(context)
        setAdBlockEnabled(context, newState)
        return newState
    }
}
