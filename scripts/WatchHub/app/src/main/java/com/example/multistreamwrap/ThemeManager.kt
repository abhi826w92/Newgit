package com.example.multistreamwrap

import android.app.Activity
import android.content.Context
import android.content.SharedPreferences
import android.content.res.Configuration

object ThemeManager {

    private const val PREFS_NAME = "theme_prefs"
    private const val KEY_THEME = "theme_mode"

    const val THEME_LIGHT = 0
    const val THEME_DARK = 1

    private fun getPrefs(context: Context): SharedPreferences {
        return context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
    }

    fun getThemeMode(context: Context): Int {
        return getPrefs(context).getInt(KEY_THEME, THEME_DARK)
    }

    fun setThemeMode(context: Context, mode: Int) {
        getPrefs(context).edit().putInt(KEY_THEME, mode).apply()
    }

    fun toggleTheme(context: Context): Boolean {
        val newMode = if (isDarkMode(context)) THEME_LIGHT else THEME_DARK
        setThemeMode(context, newMode)
        return newMode == THEME_DARK
    }

    fun wrapContext(base: Context): Context {
        val isDark = isDarkMode(base)
        val config = Configuration(base.resources.configuration)
        config.uiMode = if (isDark) {
            (config.uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or Configuration.UI_MODE_NIGHT_YES
        } else {
            (config.uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or Configuration.UI_MODE_NIGHT_NO
        }
        return base.createConfigurationContext(config)
    }

    fun applyTheme(activity: Activity) {
        val isDark = isDarkMode(activity)
        val themeName = if (isDark) "Theme.MultiStream" else "Theme.MultiStream.Light"
        val themeId = activity.resources.getIdentifier(themeName, "style", activity.packageName)
        if (themeId != 0) {
            activity.setTheme(themeId)
        }
    }

    fun isDarkMode(context: Context): Boolean {
        val mode = getThemeMode(context)
        return mode == THEME_DARK
    }
}
