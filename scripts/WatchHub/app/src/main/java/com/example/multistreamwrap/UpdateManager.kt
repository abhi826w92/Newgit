package com.example.multistreamwrap

import android.app.Activity
import android.app.Dialog
import android.content.ContentProvider
import android.content.ContentValues
import android.content.Intent
import android.database.Cursor
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

class ApkContentProvider : ContentProvider() {
    override fun onCreate(): Boolean = true
    override fun query(uri: Uri, proj: Array<String>?, sel: String?, args: Array<String>?, sort: String?): Cursor? {
        val file = File(context?.cacheDir, uri.lastPathSegment ?: return null)
        if (!file.exists()) return null
        
        val cols = proj ?: arrayOf(android.provider.OpenableColumns.DISPLAY_NAME, android.provider.OpenableColumns.SIZE)
        val cursor = android.database.MatrixCursor(cols, 1)
        val row = cursor.newRow()
        for (col in cols) {
            when (col) {
                android.provider.OpenableColumns.DISPLAY_NAME -> row.add(file.name)
                android.provider.OpenableColumns.SIZE -> row.add(file.length())
                else -> row.add(null)
            }
        }
        return cursor
    }
    override fun getType(uri: Uri): String = "application/vnd.android.package-archive"
    override fun insert(uri: Uri, values: ContentValues?): Uri? = null
    override fun delete(uri: Uri, sel: String?, args: Array<String>?) = 0
    override fun update(uri: Uri, values: ContentValues?, sel: String?, args: Array<String>?) = 0
    override fun openFile(uri: Uri, mode: String): android.os.ParcelFileDescriptor? {
        val filePath = uri.lastPathSegment ?: return null
        val file = File(context?.cacheDir, filePath)
        if (!file.exists()) throw java.io.FileNotFoundException("File not found: $uri")
        return android.os.ParcelFileDescriptor.open(file, android.os.ParcelFileDescriptor.MODE_READ_ONLY)
    }
}

object UpdateManager {

    private val executor = Executors.newSingleThreadExecutor()
    private val handler = Handler(Looper.getMainLooper())
    private val versionUrl: String get() = updateUrl()

    private fun updateUrl(): String {
        val native = NativeLib.getUpdateUrl()
        return if (native.isBlank() || native == "ERROR_LIB_NOT_LOADED") {
            "https://raw.githubusercontent.com/your-username/multistream-app/main/update.json"
        } else {
            native
        }
    }

    fun checkForUpdates(activity: Activity) {
        executor.execute {
            try {
                val conn = URL(versionUrl).openConnection() as HttpURLConnection
                conn.connectTimeout = 10000
                conn.readTimeout = 10000
                conn.connect()

                if (conn.responseCode == 200) {
                    val json = conn.inputStream.bufferedReader().readText()
                    conn.disconnect()

                    val latestVersion = extractJsonValue(json, "version")
                    val apkUrl = extractJsonValue(json, "apk_url")
                    val changelog = extractJsonValue(json, "changelog")

                    val currentVersion = getCurrentVersionName(activity)

                    if (latestVersion.isNotEmpty() && isNewerVersion(latestVersion, currentVersion)) {
                        handler.post {
                            showUpdateDialog(activity, latestVersion, changelog, apkUrl)
                        }
                    }
                }
            } catch (_: Exception) {
                // No internet or parse error
            }
        }
    }

    private fun color(context: android.content.Context, name: String): Int {
        return context.resources.getColor(context.colorRes(name))
    }

    private fun showUpdateDialog(activity: Activity, newVersion: String, changelog: String, apkUrl: String) {
        val dialog = Dialog(activity)
        dialog.requestWindowFeature(android.view.Window.FEATURE_NO_TITLE)

        val layout = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(64, 48, 64, 48)
            setBackgroundColor(color(activity, "dialog_bg"))
        }

        val title = TextView(activity).apply {
            text = "Update Available"
            setTextColor(color(activity, "text_primary"))
            textSize = 20f
            typeface = Typeface.DEFAULT_BOLD
            gravity = Gravity.START
        }

        val versionText = TextView(activity).apply {
            text = "Version $newVersion"
            setTextColor(color(activity, "text_secondary"))
            textSize = 14f
            setPadding(0, 8, 0, 0)
        }

        val changelogText = TextView(activity).apply {
            text = if (changelog.isNotEmpty()) changelog else "Bug fixes and improvements."
            setTextColor(color(activity, "text_secondary"))
            textSize = 14f
            setPadding(0, 24, 0, 24)
        }

