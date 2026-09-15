package com.example.multistreamwrap

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

object HistoryManager {
    private const val PREFS_NAME = "app_history"
    private const val KEY_SITES = "history_sites"
    private const val KEY_HISTORY_ENABLED = "history_enabled"

    data class HistoryEntry(val name: String, val url: String, val timestamp: Long)

    fun isHistoryEnabled(context: Context): Boolean {
        return context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .getBoolean(KEY_HISTORY_ENABLED, true)
    }

    fun setHistoryEnabled(context: Context, enabled: Boolean) {
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .edit()
            .putBoolean(KEY_HISTORY_ENABLED, enabled)
            .apply()
    }

    fun addSite(context: Context, name: String, url: String) {
        if (!isHistoryEnabled(context)) return

        val sites = getHistory(context).toMutableList()
        // Remove if exists to update timestamp and move to top
        sites.removeAll { it.url == url }
        sites.add(0, HistoryEntry(name, url, System.currentTimeMillis()))
        
        // Keep only top 100
        if (sites.size > 100) {
            sites.removeAt(sites.size - 1)
        }
        saveHistory(context, sites)
    }

    fun getHistory(context: Context): List<HistoryEntry> {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val jsonString = prefs.getString(KEY_SITES, "[]") ?: "[]"
        val list = mutableListOf<HistoryEntry>()
        try {
            val array = JSONArray(jsonString)
            for (i in 0 until array.length()) {
                val obj = array.getJSONObject(i)
                list.add(HistoryEntry(
                    name = obj.getString("name"),
                    url = obj.getString("url"),
                    timestamp = obj.optLong("timestamp", 0)
                ))
            }
        } catch (_: Exception) {}
        return list
    }

    fun removeSite(context: Context, url: String) {
        val sites = getHistory(context).toMutableList()
        sites.removeAll { it.url == url }
        saveHistory(context, sites)
    }

    fun clearHistory(context: Context) {
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .edit()
            .remove(KEY_SITES)
            .apply()
    }

    private fun saveHistory(context: Context, sites: List<HistoryEntry>) {
        val array = JSONArray()
        for (site in sites) {
            val obj = JSONObject()
            obj.put("name", site.name)
            obj.put("url", site.url)
            obj.put("timestamp", site.timestamp)
            array.put(obj)
        }
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_SITES, array.toString())
            .apply()
    }
}
