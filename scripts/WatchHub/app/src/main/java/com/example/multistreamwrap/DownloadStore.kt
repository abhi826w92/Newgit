package com.example.multistreamwrap

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

data class DownloadRecord(
    val id: String,
    val title: String,
    val filePath: String,
    val fileSize: Long,
    val timestamp: Long = System.currentTimeMillis()
) {
    val formattedSize: String
        get() {
            if (fileSize <= 0) return "Unknown size"
            val kb = fileSize / 1024.0
            val mb = kb / 1024.0
            val gb = mb / 1024.0
            return when {
                gb >= 1.0 -> String.format(Locale.US, "%.1f GB", gb)
                mb >= 1.0 -> String.format(Locale.US, "%.1f MB", mb)
                else -> String.format(Locale.US, "%.1f KB", kb)
            }
        }

    val formattedDate: String
        get() {
            val sdf = SimpleDateFormat("dd MMM, hh:mm a", Locale.getDefault())
            return sdf.format(Date(timestamp))
        }
}

object DownloadStore {

    private const val PREFS_NAME = "watchhub_downloads"
    private const val KEY_RECORDS = "download_records"

    fun addRecord(context: Context, record: DownloadRecord) {
        val list = getRecords(context).toMutableList()
        // Avoid duplicate by path
        list.removeAll { it.filePath == record.filePath }
        list.add(0, record)
        saveRecords(context, list)
    }

    fun getRecords(context: Context): List<DownloadRecord> {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val jsonStr = prefs.getString(KEY_RECORDS, null) ?: return emptyList()
        return try {
            val array = JSONArray(jsonStr)
            val result = mutableListOf<DownloadRecord>()
            for (i in 0 until array.length()) {
                val obj = array.getJSONObject(i)
                val filePath = obj.optString("filePath", "")
                // Verify file exists on disk
                if (filePath.isNotEmpty() && File(filePath).exists()) {
                    result.add(
                        DownloadRecord(
                            id = obj.optString("id", System.currentTimeMillis().toString()),
                            title = obj.optString("title", "Video"),
                            filePath = filePath,
                            fileSize = obj.optLong("fileSize", File(filePath).length()),
                            timestamp = obj.optLong("timestamp", System.currentTimeMillis())
                        )
                    )
                }
            }
            result
        } catch (_: Exception) {
            emptyList()
        }
    }

    fun deleteRecord(context: Context, id: String): Boolean {
        val list = getRecords(context).toMutableList()
        val toRemove = list.firstOrNull { it.id == id }
        if (toRemove != null) {
            try {
                val file = File(toRemove.filePath)
                if (file.exists()) file.delete()
            } catch (_: Exception) {}
            list.remove(toRemove)
            saveRecords(context, list)
            return true
        }
        return false
    }

    private fun saveRecords(context: Context, list: List<DownloadRecord>) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val array = JSONArray()
        for (item in list) {
            val obj = JSONObject()
            obj.put("id", item.id)
            obj.put("title", item.title)
            obj.put("filePath", item.filePath)
            obj.put("fileSize", item.fileSize)
            obj.put("timestamp", item.timestamp)
            array.put(obj)
        }
        prefs.edit().putString(KEY_RECORDS, array.toString()).apply()
    }
}