        val progressBarLayout = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE
        }

        val progressBar = ProgressBar(activity, null, android.R.attr.progressBarStyleHorizontal).apply {
            isIndeterminate = false
            max = 100
            progress = 0
        }

        val progressText = TextView(activity).apply {
            text = "0%"
            setTextColor(color(activity, "text_secondary"))
            textSize = 12f
            gravity = Gravity.CENTER
            setPadding(0, 8, 0, 0)
        }

        progressBarLayout.addView(progressBar)
        progressBarLayout.addView(progressText)

        val btnLayout = LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.END
        }

        val btnUpdate = TextView(activity).apply {
            text = "UPDATE NOW"
            setTextColor(color(activity, "primary"))
            textSize = 14f
            typeface = Typeface.DEFAULT_BOLD
            setPadding(24, 16, 24, 16)
        }
        btnUpdate.setOnClickListener {
            btnUpdate.visibility = View.GONE
            progressBarLayout.visibility = View.VISIBLE
            downloadAndInstall(activity, apkUrl, progressBar, progressText, dialog)
        }

        btnLayout.addView(btnUpdate)

        layout.addView(title)
        layout.addView(versionText)
        layout.addView(changelogText)
        layout.addView(progressBarLayout)
        layout.addView(btnLayout)

        dialog.setContentView(layout)
        dialog.setCancelable(false) // Compulsory update, cannot dismiss
        dialog.window?.setBackgroundDrawableResource(android.R.color.transparent)
        dialog.show()
    }

    private fun downloadAndInstall(
        activity: Activity,
        apkUrl: String,
        progressBar: ProgressBar,
        progressText: TextView,
        dialog: Dialog,
    ) {
        executor.execute {
            try {
                val conn = URL(apkUrl).openConnection() as HttpURLConnection
                conn.connectTimeout = 30000
                conn.readTimeout = 30000
                conn.connect()

                if (conn.responseCode == 200) {
                    val file = File(activity.cacheDir, "update.apk")
                    val fos = FileOutputStream(file)
                    val inputStream = conn.inputStream
                    val totalSize = conn.contentLength.toLong()
                    val buffer = ByteArray(8192)
                    var downloaded = 0L
                    var read: Int

                    while (inputStream.read(buffer).also { read = it } != -1) {
                        fos.write(buffer, 0, read)
                        downloaded += read
                        if (totalSize > 0) {
                            val progress = (downloaded * 100 / totalSize).toInt()
                            handler.post {
                                progressBar.progress = progress
                                progressText.text = "$progress%"
                            }
                        }
                    }
                    fos.flush()
                    fos.close()
                    inputStream.close()
                    conn.disconnect()

                    handler.post {
                        dialog.dismiss()
                        installApk(activity, file)
                    }
                }
            } catch (e: Exception) {
                handler.post {
                    progressText.text = "Download failed. Try again."
                    progressBar.visibility = View.GONE
                    // Wait, we don't have a direct reference to btnUpdate here unless we pass it.
                    // Instead of passing it, let's just make the dialog cancelable on error so they can restart the app?
                    // Better yet, let's just exit the app.
                    progressText.setOnClickListener {
                        android.os.Process.killProcess(android.os.Process.myPid())
                    }
                    progressText.text = "Download failed. Tap to exit."
                }
            }
        }
    }

    private fun installApk(context: android.content.Context, file: File) {
        try {
            val uri: Uri
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                uri = Uri.parse("content://${context.packageName}.apk_provider/update.apk")
            } else {
                uri = Uri.fromFile(file)
            }
            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, "application/vnd.android.package-archive")
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                    addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                }
            }
            context.startActivity(intent)
        } catch (_: Exception) {
            // Fallback
        }
    }

    private fun getCurrentVersionName(context: android.content.Context): String {
        return try {
            val pInfo = context.packageManager.getPackageInfo(context.packageName, 0)
            pInfo.versionName ?: "0"
        } catch (_: Exception) {
            "0"
        }
    }

    private fun isNewerVersion(remote: String, local: String): Boolean {
        val remoteParts = remote.split(".").map { it.toIntOrNull() ?: 0 }
        val localParts = local.split(".").map { it.toIntOrNull() ?: 0 }
        val maxLen = maxOf(remoteParts.size, localParts.size)
        for (i in 0 until maxLen) {
            val r = remoteParts.getOrElse(i) { 0 }
            val l = localParts.getOrElse(i) { 0 }
            if (r > l) return true
            if (r < l) return false
        }
        return false
    }

    private fun extractJsonValue(json: String, key: String): String {
        val pattern = "\"$key\"\\s*:\\s*\"([^\"]*)\""
        val regex = Regex(pattern)
        return regex.find(json)?.groupValues?.getOrElse(1) { "" } ?: ""
    }
}
